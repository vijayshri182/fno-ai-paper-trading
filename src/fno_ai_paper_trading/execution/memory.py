"""Deterministic in-memory execution adapter (smoke demo + tests, WS 7.9).

Fills immediately at the quote price (with an optional flat slippage), records
positions exactly like a broker would, and never touches a network or a real
broker. It implements the same :class:`ExecutionAdapter` interface as the real
Upstox adapter, so the manager behaves identically against both.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Callable, Mapping

from fno_ai_paper_trading.execution.errors import UpstoxExecutionError
from fno_ai_paper_trading.execution.gate import ExecutionMode
from fno_ai_paper_trading.execution.upstox import (
    ExecutionAck,
    ExecutionAdapter,
    ExecutionPosition,
)
from fno_ai_paper_trading.models.enums import OrderSide, OrderStatus
from fno_ai_paper_trading.models.order import Order
from fno_ai_paper_trading.utils.functions import new_id


@dataclass
class MemoryExecutionAdapter(ExecutionAdapter):
    """Offline adapter: immediate fills, synthesised positions, no network.

    ``dry_run`` is ``True`` by class: it never writes to a real broker, so the
    manager's real-send gate (consent + explicit confirmation) must not apply.
    """

    prices: Mapping[str, Decimal]
    account_id: str = "memory-account"
    slippage: Decimal = Decimal("0")
    now_fn: Callable[[], datetime] = datetime.now
    dry_run: bool = True

    def __post_init__(self) -> None:
        self._account = self.account_id
        self._positions: dict[str, int] = {}
        self._order_statuses: dict[str, OrderStatus] = {}
        self._last_fill: Decimal = Decimal("0")

    # ---------------- interface (mirrors UpstoxExecutionAdapter) -------------

    def get_account_id(self) -> str:
        return self._account

    def quote(self, symbol: str) -> Decimal:
        try:
            return Decimal(self.prices[symbol])
        except KeyError as exc:
            raise UpstoxExecutionError(f"no quote for {symbol!r}") from exc

    def place_order(self, order: Order, mode: ExecutionMode = ExecutionMode.LIVE_EXECUTION_TEST) -> ExecutionAck:
        if mode is ExecutionMode.LIVE:
            raise UpstoxExecutionError(
                "normal LIVE order placement is not implemented; only LIVE_EXECUTION_TEST may be enabled"
            )
        order_id = order.order_id or new_id("ORD")
        order.order_id = order_id
        ack = ExecutionAck(
            order_id=order_id,
            provider_order_id=f"MEM-{order_id}",
            status=OrderStatus.SUBMITTED,
            timestamp=self.now_fn().isoformat(),
            dry_run=False,
        )
        self._order_statuses[order_id] = OrderStatus.SUBMITTED

        premium = self.quote(order.instrument.exchange_token)
        fill_price = premium * (
            Decimal("1") + self.slippage
            if order.side is OrderSide.BUY
            else Decimal("1") - self.slippage
        )
        delta = order.quantity if order.side is OrderSide.BUY else -order.quantity
        self._positions[order.instrument.exchange_token] = (
            self._positions.get(order.instrument.exchange_token, 0) + delta
        )
        self._last_fill = fill_price
        self._order_statuses[order_id] = OrderStatus.FILLED
        return ack

    def get_order_status(self, order_id: str, quantity: int) -> tuple[OrderStatus, dict[str, object]]:
        status = self._order_statuses.get(order_id, OrderStatus.PENDING)
        detail: dict[str, object] = {
            "status": status.value,
            "filled_quantity": quantity if status is OrderStatus.FILLED else 0,
            "average_price": str(self._last_fill) if status is OrderStatus.FILLED else None,
        }
        return status, detail

    def cancel_order(self, order_id: str) -> bool:
        if order_id in self._order_statuses:
            self._order_statuses[order_id] = OrderStatus.CANCELLED
        return True

    def get_positions(self) -> list[ExecutionPosition]:
        positions: list[ExecutionPosition] = []
        for symbol, quantity in self._positions.items():
            positions.append(
                ExecutionPosition(
                    symbol=symbol,
                    exchange="NSE",
                    quantity=quantity,
                    average_price=self._last_fill if quantity else None,
                    pnl=None,
                )
            )
        return positions