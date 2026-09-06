"""Zerodha Kite Connect v3 market-data provider.

This is the only module that knows the Kite vendor specifics:

* base URL and path scheme
* authentication format (``Authorization: token <api_key>:<access_token>``)
* the ``X-Kite-Version: 3`` header
* HTTP/CSV response shapes

Everything it returns is a normalized internal domain model, so the rest of the
application never depends on Kite. The provider is deliberately *read-only*:
it fetches quotes, the instrument master and historical candles only — no
order routing of any kind exists here, and live executions remain impossible.

Rules honoured here:

* credentials must come from the caller (env-driven; never hard-coded)
* requests time out and transient failures/rate limits are retried with backoff
* non-transient failures surface as typed ``data.errors`` subclasses
"""
from __future__ import annotations

import csv
import io
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
from fno_ai_paper_trading.data.market_hours import NSE_TZ, market_session
from fno_ai_paper_trading.data.provider import MarketDataProvider
from fno_ai_paper_trading.models.enums import InstrumentType, MarketPhase
from fno_ai_paper_trading.models.instruments import Instrument
from fno_ai_paper_trading.models.market import MarketPrice, MarketQuote, MarketSession
from fno_ai_paper_trading.utils.http import HttpError, http_get, is_retryable_status
from fno_ai_paper_trading.utils.retry import RetryExhausted, describe_last_error, retry_call

# Interval buckets Kite Connect accepts for historical candles.
KITE_INTERVALS = frozenset(
    {"minute", "3minute", "5minute", "10minute", "15minute", "30minute", "60minute", "day"}
)

# Kite instrument-master CSV column order (v3).
_MASTER_COLUMNS = (
    "instrument_token",
    "exchange_token",
    "tradingsymbol",
    "name",
    "last_price",
    "expiry",
    "strike",
    "tick_size",
    "lot_size",
    "instrument_type",
    "segment",
    "exchange",
)

# Map Kite instrument_type strings onto internal types.
_TYPE_MAP = {
    "FUT": InstrumentType.FUTURE,
    "CE": InstrumentType.OPTION_CE,
    "PE": InstrumentType.OPTION_PE,
}


class _RetryableHttpError(Exception):
    """Internal marker: a transient failure worth retrying (network/429/5xx)."""

    def __init__(self, status: int | None, url: str, body: bytes = b"") -> None:
        self.status = status
        self.url = url
        self.body = body
        super().__init__(f"retryable failure status={status} for {url}")


class KiteConnectProvider(MarketDataProvider):
    """Read-only adapter for the Kite Connect v3 REST API.

    Args:
        api_key: Kite Connect API key (env-driven, never hard-coded).
        access_token: Kite Connect access/session token (env-driven).
        base_url: API base; overridable for testing.
        timeout_seconds: per-request timeout.
        max_retries: additional attempts after the first (0 = no retries).
        retry_delay: initial backoff delay between attempts.
        sleep: sleep callable (injectable for deterministic tests).
        now_fn: "now" callable (injectable for deterministic tests).
    """

    def __init__(
        self,
        api_key: str,
        access_token: str,
        base_url: str = "https://api.kite.trade",
        timeout_seconds: float = 10.0,
        max_retries: int = 3,
        retry_delay: float = 0.1,
        sleep: Callable[[float], None] = time.sleep,
        now_fn: Callable[[], datetime] = datetime.now,
    ) -> None:
        self.api_key = (api_key or "").strip()
        self.access_token = (access_token or "").strip()
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = float(timeout_seconds)
        self.max_retries = int(max_retries)
        self.retry_delay = float(retry_delay)
        self._sleep = sleep
        self._now_fn = now_fn
        self._master: list[Instrument] | None = None

    # ------------------------------------------------------------------ auth

    @property
    def _headers(self) -> dict[str, str]:
        if not self.api_key or not self.access_token:
            raise ProviderConfigurationError(
                "Kite provider requires both api_key and access_token (set FNO_KITE_API_KEY / "
                "FNO_KITE_ACCESS_TOKEN in .env)"
            )
        return {
            "Authorization": f"token {self.api_key}:{self.access_token}",
            "X-Kite-Version": "3",
            "Accept": "application/json",
        }

    # ------------------------------------------------------------- transport

    def _get(self, path: str, params: dict[str, Any] | None = None):
        url = f"{self.base_url}{path}"

        def attempt():
            headers = self._headers  # raises ProviderConfigurationError if unconfigured
            try:
                return http_get(url, params=params, headers=headers, timeout=self.timeout_seconds)
            except HttpError as exc:
                status = exc.status
                if status in (401, 403):
                    raise AuthenticationError(f"Kite rejected credentials (HTTP {status})") from exc
                if status == 404:
                    raise InstrumentNotFoundError(f"Kite returned 404 for {exc.url}") from exc
                if is_retryable_status(status):
                    raise _RetryableHttpError(status, exc.url, exc.body) from exc
                raise MarketDataError(f"unexpected HTTP {status} for {exc.url}") from exc
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
                raise RateLimitError("Kite rate limit hit and retries exhausted") from exc
            raise UnavailableError("Kite service unavailable after retries") from exc

    # ------------------------------------------------------------ instruments

    def get_instruments(self) -> list[Instrument]:
        """Fetch and parse the full Kite instrument master (CSV), F&O rows only."""
        if self._master is None:
            response = self._get("/instruments")
            self._master = self._parse_master(response.text)
        return list(self._master)

    def _parse_master(self, csv_text: str) -> list[Instrument]:
        instruments: list[Instrument] = []
        reader = csv.reader(io.StringIO(csv_text))
        header = next(reader, None)
        if header is None or header[0].strip().lower() != "instrument_token":
            raise MarketDataError("unexpected Kite instrument master format (missing header)")

        for row in reader:
            if len(row) < len(_MASTER_COLUMNS):
                continue
            record = dict(zip(_MASTER_COLUMNS, row))
            if record.get("segment") != "NFO":
                continue
            instrument_type = _TYPE_MAP.get(record.get("instrument_type", ""))
            if instrument_type is None:
                continue
            try:
                expiry = (
                    datetime.strptime(record["expiry"], "%d-%m-%Y").date()
                    if record.get("expiry")
                    else None
                )
                strike = Decimal(record["strike"]) if record.get("strike") else None
                lot_size = int(record.get("lot_size") or 1)
                tick_size = Decimal(record.get("tick_size") or "0.05")
            except (ValueError, InvalidOperation):
                continue
            instruments.append(
                Instrument(
                    symbol=record["tradingsymbol"].strip(),
                    instrument_type=instrument_type,
                    underlying_symbol=(record.get("name") or record["tradingsymbol"]).strip(),
                    expiry=expiry,
                    strike=strike,
                    option_type=record["instrument_type"]
                    if instrument_type in (InstrumentType.OPTION_CE, InstrumentType.OPTION_PE)
                    else None,
                    exchange=record.get("exchange", "NSE").strip().upper(),
                    exchange_token=record["exchange_token"].strip(),
                    lot_size=lot_size,
                    tick_size=tick_size,
                )
            )
        return instruments

    def get_instrument(self, symbol: str) -> Instrument | None:
        for instrument in self.get_instruments():
            if instrument.symbol == symbol:
                return instrument
        return None

    # --------------------------------------------------------------- quotes

    def _quote_key(self, instrument: Instrument) -> str:
        if not instrument.exchange_token:
            raise InstrumentNotFoundError(
                f"'{instrument.symbol}' has no exchange_token; Kite needs one to fetch data"
            )
        return f"{instrument.exchange}:{instrument.exchange_token}"

    @staticmethod
    def _single_quote(payload: dict[str, Any], key: str) -> dict[str, Any]:
        data = payload.get("data")
        if not isinstance(data, dict) or key not in data:
            raise InstrumentNotFoundError(f"Kite returned no quote for '{key}'")
        return data[key]

    def get_last_price(self, instrument: Instrument) -> Decimal:
        key = self._quote_key(instrument)
        response = self._get("/quote/ltp", params={"i": key})
        block = self._single_quote(response.json, key)
        return Decimal(str(block.get("last_price", "0")))

    def get_quote(self, instrument: Instrument) -> MarketQuote:
        key = self._quote_key(instrument)
        response = self._get("/quote", params={"i": key})
        block = self._single_quote(response.json, key)
        return self._quote_from_block(block, instrument)

    def get_market_price(self, instrument: Instrument) -> MarketPrice:
        key = self._quote_key(instrument)
        response = self._get("/quote", params={"i": key})
        block = self._single_quote(response.json, key)
        return self._price_from_block(block, instrument)

    def _quote_from_block(self, block: dict[str, Any], instrument: Instrument) -> MarketQuote:
        ohlc = block.get("ohlc") or {}
        depth = block.get("depth") or {}
        buy = (depth.get("buy") or [{}])[0]
        sell = (depth.get("sell") or [{}])[0]
        previous_close = self._decimal(ohlc.get("close"))
        last_price = self._decimal(block.get("last_price"), zero_if_missing=True)
        change = (
            last_price - previous_close
            if previous_close is not None
            else None
        )
        change_percent = (change / previous_close * 100) if (change is not None and previous_close) else None

        return MarketQuote(
            instrument=instrument,
            timestamp=self._timestamp(block.get("timestamp")) or self._now_fn(),
            last_price=last_price,
            open=self._decimal(ohlc.get("open")),
            high=self._decimal(ohlc.get("high")),
            low=self._decimal(ohlc.get("low")),
            previous_close=previous_close,
            volume=int(block.get("volume") or 0),
            open_interest=int(block.get("oi")) if block.get("oi") is not None else None,
            bid=self._decimal(buy.get("price")),
            ask=self._decimal(sell.get("price")),
            change=change,
            change_percent=change_percent,
        )

    def _price_from_block(self, block: dict[str, Any], instrument: Instrument) -> MarketPrice:
        quote = self._quote_from_block(block, instrument)
        return MarketPrice(
            instrument=instrument,
            timestamp=quote.timestamp,
            open=quote.open if quote.open is not None else quote.last_price,
            high=quote.high if quote.high is not None else quote.last_price,
            low=quote.low if quote.low is not None else quote.last_price,
            close=quote.last_price,
            volume=quote.volume,
            open_interest=quote.open_interest,
        )

    # ------------------------------------------------------------------ session

    def get_market_session(self) -> MarketSession:
        now = self._now_fn()
        response = self._get("/market/status")
        payload = response.json
        exchange_status = ((payload.get("data") or {}).get("exchange_status")) or {}
        nse = exchange_status.get("NSE") or {}
        trading = bool(nse.get("trading"))
        if not nse and exchange_status:
            trading = any(segment.get("trading") for segment in exchange_status.values() if segment)

        base = market_session(now, label="NSE via Kite Connect")
        return MarketSession(
            is_open=trading,
            phase=MarketPhase.OPEN if trading else MarketPhase.CLOSED,
            observed_at=base.observed_at,
            open_time=base.open_time,
            close_time=base.close_time,
            exchange="NSE",
            label=base.label,
        )

    # --------------------------------------------------------------- history

    def _historical(
        self,
        instrument: Instrument,
        interval: str,
        start: datetime,
        end: datetime,
    ) -> list[MarketPrice]:
        if not instrument.exchange_token:
            raise InstrumentNotFoundError(
                f"'{instrument.symbol}' has no exchange_token; Kite needs one for historical data"
            )
        interval = interval.lower()
        if interval not in KITE_INTERVALS:
            raise MarketDataError(
                f"unsupported Kite interval '{interval}'; expected one of {sorted(KITE_INTERVALS)}"
            )
        start_iso = start.strftime("%Y-%m-%d")
        end_iso = end.strftime("%Y-%m-%d")
        path = f"/instruments/historical/{instrument.exchange_token}/{interval}"
        response = self._get(path, params={"from": start_iso, "to": end_iso})
        candles = ((response.json.get("data") or {}).get("candles")) or []
        bars: list[MarketPrice] = []
        for candle in candles:
            if len(candle) < 6:
                continue
            ts = self._epoch_to_naive(candle[0])
            volume = int(candle[5] or 0)
            oi = int(candle[6]) if len(candle) > 6 and candle[6] is not None else None
            bars.append(
                MarketPrice(
                    instrument=instrument,
                    timestamp=ts,
                    open=self._decimal(candle[1]),
                    high=self._decimal(candle[2]),
                    low=self._decimal(candle[3]),
                    close=self._decimal(candle[4]),
                    volume=volume,
                    open_interest=oi,
                )
            )
        return bars

    def get_ohlcv(self, instrument: Instrument, limit: int | None = None) -> list[MarketPrice]:
        now = self._now_fn()
        start = now - timedelta(days=366)
        bars = self._historical(instrument, "day", start, now)
        if limit is not None:
            if limit < 0:
                raise ValueError("limit must be >= 0")
            if limit == 0:
                return []
            return bars[-limit:]
        return bars

    def get_historical_ohlcv(
        self,
        instrument: Instrument,
        interval: str = "day",
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[MarketPrice]:
        now = self._now_fn()
        start = start if start is not None else now - timedelta(days=366)
        end = end if end is not None else now
        return self._historical(instrument, interval, start, end)

    # --------------------------------------------------------------- helpers

    @staticmethod
    def _decimal(value: Any, zero_if_missing: bool = False) -> Decimal | None:
        if value is None:
            return None
        try:
            result = Decimal(str(value))
        except (InvalidOperation, ValueError):
            return None
        if not result.is_finite():
            return None
        return result if result >= 0 or zero_if_missing else None

    @staticmethod
    def _timestamp(value: Any) -> datetime | None:
        if value in (None, ""):
            return None
        if isinstance(value, (int, float)):
            return KiteConnectProvider._epoch_to_naive(value)
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None
        # Normalise to the market timezone as a naive datetime.
        return parsed.astimezone(NSE_TZ).replace(tzinfo=None)

    @staticmethod
    def _epoch_to_naive(value: float) -> datetime:
        epoch = float(value)
        # Kite v3 returns seconds, but tolerate milliseconds.
        if abs(epoch) > 1e12:
            epoch = epoch / 1000.0
        return datetime.fromtimestamp(epoch, tz=NSE_TZ).replace(tzinfo=None)