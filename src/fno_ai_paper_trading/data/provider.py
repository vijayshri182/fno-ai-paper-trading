"""Market-data provider abstraction.

Phase 1 shipped only the interface and an in-memory implementation. Phase 2
adds quote/session/historical contracts and a real vendor adapter
(:class:`~fno_ai_paper_trading.data.kite_provider.KiteConnectProvider`) that
implements this protocol. No other component depends on a specific vendor.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from decimal import Decimal

from fno_ai_paper_trading.models.instruments import Instrument
from fno_ai_paper_trading.models.market import MarketPrice, MarketQuote, MarketSession


class MarketDataProvider(ABC):
    """Contract any market-data source must satisfy."""

    @abstractmethod
    def get_instruments(self) -> list[Instrument]:
        """Return the list of instruments this provider knows about."""

    @abstractmethod
    def get_instrument(self, symbol: str) -> Instrument | None:
        """Return the instrument with ``symbol``, or ``None`` if unknown."""

    @abstractmethod
    def get_market_price(self, instrument: Instrument) -> MarketPrice:
        """Return the latest OHLCV bar for ``instrument``."""

    @abstractmethod
    def get_last_price(self, instrument: Instrument) -> Decimal:
        """Return the most recent close price for ``instrument``."""

    @abstractmethod
    def get_quote(self, instrument: Instrument) -> MarketQuote:
        """Return a point-in-time quote (LTP plus context) for ``instrument``."""

    @abstractmethod
    def get_market_session(self) -> MarketSession:
        """Return the normalized current market session state."""

    @abstractmethod
    def get_ohlcv(self, instrument: Instrument, limit: int | None = None) -> list[MarketPrice]:
        """Return OHLCV bars for ``instrument``.

        Bars are chronological (oldest first). If ``limit`` is given, only the
        ``limit`` most recent bars are returned.
        """

    @abstractmethod
    def get_historical_ohlcv(
        self,
        instrument: Instrument,
        interval: str = "day",
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[MarketPrice]:
        """Return OHLCV bars in the given window.

        ``interval`` is a provider-specific bucket label (``"day"``, ``"minute"``,
        etc.) normalized by each provider. Bars are returned oldest-first.
        """