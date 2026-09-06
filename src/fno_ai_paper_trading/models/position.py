"""Position and Trade models for tracking open positions and their transactions."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation

from fno_ai_paper_trading.models.enums import OrderSide
from fno_ai_paper_trading.models.instruments import Instrument
from fno_ai_paper_trading.utils.functions import (
    non_negative_decimal,
    positive_decimal,
    positive_int,
    to_decimal,
)


@dataclass
class Position:
    """An open position in a single instrument.

    ``quantity`` is signed: positive = long, negative = short.
    """

    instrument: Instrument
    quantity: int
    average_entry_price: Decimal
    realized_pnl: Decimal = Decimal("0")
    opened_at: datetime = field(default_factory=datetime.now)

    def __post_init__(self) -> None:
        if self.quantity == 0:
            raise ValueError("a Position must have non-zero quantity")
        self.average_entry_price = non_negative_decimal(self.average_entry_price, "average_entry_price")
        self.realized_pnl = non_negative_decimal(self.realized_pnl, "realized_pnl")

    @property
    def is_long(self) -> bool:
        return self.quantity > 0

    @property
    def is_short(self) -> bool:
        return self.quantity < 0

    @property
    def is_flat(self) -> bool:
        return self.quantity == 0

    def unrealized_pnl(self, market_price: Decimal) -> Decimal:
        """Unrealized P&L given the current market price (signed, uses multiplier)."""
        direction = 1 if self.is_long else -1
        return direction * (market_price - self.average_entry_price) * abs(self.quantity) * self.instrument.multiplier

    def market_value(self, market_price: Decimal) -> Decimal:
        return self.quantity * market_price * self.instrument.multiplier


@dataclass(frozen=True)
class Trade:
    """A completed transaction that moves portfolio risk (opening or closing)."""

    trade_id: str
    instrument: Instrument
    side: OrderSide
    quantity: int  # absolute quantity transacted
    price: Decimal
    commission: Decimal
    executed_at: datetime = field(default_factory=datetime.now)
    realized_pnl: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        if not self.trade_id.strip():
            raise ValueError("trade requires a trade_id")
        try:
            realized_pnl = to_decimal(self.realized_pnl)
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError("realized_pnl must be a number") from exc
        if not realized_pnl.is_finite():
            raise ValueError("realized_pnl must be finite")
        object.__setattr__(self, "quantity", positive_int(self.quantity, "quantity"))
        object.__setattr__(self, "price", positive_decimal(self.price, "price"))
        object.__setattr__(self, "commission", non_negative_decimal(self.commission, "commission"))
        object.__setattr__(self, "realized_pnl", realized_pnl)

    @property
    def notional(self) -> Decimal:
        return self.quantity * self.price * self.instrument.multiplier