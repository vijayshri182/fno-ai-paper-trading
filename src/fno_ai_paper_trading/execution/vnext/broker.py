"""Broker abstraction for the vNext layer.

The state machine depends only on this protocol — a minimal option-broker
surface (submit, get status, get position, reconcile). No network
implementation is provided in this phase; deterministic fakes implement it in
the isolated test-suite, and a future real adapter would implement the same
protocol without touching the machine.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from fno_ai_paper_trading.execution.vnext.contract import OptionContract
from fno_ai_paper_trading.execution.vnext.enums import (
    OrderAction,
    OrderStatus,
    TxSide,
)
from fno_ai_paper_trading.execution.vnext.errors import InvalidOrderSemanticsError


@dataclass(frozen=True)
class OrderRequest:
    """One order submission with the fixed (contract, side, action) triple.

    The vNext layer always constructs the exact contract together with the
    transaction side; the broker adapter must never infer the contract from the
    transaction side.
    """

    order_action: OrderAction
    contract: OptionContract
    tx_side: TxSide
    quantity: int

    def __post_init__(self) -> None:
        if isinstance(self.quantity, bool) or not isinstance(self.quantity, int):
            raise InvalidOrderSemanticsError(
                "quantity must be an integer, got "
                f"{type(self.quantity).__name__}"
            )
        if self.quantity <= 0:
            raise InvalidOrderSemanticsError("quantity must be positive")
        if self.order_action is OrderAction.OPEN and self.tx_side is TxSide.SELL:
            raise InvalidOrderSemanticsError(
                "OPEN must be BUY in vNext (no options selling as an opening "
                "transaction)"
            )


@dataclass(frozen=True)
class OrderTicket:
    """Broker acknowledgement of an order submission."""

    order_id: str
    status: OrderStatus


@dataclass(frozen=True)
class OrderStatusRecord:
    """Broker-reported order status."""

    order_id: str
    status: OrderStatus
    message: str = ""


@dataclass(frozen=True)
class PositionDetail:
    """A single instrument position quantity observed at the broker."""

    contract: OptionContract
    quantity: int


@dataclass(frozen=True)
class PositionSnapshot:
    """Aggregate option position as reported by the broker.

    Raw quantities only; the machine reconciles them into a single-slot state.
    """

    details: tuple[PositionDetail, ...] = ()


@runtime_checkable
class Broker(Protocol):
    """Minimal option broker surface used by the vNext machine."""

    def submit_order(self, order: OrderRequest) -> OrderTicket:
        ...

    def get_order_status(self, order_id: str) -> OrderStatusRecord:
        ...

    def get_position(self) -> PositionSnapshot:
        ...

    def reconcile(self) -> PositionSnapshot:
        ...