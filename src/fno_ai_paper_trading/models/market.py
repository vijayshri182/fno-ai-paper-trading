"""Market data models (OHLCV bars and quotes)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from fno_ai_paper_trading.models.instruments import Instrument
from fno_ai_paper_trading.utils.functions import non_negative_decimal, non_negative_int


@dataclass(frozen=True)
class MarketPrice:
    """A single OHLCV bar. ``timestamp`` is the bar open time."""

    instrument: Instrument
    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int = 0

    def __post_init__(self) -> None:
        open_ = non_negative_decimal(self.open, "open")
        close = non_negative_decimal(self.close, "close")
        high = non_negative_decimal(self.high, "high")
        low = non_negative_decimal(self.low, "low")
        volume = non_negative_int(self.volume, "volume")

        if high < open_ or high < close:
            raise ValueError("high must be >= open and close")
        if low > open_ or low > close:
            raise ValueError("low must be <= open and close")

        object.__setattr__(self, "open", open_)
        object.__setattr__(self, "high", high)
        object.__setattr__(self, "low", low)
        object.__setattr__(self, "close", close)
        object.__setattr__(self, "volume", volume)