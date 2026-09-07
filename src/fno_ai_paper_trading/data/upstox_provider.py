"""Upstox REST (V3) historical market-data provider.

This is the second real vendor adapter (after Kite Connect) and it is
deliberately *read-only*:

* it fetches historical OHLCV candles only (``GET /v3/historical-candle``);
* quotes / last price are derived from the same historical endpoint;
* there is no order API, no place-order path and no write endpoint anywhere —
  live execution remains impossible through this class.

Everything it returns is the normalized internal domain model
(:class:`~fno_ai_paper_trading.models.market.MarketPrice`), so the rest of the
application never depends on Upstox specifics.

Documented Upstox V3 facts this adapter relies on (verified against the
official API docs):

* auth header: ``Authorization: Bearer {access_token}``
* instrument key format: ``{SEGMENT}|{symbol}`` — e.g. ``NSE_FO|NIFTY 27 MAR 2025``,
  ``NSE_INDEX|Nifty 50``
* historical-candle path: ``/v3/historical-candle/{instrument_key}/{unit}/{interval}/{to_date}/{from_date}``
  where ``unit`` is ``minutes``/``hours``/``days``/``weeks``/``months`` and
  ``to_date`` is required and inclusive; ``from_date`` is optional
* candle row: ``[iso_timestamp, open, high, low, close, volume, open_interest]``
* data depth: minutes/hours since Jan 2022, days since Jan 2000
* per-request retrieval caps (see ``UPSTOX_MAX_WINDOW_DAYS``): 1 month for
  minute bars up to 15, 1 quarter for 30-minute and hourly bars, ~1 decade for
  daily bars, unlimited for weekly/monthly. Ranges longer than one request are
  fetched as contiguous, non-overlapping windows and merged transparently.

Rules honoured here (same as the Kite adapter):

* credentials come from the caller (env-driven; never hard-coded)
* requests time out; transient failures / rate limits (429) are retried with backoff
* non-transient failures surface as typed ``data.errors`` subclasses
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from fno_ai_paper_trading.data.errors import (
    AuthenticationError,
    InstrumentNotFoundError,
    MarketDataError,
    ProviderConfigurationError,
    RateLimitError,
    UnavailableError,
)
from fno_ai_paper_trading.data.intervals import canonical_interval, upstox_unit_interval
from fno_ai_paper_trading.data.market_hours import NSE_TZ, market_session
from fno_ai_paper_trading.data.provider import MarketDataProvider
from fno_ai_paper_trading.models.instruments import Instrument
from fno_ai_paper_trading.models.market import MarketPrice, MarketQuote, MarketSession
from fno_ai_paper_trading.utils.http import HttpError, http_get, is_retryable_status
from fno_ai_paper_trading.utils.retry import RetryExhausted, describe_last_error, retry_call

UPSTOX_BASE_URL = "https://api.upstox.com"

# Instrument key = `{SEGMENT}|{symbol}`; segments like NSE_EQ / NSE_FO / NSE_INDEX / MCX_FO.
_INSTRUMENT_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z_]*\|[\w. ]+$")

# Upstox error codes reported with bad requests (UDAPI-prefixed, per the docs).
_ERROR_CODE_INSTRUMENT = frozenset({"UDAPI100011", "UDAPI1021"})
_ERROR_CODE_DATES = frozenset({"UDAPI1015", "UDAPI1016", "UDAPI1014", "UDAPI1022"})
_ERROR_CODE_INTERVAL = frozenset({"UDAPI1146", "UDAPI1147"})
_ERROR_CODE_RANGE = frozenset({"UDAPI1148"})

# Per-request retrieval caps for ``GET /v3/historical-candle``, in calendar
# days, keyed by canonical interval token. Values stay inside the documented
# limits (minutes <=15: 1 month; minutes >15 / hours: 1 quarter; days: 1
# decade). ``None`` = single request covers the whole range.
UPSTOX_MAX_WINDOW_DAYS: dict[str, int | None] = {
    "1m": 30, "3m": 30, "5m": 30, "10m": 30, "15m": 30,
    "30m": 89, "1h": 89, "2h": 89, "3h": 89, "4h": 89,
    "1d": 3500, "1w": None, "1M": None,
}


def upstox_instrument_key(segment: str, symbol: str) -> str:
    """Build an Upstox V3 instrument key, e.g. ``NSE_FO|NIFTY 27 MAR 2025``.

    Raises :class:`ValueError` when ``segment`` or ``symbol`` are missing/empty.
    """
    segment = (segment or "").strip().upper().replace(" ", "_")
    symbol = (symbol or "").strip()
    if not segment or not symbol:
        raise ValueError("both an exchange segment and a symbol are required for an Upstox key")
    key = f"{segment}|{symbol}"
    if _INSTRUMENT_KEY_RE.fullmatch(key) is None:
        raise ValueError(f"invalid Upstox instrument key {key!r}; expected SEGMENT|SYMBOL")
    return key


def _historical_date_windows(
    start: datetime, end: datetime, max_days: int | None
) -> list[tuple[str, str]]:
    """Split ``[start, end]`` into ``(from_date, to_date)`` request windows.

    Returns ``YYYY-MM-DD`` strings for the Upstox path. A single window covers
    the whole range when ``max_days`` is ``None`` or the range already fits;
    otherwise contiguous, non-overlapping day windows step forward from
    ``start`` so the merged series stays chronological.
    """
    end_date = end.date()
    if max_days is None or (end_date - start.date()).days <= max_days:
        return [(start.date().isoformat(), end_date.isoformat())]
    windows: list[tuple[str, str]] = []
    cursor = start.date()
    while cursor < end_date:
        stop = min(end_date, cursor + timedelta(days=max_days))
        windows.append((cursor.isoformat(), stop.isoformat()))
        cursor = stop + timedelta(days=1)
    return windows


class UpstoxHistoricalDataProvider(MarketDataProvider):
    """Read-only adapter for the Upstox REST V3 historical-candle API.

    Args:
        access_token: Upstox access token (env-driven, never hard-coded).
        base_url: API base; overridable for testing.
        timeout_seconds: per-request timeout.
        max_retries: additional attempts after the first (0 = no retries).
        retry_delay: initial backoff delay between attempts.
        interval: default bar interval token (``"1d"``) for windowed calls.
        default_days: default look-back window for calls with no dates.
        instruments: optional local instrument list served by
            :meth:`get_instruments` (Upstox master download is intentionally
            *not* automated).
        sleep: sleep callable (injectable for deterministic tests).
        now_fn: "now" callable (injectable for deterministic tests).
    """

    def __init__(
        self,
        access_token: str,
        base_url: str = UPSTOX_BASE_URL,
        timeout_seconds: float = 10.0,
        max_retries: int = 3,
        retry_delay: float = 0.1,
        interval: str = "1d",
        default_days: int = 30,
        instruments: list[Instrument] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        now_fn: Callable[[], datetime] = datetime.now,
    ) -> None:
        self.access_token = (access_token or "").strip()
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = float(timeout_seconds)
        self.max_retries = int(max_retries)
        self.retry_delay = float(retry_delay)
        if canonical_interval(interval) is None:
            raise ValueError(
                f"unknown interval {interval!r}; expected a canonical bar interval"
            )
        self.interval = canonical_interval(interval)  # type: ignore[assignment]
        self.default_days = int(default_days)
        self._instruments: list[Instrument] = []
        if instruments is not None:
            self._instruments = list(instruments)
        self._now_fn = now_fn
        self._sleep = sleep

    # ------------------------------------------------------------------ auth

    @property
    def _headers(self) -> dict[str, str]:
        if not self.access_token:
            raise ProviderConfigurationError(
                "Upstox provider requires an access token "
                "(set UPSTOX_ACCESS_TOKEN in .env)"
            )
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Accept": "application/json",
        }

    # ------------------------------------------------------------- transport

    def _get(self, path: str):
        url = f"{self.base_url}{path}"

        def attempt():
            headers = self._headers  # raises ProviderConfigurationError if unconfigured
            try:
                return http_get(url, headers=headers, timeout=self.timeout_seconds)
            except HttpError as exc:
                detail = self._error_from_body(exc.text)
                code = detail.get("code")
                if exc.status in (401, 403):
                    raise AuthenticationError(
                        f"Upstox rejected the access token (HTTP {exc.status})"
                    ) from exc
                if exc.status == 404:
                    raise InstrumentNotFoundError(f"Upstox returned 404 for {exc.url}") from exc
                if exc.status == 400 and code in _ERROR_CODE_INSTRUMENT:
                    raise InstrumentNotFoundError(
                        f"Upstox rejected instrument key for {exc.url}: {detail.get('message')}"
                    ) from exc
                if exc.status == 400 and code in _ERROR_CODE_INTERVAL:
                    raise MarketDataError(
                        f"invalid Upstox bar interval for {exc.url}: {detail.get('message')}"
                    ) from exc
                if exc.status == 400 and (code in _ERROR_CODE_DATES or code in _ERROR_CODE_RANGE):
                    raise MarketDataError(
                        f"invalid Upstox date range for {exc.url}: {detail.get('message')}"
                    ) from exc
                if is_retryable_status(exc.status):
                    raise _RetryableHttpError(exc.status, exc.url, exc.body) from exc
                raise MarketDataError(
                    f"unexpected HTTP {exc.status} for {exc.url}: {detail.get('message')}"
                ) from exc
            except OSError as exc:
                # DNS / connection / timeout failures are transient.
                raise _RetryableHttpError(None, url) from exc

        try:
            return retry_call(
                attempt,
                attempts=self.max_retries + 1,
                delay=self.retry_delay,
                backoff=2.0,
                max_delay=self.retry_delay * 8,
                exceptions=(_RetryableHttpError,),
                sleep=self._sleep,
            )
        except RetryExhausted as exc:
            cause = describe_last_error(exc)
            status = getattr(cause, "status", None)
            if status == 429:
                raise RateLimitError(
                    "Upstox rate limit hit (429) and retries exhausted"
                ) from exc
            raise UnavailableError("Upstox service unavailable after retries") from exc

    @staticmethod
    def _error_from_body(text: str) -> dict[str, str]:
        """Best-effort extraction of ``error_code``/``error_message`` from a body."""
        if not text:
            return {}
        try:
            payload = json.loads(text) if isinstance(text, str) else {}
        except (ValueError, TypeError):
            return {}
        code = payload.get("error_code") or ""
        message = payload.get("error_message") or ""
        errors = payload.get("errors")
        if isinstance(errors, list) and errors:
            first = errors[0]
            if isinstance(first, dict):
                code = code or first.get("code") or ""
                message = message or first.get("message") or ""
        return {"code": str(code), "message": str(message)}

    # ------------------------------------------------------- instrument keys

    def _instrument_key(self, instrument: Instrument) -> str:
        key = (instrument.exchange_token or "").strip()
        if not key:
            raise InstrumentNotFoundError(
                f"'{instrument.symbol}' needs an Upstox instrument key; set "
                "instrument.exchange_token = 'SEGMENT|SYMBOL' (e.g. "
                "'NSE_FO|NIFTY 27 MAR 2025')"
            )
        if _INSTRUMENT_KEY_RE.fullmatch(key) is None:
            raise MarketDataError(
                f"invalid Upstox instrument key {key!r}; expected SEGMENT|SYMBOL"
            )
        return key

    # ------------------------------------------------------------ instruments

    def get_instruments(self) -> list[Instrument]:
        """Return the locally-supplied instrument list (never auto-downloaded).

        Raises :class:`MarketDataError` when no list was provided at construction;
        Upstox master-file downloads are intentionally not automated.
        """
        if not self._instruments:
            raise MarketDataError(
                "UpstoxHistoricalDataProvider has no local instrument list; pass "
                "instruments=[...] at construction (master download is not automated)"
            )
        return list(self._instruments)

    def get_instrument(self, symbol: str) -> Instrument | None:
        for instrument in self.get_instruments():
            if instrument.symbol == symbol:
                return instrument
        return None

    # --------------------------------------------------------------- history

    def _historical(
        self,
        instrument: Instrument,
        interval: str,
        start: datetime,
        end: datetime,
    ) -> list[MarketPrice]:
        if start.tzinfo is not None or end.tzinfo is not None:
            raise MarketDataError("start/end must be naive datetimes (naive IST)")
        if start > end:
            raise MarketDataError(
                f"start ({start:%Y-%m-%d}) must not be after end ({end:%Y-%m-%d})"
            )

        key = self._instrument_key(instrument)
        try:
            unit, number = upstox_unit_interval(interval)
            token = canonical_interval(interval)
        except ValueError as exc:
            raise MarketDataError(str(exc)) from exc

        # Upstox caps how much history one request may return (see
        # UPSTOX_MAX_WINDOW_DAYS); longer ranges are fetched as contiguous,
        # non-overlapping windows and merged into one chronological series.
        windows = _historical_date_windows(start, end, UPSTOX_MAX_WINDOW_DAYS.get(token))

        bars: list[MarketPrice] = []
        for from_date, to_date in windows:
            path = (
                f"/v3/historical-candle/{key}/{unit}/{number}/{to_date}/{from_date}"
            )
            response = self._get(path)
            payload = response.json
            if not isinstance(payload, dict) or payload.get("status") not in ("success", None):
                detail = self._error_from_body(response.text)
                raise MarketDataError(
                    f"Upstox returned an unsuccessful response for {key}: {detail.get('message')}"
                )
            candles = ((payload.get("data") or {}).get("candles")) or []
            if not isinstance(candles, list):
                raise MarketDataError(f"unexpected Upstox candle payload shape for {key}")

            for index, candle in enumerate(candles):
                bars.append(self._candle_to_bar(instrument, candle, index, key))

        # The merged series must be globally chronological and duplicate-free.
        for previous, current in zip(bars, bars[1:]):
            if current.timestamp <= previous.timestamp:
                raise MarketDataError(
                    f"{key}: candles are out of order or contain duplicate timestamps "
                    f"({previous.timestamp.isoformat()} -> {current.timestamp.isoformat()})"
                )
        return bars

    @staticmethod
    def _candle_to_bar(
        instrument: Instrument, candle: Any, index: int, key: str
    ) -> MarketPrice:
        """Normalize one raw candle row into a validated :class:`MarketPrice`.

        Raises :class:`MarketDataError` for malformed or structurally invalid
        rows — bad data is never silently dropped or fabricated.
        """
        context = f"{key} candle index {index}"
        if not isinstance(candle, list) or len(candle) < 6:
            raise MarketDataError(
                f"{context}: expected [ts, open, high, low, close, volume[, oi]]"
            )
        timestamp = UpstoxHistoricalDataProvider._parse_timestamp(candle[0])
        if timestamp is None:
            raise MarketDataError(f"{context}: unparseable timestamp {candle[0]!r}")
        values: list[Decimal] = []
        for name, value in zip(("open", "high", "low", "close"), candle[1:5]):
            parsed = UpstoxHistoricalDataProvider._parse_decimal(value)
            if parsed is None:
                raise MarketDataError(f"{context}: non-numeric {name} {value!r}")
            values.append(parsed)
        volume_raw = candle[5]
        try:
            volume = int(volume_raw) if volume_raw is not None else 0
        except (TypeError, ValueError):
            raise MarketDataError(f"{context}: non-integer volume {volume_raw!r}") from None
        oi_raw = candle[6] if len(candle) > 6 else None
        open_interest = None
        if oi_raw is not None:
            try:
                open_interest = int(oi_raw)
            except (TypeError, ValueError):
                raise MarketDataError(f"{context}: non-integer open_interest {oi_raw!r}") from None

        try:
            return MarketPrice(
                instrument=instrument,
                timestamp=timestamp,
                open=values[0],
                high=values[1],
                low=values[2],
                close=values[3],
                volume=volume,
                open_interest=open_interest,
            )
        except ValueError as exc:
            raise MarketDataError(f"{context}: invalid OHLC semantics ({exc})") from exc

    def get_historical_ohlcv(
        self,
        instrument: Instrument,
        interval: str = "day",
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[MarketPrice]:
        """Return normalized OHLCV bars in the ``[start, end]`` window.

        Bars are chronological (oldest first) with naive-IST timestamps. An
        empty window returns an empty list. Duplicate or out-of-order candles
        from the provider raise :class:`MarketDataError`. Ranges longer than a
        single Upstox request (see ``UPSTOX_MAX_WINDOW_DAYS``) are fetched as
        contiguous, non-overlapping windows and merged transparently.
        """
        now = self._now_fn()
        end = end if end is not None else now
        start = start if start is not None else end - timedelta(days=self.default_days)
        return self._historical(instrument, interval, start, end)

    # ---------------------------------------------------------------- quotes

    def get_ohlcv(self, instrument: Instrument, limit: int | None = None) -> list[MarketPrice]:
        """Recent bars at the provider's default interval.

        ``limit`` keeps only the most recent bars (0 yields an empty list).
        """
        now = self._now_fn()
        bars = self.get_historical_ohlcv(instrument, self.interval, None, now)
        if limit is None:
            return bars
        if limit < 0:
            raise ValueError("limit must be >= 0")
        if limit == 0:
            return []
        return bars[-limit:]

    def get_market_price(self, instrument: Instrument) -> MarketPrice:
        bars = self.get_ohlcv(instrument, limit=1)
        if not bars:
            raise MarketDataError(f"no market data available for '{instrument.symbol}'")
        return bars[-1]

    def get_last_price(self, instrument: Instrument) -> Decimal:
        return self.get_market_price(instrument).close

    def get_quote(self, instrument: Instrument) -> MarketQuote:
        bar = self.get_market_price(instrument)
        return MarketQuote(
            instrument=instrument,
            timestamp=bar.timestamp,
            last_price=bar.close,
            open=bar.open,
            high=bar.high,
            low=bar.low,
            previous_close=bar.close,
            volume=bar.volume,
            open_interest=bar.open_interest,
        )

    # ---------------------------------------------------------------- session

    def get_market_session(self) -> MarketSession:
        """Deterministic local NSE session state (no extra network call).

        The historical-data adapter does not call Upstox market-status
        endpoints; the session snapshot derives from the configured NSE market
        hours instead.
        """
        now = self._now_fn()
        return market_session(now, label="NSE via Upstox (local session state)")

    # --------------------------------------------------------------- helpers

    @staticmethod
    def _parse_decimal(value: Any) -> Decimal | None:
        if value is None:
            return None
        try:
            result = Decimal(str(value))
        except (InvalidOperation, ValueError):
            return None
        if not result.is_finite():
            return None
        return result

    @staticmethod
    def _parse_timestamp(value: Any) -> datetime | None:
        """Parse Upstox timestamps: ISO-8601 strings or epoch seconds/millis.

        Always returns a naive datetime expressed in IST (UTC+05:30).
        """
        if isinstance(value, (int, float)):
            epoch = float(value)
            if abs(epoch) > 1e12:  # milliseconds
                epoch = epoch / 1000.0
            try:
                return datetime.fromtimestamp(epoch, tz=NSE_TZ).replace(tzinfo=None)
            except (OverflowError, OSError, ValueError):
                return None
        if not isinstance(value, str):
            return None
        text = value.strip()
        if text.isdigit() or (text[:1] == "-" and text[1:].isdigit()):
            return UpstoxHistoricalDataProvider._parse_timestamp(float(text))
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(NSE_TZ)
        return parsed.replace(tzinfo=None)


class _RetryableHttpError(Exception):
    """Internal marker: a transient failure worth retrying (network/429/5xx)."""

    def __init__(self, status: int | None, url: str, body: bytes = b"") -> None:
        self.status = status
        self.url = url
        self.body = body
        super().__init__(f"retryable failure status={status} for {url}")