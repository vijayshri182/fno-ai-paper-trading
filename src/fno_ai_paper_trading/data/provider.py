"""Market-data provider abstraction.

Phase 1 ships only the interface and an in-memory implementation for tests and
demos. Live market-data feeds (e.g. broker/API sourced) can be added in a later
phase by implementing this protocol; no other component needs to change.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from decimal import Decimal

from fno_ai_paper_trading.models.instruments import Instrument
from fno_ai_paper_trading.models.market import MarketPrice


class MarketDataProvider(ABC):
    """Contract any market-data source must satisfy."""

    @abstractmethod
    def get_instruments(self) -> list[Instrument]:
        """Return the list of instruments this provider knows about."""

    @abstractmethod
    def get_market_price(self, instrument: Instrument) -> MarketPrice:
        """Return the latest OHLCV bar for ``instrument``."""

    @abstractmethod
    def get_last_price(self, instrument: Instrument) -> Decimal:
        """Return the most recent close price for ``instrument``."""

    @abstractmethod
    def get_ohlcv(self, instrument: Instrument, limit: int | None = None) -> list[MarketPrice]:
        """Return OHLCV bars for ``instrument``, most recent first if ``limit`` given."""