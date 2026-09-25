"""Shared deterministic doubles for the vNext isolated test-suite.

Nothing here touches the network, Upstox, paper, or live accounts. All fakes
are scriptable and deterministic so the state machine can be tested exactly.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Callable

from fno_ai_paper_trading.execution.vnext.broker import (
    OrderRequest,
    OrderStatusRecord,
    OrderTicket,
    PositionDetail,
    PositionSnapshot,
)
from fno_ai_paper_trading.execution.vnext.contract import (
    MarketContext,
    OptionContract,
    TableContractResolver,
)
from fno_ai_paper_trading.execution.vnext.enums import (
    ContractType,
    OptionLeg,
    OrderStatus,
    TxSide,
)


def make_contract(
    option_leg: OptionLeg,
    *,
    expiry: date = date(2026, 12, 24),
    strike: Decimal = Decimal("24500"),
    lot_size: int = 75,
    tick_size: Decimal = Decimal("0.05"),
) -> OptionContract:
    """Deterministic, expiry/strike/tick fixed per test but never hardcoded
    in the machine. The production layer never hardcodes a trading day."""
    contract_type = (
        ContractType.CE if option_leg is OptionLeg.CALL else ContractType.PE
    )
    return OptionContract(
        option_leg=option_leg,
        contract_type=contract_type,
        instrument_key=f"{option_leg.value}.{contract_type.value}",
        expiry=expiry,
        strike=strike,
        lot_size=lot_size,
        tick_size=tick_size,
    )


def make_resolver(
    contracts: dict[OptionLeg, OptionContract] | None = None,
) -> TableContractResolver:
    existing = contracts or {
        OptionLeg.CALL: make_contract(OptionLeg.CALL),
        OptionLeg.PUT: make_contract(OptionLeg.PUT),
    }
    return TableContractResolver(existing, MarketContext(underlying="NIFTY"))


@dataclass
class ScriptedBroker:
    """Deterministic in-memory broker.

    Script-driven:
      - ``auto_fill``: when True every accepted order fills immediately;
      - ``reject_next``: optionally reject the NEXT submission with a status
        (REJECTED / CANCELLED / UNFILLED) to exercise failure paths;
      - ``latch_position``: override the broker-held position snapshot
        (used to simulate "reconciliation says old position remains").
    """

    contracts: dict[OptionLeg, OptionContract] = field(default_factory=dict)
    auto_fill: bool = True
    reject_next: OrderStatus | None = None
    reject_if: Callable[[OrderRequest], OrderStatus | None] | None = None
    latch_position: PositionSnapshot | None = None
    suppress_fills: bool = False
    raise_on_submit: Exception | None = None
    raise_on_status: Exception | None = None
    raise_on_position: Exception | None = None
    raise_on_reconcile: Exception | None = None

    _orders: dict[str, OrderRequest] = field(default_factory=dict)
    _status: dict[str, OrderStatus] = field(default_factory=dict)
    _seq: int = field(default=0)
    submitted: list[OrderRequest] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.contracts:
            self.contracts = {
                OptionLeg.CALL: make_contract(OptionLeg.CALL),
                OptionLeg.PUT: make_contract(OptionLeg.PUT),
            }
        # Default: no real position; reconciled as FLAT.
        self._position: dict[str, tuple[OptionContract, int]] = {}

    # --------------------------------------------------------------- script

    def set_position(self, contract: OptionContract, quantity: int) -> None:
        """Pin the broker's view of a position (for restart/reconcile tests)."""
        self._position[contract.instrument_key] = (contract, quantity)

    def set_order_status(self, order_id: str, status: OrderStatus) -> None:
        """Manually drive an order's status (for in-flight polling tests)."""
        if status is OrderStatus.FILLED and not self.suppress_fills:
            order = self._orders[order_id]
            self._apply_fill(order)
        self._status[order_id] = status

    @property
    def position_snapshot(self) -> PositionSnapshot:
        if self.latch_position is not None:
            return self.latch_position
        details = tuple(
            PositionDetail(contract=contract, quantity=quantity)
            for contract, quantity in self._position.values()
        )
        return PositionSnapshot(details=details)

    # ------------------------------------------------------------- protocol

    def submit_order(self, order: OrderRequest) -> OrderTicket:
        if self.raise_on_submit is not None:
            raise self.raise_on_submit
        self._seq += 1
        order_id = f"t{self._seq}"
        self._orders[order_id] = order
        self.submitted.append(order)

        if self.reject_next is not None:
            status = self.reject_next
            self.reject_next = None
        elif self.reject_if is not None:
            override = self.reject_if(order)
            if override is not None:
                status = override
            elif self.auto_fill:
                status = OrderStatus.FILLED
            else:
                status = OrderStatus.PENDING
        elif self.auto_fill:
            status = OrderStatus.FILLED
        else:
            status = OrderStatus.PENDING

        self._status[order_id] = status
        if status is OrderStatus.FILLED and not self.suppress_fills:
            self._apply_fill(order)
        return OrderTicket(order_id=order_id, status=status)

    def _apply_fill(self, order: OrderRequest) -> None:
        qty = order.quantity
        delta = qty if order.tx_side is TxSide.BUY else -qty
        key = order.contract.instrument_key
        held = self._position.get(key)
        if held is None:
            self._position[key] = (order.contract, delta)
        else:
            self._position[key] = (held[0], held[1] + delta)

    def get_order_status(self, order_id: str) -> OrderStatusRecord:
        if self.raise_on_status is not None:
            raise self.raise_on_status
        status = self._status.get(order_id, OrderStatus.PENDING)
        message = "scripted"
        return OrderStatusRecord(order_id=order_id, status=status, message=message)

    def get_position(self) -> PositionSnapshot:
        if self.raise_on_position is not None:
            raise self.raise_on_position
        return self.position_snapshot

    def reconcile(self) -> PositionSnapshot:
        if self.raise_on_reconcile is not None:
            raise self.raise_on_reconcile
        return self.position_snapshot