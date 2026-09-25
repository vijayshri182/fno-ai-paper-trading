"""Narrow historical-data client used by the fresh-OOS collector.

The collector depends only on this narrow interface (one method: fetch one
trading day of 5-minute bars). The real implementation wraps the existing
read-only Upstox REST V3 provider (``GET /v3/historical-candle`` only -- no
execution endpoint is reachable through it). Fakes used in tests implement the
same two methods, so no test ever touches the network.

Credential handling: the analytics/data token comes from the runtime
credential provider (:class:`RuntimeCredentialProvider`), which resolves
``FNO_UPSTOX_ACCESS_TOKEN`` from the process environment *only when a fetch is
attempted* and returns it to the provider in memory -- never persisted,
printed, logged or placed in any CLI argument. An explicit ``access_token``
may also be injected in memory for tests. When no token is available the
fetch raises :class:`CredentialsUnavailableError`, which the collector records
as ``AUTH_REQUIRED`` -- the credential-gated exit described by the protocol.
"""
from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone as _utc
from typing import Callable, Protocol, runtime_checkable

from fno_ai_paper_trading.data.upstox_provider import (
    UPSTOX_BASE_URL,
    UpstoxHistoricalDataProvider,
)
from fno_ai_paper_trading.fresh_oos.credential_provider import (  # noqa: F401
    CREDENTIAL_ENV,
    RuntimeCredentialProvider,
)
from fno_ai_paper_trading.fresh_oos.protocol import (
    INTERVAL,
    SESSION_END_EXCLUSIVE,
    SESSION_FIRST_TIME,
)
from fno_ai_paper_trading.models.instruments import Instrument
from fno_ai_paper_trading.models.market import MarketPrice


@runtime_checkable
class HistoricalDataClient(Protocol):
    """The single data seam the collector talks to (implemented by fakes)."""

    def fetch_5m_day(self, instrument: Instrument, day: date) -> list[MarketPrice]:
        """Return the fully-replayed 09:15..15:30 window for ``day`` (naive IST)."""


class UpstoxHistoricalDataClient:
    """READ-ONLY historical-data adapter for the fresh-OOS collector.

    Wraps :class:`UpstoxHistoricalDataProvider` (``GET`` historical candles
    only). Constructing this class performs no network activity; the provider is
    built lazily on the first fetch so tests / status checks never need
    credentials.
    """

    def __init__(
        self,
        access_token: str = "",
        *,
        credential_provider: RuntimeCredentialProvider | None = None,
        base_url: str = UPSTOX_BASE_URL,
        timeout_seconds: float = 10.0,
        max_retries: int = 3,
        retry_delay: float = 0.1,
        sleep: Callable[[float], None] = time.sleep,
        now_fn: Callable[[], datetime] = datetime.now,
        provider: UpstoxHistoricalDataProvider | None = None,
    ) -> None:
        self.access_token = (access_token or "").strip()
        self._credentials = credential_provider or RuntimeCredentialProvider()
        self.base_url = (base_url or UPSTOX_BASE_URL).rstrip("/")
        self.timeout_seconds = float(timeout_seconds)
        self.max_retries = int(max_retries)
        self.retry_delay = float(retry_delay)
        self._sleep = sleep
        self._now_fn = now_fn
        self._injected = provider

    def _provider(self) -> UpstoxHistoricalDataProvider:
        token = self.access_token
        if not token:
            token = self._credentials.resolve()  # fail-closed AUTH_REQUIRED when absent
        if self._injected is not None:
            return self._injected
        return UpstoxHistoricalDataProvider(
            access_token=token,
            base_url=self.base_url,
            timeout_seconds=self.timeout_seconds,
            max_retries=self.max_retries,
            retry_delay=self.retry_delay,
            sleep=self._sleep,
            now_fn=self._now_fn,
            interval=INTERVAL,
            default_days=2,
        )

    def redact_error_text(self, text: str) -> str:
        """Strip any occurrence of the resolved token from ``text`` (no-op when absent)."""
        return self._credentials.redact_text(text)

    def fetch_5m_day(self, instrument: Instrument, day: date) -> list[MarketPrice]:
        """Fetch one trading day as 5-minute bars (naive IST, chronological).

        The request window covers the full NSE derivative session
        (``SESSION_FIRST_TIME`` .. ``SESSION_END_EXCLUSIVE``). Provider errors
        (typed ``data.errors`` subclasses) propagate untouched to the collector
        which maps them to run statuses.
        """
        start = datetime(day.year, day.month, day.day, SESSION_FIRST_TIME.hour, SESSION_FIRST_TIME.minute)
        end = datetime(
            day.year,
            day.month,
            day.day,
            SESSION_END_EXCLUSIVE.hour,
            SESSION_END_EXCLUSIVE.minute,
        )
        return self._provider().get_historical_ohlcv(instrument, INTERVAL, start, end)

    def fetch_intraday_day(self, instrument: Instrument) -> list[MarketPrice]:
        """Fetch the current trading day's 5-minute bars via **Intraday V3**.

        This is the dedicated current-day endpoint (the dated Historical
        endpoint returns zero candles for the still-open trading day). There is
        deliberately no Intraday -> Historical fallback: callers treat an empty
        result as current-day data unavailable (NOT_READY / fail closed).
        Provider errors (typed ``data.errors`` subclasses) propagate untouched.
        """
        return self._provider().get_intraday_ohlcv(instrument, INTERVAL)