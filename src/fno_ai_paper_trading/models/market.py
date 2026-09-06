"""Market data models (OHLCV bars and quotes)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from fno_ai_paper_trading.models.enums import MarketPhase
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
    open_interest: int | None = None

    def __post_init__(self) -> None:
        open_ = non_negative_decimal(self.open, "open")
        close = non_negative_decimal(self.close, "close")
        high = non_negative_decimal(self.high, "high")
        low = non_negative_decimal(self.low, "low")
        volume = non_negative_int(self.volume, "volume")
        open_interest = (
            non_negative_int(self.open_interest, "open_interest") if self.open_interest is not None else None
        )

        if high < open_ or high < close:
            raise ValueError("high must be >= open and close")
        if low > open_ or low > close:
            raise ValueError("low must be <= open and close")

        object.__setattr__(self, "open", open_)
        object.__setattr__(self, "high", high)
        object.__setattr__(self, "low", low)
        object.__setattr__(self, "close", close)
        object.__setattr__(self, "volume", volume)
        object.__setattr__(self, "open_interest", open_interest)

    @property
    def mid(self) -> Decimal:
        """Midpoint of the bar's high/low range (used for deterministic quotes)."""
        return (self.high + self.low) / 2


@dataclass(frozen=True)
class MarketQuote:
    """A point-in-time quote (LTP plus optional depth/OHLC context)."""

    instrument: Instrument
    timestamp: datetime
    last_price: Decimal
    open: Decimal | None = None
    high: Decimal | None = None
    low: Decimal | None = None
    previous_close: Decimal | None = None
    volume: int = 0
    open_interest: int | None = None
    bid: Decimal | None = None
    ask: Decimal | None = None
    change: Decimal | None = None
    change_percent: Decimal | None = None

    def __post_init__(self) -> None:
        last_price = non_negative_decimal(self.last_price, "last_price")
        volume = non_negative_int(self.volume, "volume")
        open_interest = (
            non_negative_int(self.open_interest, "open_interest") if self.open_interest is not None else None
        )
        open_ = option_decimal(self.open, "open")
        high = option_decimal(self.high, "high")
        low = option_decimal(self.low, "low")
        previous_close = option_decimal(self.previous_close, "previous_close")
        bid = option_decimal(self.bid, "bid")
        ask = option_decimal(self.ask, "ask")

        if high is not None and low is not None and high < low:
            raise ValueError("high must be >= low")
        if bid is not None and ask is not None and bid > ask:
            raise ValueError("bid must be <= ask")

        for attr, value in (
            ("last_price", last_price),
            ("open", open_),
            ("high", high),
            ("low", low),
            ("previous_close", previous_close),
            ("bid", bid),
            ("ask", ask),
        ):
            object.__setattr__(self, attr, value)
        object.__setattr__(self, "volume", volume)
        object.__setattr__(self, "open_interest", open_interest)

    @property
    def change_abs(self) -> Decimal | None:
        """Absolute change vs previous close, if both are known."""
        if self.previous_close is None:
            return None
        return self.last_price - self.previous_close


@dataclass(frozen=True)
class MarketSession:
    """Normalized market status/hours for a single session."""

    is_open: bool
    phase: MarketPhase
    observed_at: datetime
    open_time: datetime | None = None
    close_time: datetime | None = None
    exchange: str = "NSE"
    label: str = "session"

    def __post_init__(self) -> None:
        if not self.exchange.strip():
            raise ValueError("exchange must not be empty")
        if not self.label.strip():
            raise ValueError("label must not be empty")
        if self.open_time is not None and self.close_time is not None and self.open_time > self.close_time:
            raise ValueError("open_time must be <= close_time")
        object.__setattr__(self, "exchange", self.exchange.strip().upper())
        object.__setattr__(self, "label", self.label.strip())


def option_decimal(value: Decimal | None, name: str) -> Decimal | None:
    """Validate an optional Decimal: must be non-negative and finite if present."""
    if value is None:
        return None
    return non_negative_decimal(value, name)