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

#: Deterministic order lifecycle. Each status maps to the set of statuses it may
#: transition to. Missing statuses are terminal; any other transition is invalid
#: and raises instead of silently corrupting order state.
ORDER_TRANSITIONS: dict[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.PENDING: frozenset(
        {
            OrderStatus.SUBMITTED,
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED,
        }
    ),
    OrderStatus.SUBMITTED: frozenset(
        {
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED,
        }
    ),
    OrderStatus.PARTIALLY_FILLED: frozenset(
        {
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
        }
    ),
    OrderStatus.FILLED: frozenset(),
    OrderStatus.CANCELLED: frozenset(),
    OrderStatus.REJECTED: frozenset(),
}


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

    def transition(self, new_status: OrderStatus) -> "Order":
        """Move ``status`` deterministically to ``new_status``.

        Valid transitions come from :data:`ORDER_TRANSITIONS`. Integrity rules:
        a ``REJECTED`` order needs a ``rejection_reason``, a
        ``PARTIALLY_FILLED`` order needs ``0 < filled_quantity < quantity``, and
        a ``FILLED`` order needs ``filled_quantity == quantity`` with a positive
        ``average_fill_price``. Invalid transitions raise ``ValueError`` and
        leave the order unchanged.
        """
        if not isinstance(new_status, OrderStatus):
            raise TypeError(f"new_status must be an OrderStatus, got {type(new_status).__name__}")
        if new_status not in ORDER_TRANSITIONS[self.status]:
            raise ValueError(
                f"invalid order status transition: {self.status.value} -> {new_status.value}"
            )
        if new_status is OrderStatus.PARTIALLY_FILLED:
            if not (0 < self.filled_quantity < self.quantity):
                raise ValueError("PARTIALLY_FILLED requires a partial (positive, below total) filled_quantity")
        if new_status is OrderStatus.FILLED:
            if self.filled_quantity != self.quantity:
                raise ValueError("FILLED requires filled_quantity == quantity")
            if self.average_fill_price <= 0:
                raise ValueError("FILLED requires a positive average_fill_price")
        if new_status is OrderStatus.REJECTED and not (self.rejection_reason or "").strip():
            raise ValueError("REJECTED requires a rejection reason")
        self.status = new_status
        return self

    def submit(self) -> "Order":
        """Transition PENDING -> SUBMITTED."""
        return self.transition(OrderStatus.SUBMITTED)

    def reject(self, reason: str) -> "Order":
        """Reject the order with ``reason`` (PENDING/SUBMITTED -> REJECTED)."""
        self.rejection_reason = reason
        return self.transition(OrderStatus.REJECTED)

    def cancel(self) -> "Order":
        """Cancel an open order (PENDING/SUBMITTED/PARTIALLY_FILLED -> CANCELLED)."""
        return self.transition(OrderStatus.CANCELLED)

    def mark_filled(self, average_fill_price: int | float | str | Decimal, filled_quantity: int | None = None) -> "Order":
        """Record the fill fields and transition PENDING/SUBMITTED -> FILLED."""
        if self.status is OrderStatus.PENDING:
            self.submit()
        self.average_fill_price = positive_decimal(average_fill_price, "average_fill_price")
        self.filled_quantity = positive_int(
            filled_quantity if filled_quantity is not None else self.quantity, "filled_quantity"
        )
        return self.transition(OrderStatus.FILLED)


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