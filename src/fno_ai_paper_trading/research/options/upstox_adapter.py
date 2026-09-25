"""Upstox V2 option-chain adapter implementing the Phase 5 data boundary.

Read-only adapter. Sources:

* ``GET /v2/option/contract``  -> option contracts (expiry resolution).
* ``GET /v2/option/chain``     -> one expiry's full strike x CE/PE matrix with
  market data, spot and option greeks.

Mappings (all provider-neutral output is built through the Phase 5 stack):

* ``call_options``/``put_options``  -> :class:`OptionSide` CE/PE
* ``strike_price``/``expiry``       -> contract identity
* ``market_data.ltp``/``bid_price``/``ask_price``/``oi``/``volume`` -> prices/OI
* ``oi - prev_oi``                  -> ``oi_change`` (derived, documented)
* ``option_greeks.{delta,gamma,theta,vega,iv}`` -> greeks (``rho`` in the model
  stays ``None`` = not supplied by Upstox)
* ``underlying_spot_price``         -> snapshot spot (never substituted)

The Upstox chain payload contains **no per-quote timestamp**; the adapter
stamps every row with the receive instant (``now_fn``) and records it as the
provider receive time. Fields Upstox does not publish stay ``None`` and surface
as ``UNAVAILABLE`` through the Phase 5 validator — nothing is fabricated and
nothing is converted into zero.

Authentication: data-layer OAuth2 bearer token (``FNO_UPSTOX_ACCESS_TOKEN``),
caller-supplied, exactly like :class:`UpstoxHistoricalDataProvider`. No
``x-api-key`` is required for these option endpoints (verified live, Bearer
only). This module is read-only research tooling: it imports no execution code,
never places orders and never touches credentials beyond what the caller
injects.
"""
from __future__ import annotations

import time
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Callable, Mapping

from fno_ai_paper_trading.data.errors import (
    AuthenticationError,
    MarketDataError,
    ProviderConfigurationError,
    RateLimitError,
    UnavailableError,
)
from fno_ai_paper_trading.data.instrument_registry import get_research_instrument
from fno_ai_paper_trading.data.market_hours import NSE_TZ
from fno_ai_paper_trading.models.instruments import Instrument
from fno_ai_paper_trading.research.options.errors import OptionDataError
from fno_ai_paper_trading.research.options.models import OptionChainSnapshot, OptionSide
from fno_ai_paper_trading.research.options.normalization import normalize_chain
from fno_ai_paper_trading.research.options.protocol import OptionChainProvider
from fno_ai_paper_trading.utils.http import HttpError, http_get
from fno_ai_paper_trading.utils.retry import RetryExhausted, describe_last_error, retry_call

UPSTOX_BASE_URL = "https://api.upstox.com"
UPSTOX_PROVIDER_ID = "UPSTOX_V2_OPTION_CHAIN"

_SIDE_KEYS = (("call_options", OptionSide.CE), ("put_options", OptionSide.PE))
_SYMBOL_ALIASES = {
    "NIFTY": "NSE_INDEX|Nifty 50",
    "NIFTY 50": "NSE_INDEX|Nifty 50",
    "BANKNIFTY": "NSE_INDEX|Nifty Bank",
    "NIFTY BANK": "NSE_INDEX|Nifty Bank",
    "FINNIFTY": "NSE_INDEX|Nifty Financial Services",
    "NIFTY FINANCIAL SERVICES": "NSE_INDEX|Nifty Financial Services",
}


class _Retryable(UnavailableError):
    """Internal marker: a transient attempt that may be retried."""


def _to_naive_ist(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(NSE_TZ).replace(tzinfo=None)


def _norm_int(value: Any) -> int | None:
    """Coerce an integral number to int; anything fractional/None becomes None.

    Upstox delivers counts (OI, volume) as JSON floats; a non-integral value is
    not a valid count and is treated as unavailable rather than fabricated.
    """
    if value is None:
        return None
    try:
        number = value if isinstance(value, int) else int(value)
        if float(value) != number:
            return None
        return number
    except (TypeError, ValueError, OverflowError):
        return None


def resolve_upstox_underlying_key(underlying: Instrument) -> str:
    """Resolve the Upstox ``SEGMENT|SYMBOL`` instrument key for the underlying.

    Preferred source is ``underlying.exchange_token`` (the ``NSE_INDEX|Nifty 50``
    style key); curated names fall back to the instrument registry, then the
    documented symbol aliases. Raises :class:`OptionDataError` when unresolvable.
    """
    token = (underlying.exchange_token or "").strip()
    if token and "|" in token:
        return token
    name = (underlying.underlying_symbol or underlying.symbol or "").strip()
    try:
        return get_research_instrument(name).exchange_token  # type: ignore[return-value]
    except KeyError:
        pass
    normalized = " ".join(name.upper().split())
    if normalized in _SYMBOL_ALIASES:
        return _SYMBOL_ALIASES[normalized]
    raise OptionDataError(
        f"cannot resolve an Upstox instrument key for {underlying.symbol!r}; "
        f"set Instrument.exchange_token to a SEGMENT|SYMBOL key (e.g. NSE_INDEX|Nifty 50)"
    )


class UpstoxOptionChainProvider(OptionChainProvider):
    """Read-only Upstox V2 option-chain source for the Phase 5 pipeline.

    Args:
        access_token: data-layer OAuth2 bearer token (caller-supplied; never
            read from the environment here, mirroring the historical provider).
        base_url: Overridable for tests.
        timeout_seconds: per-request timeout.
        max_retries: additional attempts after the first (0 = no retries).
        retry_delay: initial backoff.
        now_fn: "now" callable (injectable for deterministic tests); used as
            the snapshot receive instant because Upstox sends no quote
            timestamp.
        http_fn: transport callable (injectable for offline tests).
    """

    provider_id: str = UPSTOX_PROVIDER_ID

    def __init__(
        self,
        access_token: str,
        *,
        base_url: str = UPSTOX_BASE_URL,
        timeout_seconds: float = 10.0,
        max_retries: int = 3,
        retry_delay: float = 0.1,
        now_fn: Callable[[], datetime] = datetime.now,
        http_fn: Callable[..., Any] = http_get,
    ) -> None:
        self.access_token = (access_token or "").strip()
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = float(timeout_seconds)
        self.max_retries = int(max_retries)
        self.retry_delay = float(retry_delay)
        self._now_fn = now_fn
        self._http_fn = http_fn

    # -------------------------------------------------------------- transport

    def _headers(self) -> dict[str, str]:
        if not self.access_token:
            raise ProviderConfigurationError(
                "Upstox option-chain provider requires an access token "
                "(set FNO_UPSTOX_ACCESS_TOKEN in .env)"
            )
        return {"Authorization": f"Bearer {self.access_token}", "Accept": "application/json"}

    @staticmethod
    def _error_text(exc: Exception) -> str:
        body = getattr(exc, "text", "") or str(getattr(exc, "body", b""))
        return str(body)[:200]

    def _get(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        url = f"{self.base_url}{path}"

        def attempt() -> Any:
            headers = self._headers()
            try:
                response = self._http_fn(url, params=params if params else None, headers=headers, timeout=self.timeout_seconds)
                return response.json
            except HttpError as exc:
                if exc.status == 401:
                    raise AuthenticationError(
                        f"Upstox rejected the option-chain access token (HTTP 401): {self._error_text(exc)}"
                    ) from exc
                if exc.status == 404:
                    raise OptionDataError(f"Upstox returned 404 for {exc.url}: {self._error_text(exc)}") from exc
                if exc.status == 429 or 500 <= exc.status <= 599:
                    raise _Retryable(f"transient HTTP {exc.status} for {exc.url}") from exc
                raise MarketDataError(
                    f"unexpected HTTP {exc.status} for {exc.url}: {self._error_text(exc)}"
                ) from exc
            except OSError as exc:
                raise _Retryable(f"transport failure for {url}: {exc}") from exc

        try:
            return retry_call(
                attempt,
                attempts=self.max_retries + 1,
                delay=self.retry_delay,
                backoff=2.0,
                max_delay=self.retry_delay * 8,
                exceptions=(_Retryable,),
                sleep=time.sleep,
            )
        except RetryExhausted as exc:
            cause = describe_last_error(exc)
            if isinstance(cause, _Retryable) and str(cause).startswith("transient HTTP 429"):
                raise RateLimitError("Upstox option-chain rate limit hit (429) and retries exhausted") from exc
            raise UnavailableError("Upstox option-chain service unavailable after retries") from exc

    # -------------------------------------------------------------- universe

    def _underlying_key(self, underlying: Instrument) -> str:
        return resolve_upstox_underlying_key(underlying)

    def expiries(self, underlying: Instrument) -> tuple[date, ...]:
        """Deterministic, ascending option expiries via ``/v2/option/contract``."""
        payload = self._get("/v2/option/contract", {"instrument_key": self._underlying_key(underlying)})
        rows = payload.get("data") if isinstance(payload, Mapping) else None
        if not isinstance(rows, list):
            raise OptionDataError("Upstox /v2/option/contract returned no contract list")
        seen: dict[str, date] = {}
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            raw_expiry = row.get("expiry")
            if not raw_expiry:
                continue
            try:
                seen[raw_expiry] = date.fromisoformat(str(raw_expiry))
            except ValueError:
                continue
        return tuple(sorted(seen.values()))

    def _nearest_expiry(self, underlying: Instrument) -> date:
        today = _to_naive_ist(self._now_fn()).date()
        expiries = self.expiries(underlying)
        if not expiries:
            raise OptionDataError("Upstox returned no option expiries for the underlying")
        upcoming = [exp for exp in expiries if exp >= today]
        return upcoming[0] if upcoming else expiries[0]

    # ---------------------------------------------------------- chain mapping

    @staticmethod
    def _row_to_raw(
        record: Mapping[str, Any],
        *,
        receive_at: datetime,
        provider_id: str,
    ) -> list[dict[str, Any]]:
        expiry = str(record.get("expiry") or "")
        strike = str(record.get("strike_price") or "")
        raw_rows: list[dict[str, Any]] = []
        for node_key, side in _SIDE_KEYS:
            node = record.get(node_key)
            if not isinstance(node, Mapping):
                continue
            market = node.get("market_data") if isinstance(node.get("market_data"), Mapping) else {}
            greeks = node.get("option_greeks") if isinstance(node.get("option_greeks"), Mapping) else {}
            oi = _norm_int(market.get("oi"))
            prev_oi = _norm_int(market.get("prev_oi"))
            oi_change = (oi - prev_oi) if (oi is not None and prev_oi is not None) else None
            raw_rows.append(
                {
                    "instrument_key": node.get("instrument_key"),
                    "symbol": None,
                    "expiry": expiry,
                    "strike": strike,
                    "option_type": side.value,
                    "timestamp": receive_at.isoformat(),
                    "bid": market.get("bid_price"),
                    "ask": market.get("ask_price"),
                    "last_price": market.get("ltp"),
                    "oi": oi,
                    "oi_change": oi_change,
                    "volume": _norm_int(market.get("volume")),
                    "iv": greeks.get("iv"),
                    "greeks": {
                        "delta": greeks.get("delta"),
                        "gamma": greeks.get("gamma"),
                        "theta": greeks.get("theta"),
                        "vega": greeks.get("vega"),
                    },
                    "source": provider_id,
                    "source_timestamp": receive_at.isoformat(),
                }
            )
        return raw_rows

    def fetch_chain(
        self,
        underlying: Instrument,
        expiry: date | None = None,
        *,
        timestamp: datetime | None = None,
    ) -> OptionChainSnapshot:
        """Fetch and normalize one Upstox expiry's full option chain.

        No order API is ever touched. The upcoming expiry is used when
        ``expiry`` is ``None``. ``timestamp`` forces the receive instant
        (deterministic tests); otherwise ``now_fn`` is used.
        """
        underlying_key = self._underlying_key(underlying)
        receive_at = _to_naive_ist(timestamp or self._now_fn())
        resolved = expiry or self._nearest_expiry(underlying)

        payload = self._get(
            "/v2/option/chain",
            {"instrument_key": underlying_key, "expiry_date": resolved.isoformat()},
        )
        rows = payload.get("data") if isinstance(payload, Mapping) else None
        if not isinstance(rows, list):
            raise OptionDataError("Upstox /v2/option/chain returned no chain list")

        raw_rows: list[dict[str, Any]] = []
        spot: Any = None
        for index, record in enumerate(rows):
            if not isinstance(record, Mapping):
                continue
            raw_rows.extend(self._row_to_raw(record, receive_at=receive_at, provider_id=self.provider_id))
            if spot is None:
                spot = record.get("underlying_spot_price")

        if not raw_rows:
            raise OptionDataError(
                f"Upstox returned an empty chain for {underlying_key} / {resolved.isoformat()}"
            )

        chain_payload: dict[str, Any] = {
            "expiry": resolved.isoformat(),
            "timestamp": receive_at.isoformat(),
            "spot_price": spot,
            "source": self.provider_id,
            "quotes": raw_rows,
        }
        return normalize_chain(chain_payload, underlying=underlying, source=self.provider_id)

    def supports(self, underlying: Instrument, expiry: date | None = None) -> bool:
        if underlying.is_option():
            return False
        try:
            self._underlying_key(underlying)
            return True
        except OptionDataError:
            return False


__all__ = [
    "UPSTOX_PROVIDER_ID",
    "UpstoxOptionChainProvider",
    "resolve_upstox_underlying_key",
]