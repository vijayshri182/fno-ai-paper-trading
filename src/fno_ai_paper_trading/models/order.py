"""Order and Fill models."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from fno_ai_paper_trading.models.enums import OrderSide, OrderStatus, OrderType
from fno_ai_paper_trading.models.instruments import Instrument
from fno_ai_paper_trading.utils.functions import (
    non_negative_decimal,
    positive_decimal,
    positive_int,
)


@dataclass
class Order:
    """An order submitted to (and later filled by) the paper broker."""

    instrument: Instrument
    side: OrderSide
    quantity: int
    order_type: OrderType = OrderType.MARKET
    status: OrderStatus = OrderStatus.PENDING
    created_at: datetime = field(default_factory=datetime.now)
    submitted_at: datetime | None = None
    filled_at: datetime | None = None
    filled_quantity: int = 0
    average_fill_price: Decimal = Decimal("0")
    order_id: str | None = None
    rejection_reason: str | None = None

    def __post_init__(self) -> None:
        self.quantity = positive_int(self.quantity, "quantity")
        self.average_fill_price = non_negative_decimal(self.average_fill_price, "average_fill_price")
        self.filled_quantity = positive_int(self.filled_quantity) if self.filled_quantity else 0

    @property
    def is_filled(self) -> bool:
        return self.status == OrderStatus.FILLED

    @property
    def is_open(self) -> bool:
        return self.status in (OrderStatus.PENDING, OrderStatus.SUBMITTED)

    @property
    def remaining_quantity(self) -> int:
        return self.quantity - self.filled_quantity


@dataclass(frozen=True)
class Fill:
    """A single simulated fill against part or all of an order."""

    order_id: str
    instrument: Instrument
    side: OrderSide
    quantity: int
    price: Decimal
    commission: Decimal
    filled_at: datetime = field(default_factory=datetime.now)

    def __post_init__(self) -> None:
        if not self.order_id.strip():
            raise ValueError("fill requires an order_id")
        object.__setattr__(self, "quantity", positive_int(self.quantity, "quantity"))
        object.__setattr__(self, "price", positive_decimal(self.price, "price"))
        object.__setattr__(self, "commission", non_negative_decimal(self.commission, "commission"))

    @property
    def notional(self) -> Decimal:
        return self.quantity * self.price * self.instrument.multiplier