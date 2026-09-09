"""Paper broker: simulated order execution.

IMPORTANT SAFETY NOTE
---------------------
This broker is a simulation only. It never connects to a network endpoint,
never contacts a real broker, and never submits or executes a real order.
``is_live`` is hard-coded to ``False`` and the class raises if it is ever
constructed with a live flag. This is the only broker shipped in Phase 1.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Callable

from fno_ai_paper_trading.broker.base import Broker
from fno_ai_paper_trading.models.enums import OrderSide, OrderStatus
from fno_ai_paper_trading.models.market import MarketPrice
from fno_ai_paper_trading.models.order import Fill, Order
from fno_ai_paper_trading.utils.functions import (
    new_id,
    non_negative_decimal,
    positive_decimal,
    positive_int,
)


@dataclass(frozen=True)
class PaperBrokerConfig:
    """Cost model for simulated fills."""

    commission_rate: Decimal = Decimal("0.0003")  # fraction of notional
    commission_fixed: Decimal = Decimal("0")  # flat per-fill fee
    slippage_rate: Decimal = Decimal("0.001")  # fraction of price

    def __post_init__(self) -> None:
        object.__setattr__(self, "commission_rate", non_negative_decimal(self.commission_rate, "commission_rate"))
        object.__setattr__(self, "commission_fixed", non_negative_decimal(self.commission_fixed, "commission_fixed"))
        object.__setattr__(self, "slippage_rate", non_negative_decimal(self.slippage_rate, "slippage_rate"))


class PaperBroker(Broker):
    """Simulates order submission, execution, fills and order status."""

    is_live: bool = False  # never allow live execution from this class

    def __init__(
        self,
        config: PaperBrokerConfig | None = None,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        if self.is_live:
            raise RuntimeError("PaperBroker must never execute live orders")
        self.config = config if config is not None else PaperBrokerConfig()
        self._now = now_fn if now_fn is not None else datetime.now
        self._orders: dict[str, Order] = {}
        self._fills: list[Fill] = []

    def place_order(self, order: Order, market_price: MarketPrice | None = None) -> Fill | None:
        if market_price is None:
            order.reject("no market price available for paper fill")
            return None

        order.order_id = new_id("ORD")
        order.submitted_at = self._now()
        order.submit()

        fill_price = self._apply_slippage(market_price.close, order.side)
        commission = self._compute_commission(order, fill_price)

        order.filled_quantity = order.quantity
        order.average_fill_price = fill_price
        order.filled_at = self._now()
        order.transition(OrderStatus.FILLED)

        fill = Fill(
            order_id=order.order_id,
            instrument=order.instrument,
            side=order.side,
            quantity=order.quantity,
            price=fill_price,
            commission=commission,
            filled_at=order.filled_at,
        )
        self._orders[order.order_id] = order
        self._fills.append(fill)
        return fill

    def cancel_order(self, order_id: str) -> Order | None:
        order = self._orders.get(order_id)
        if order is None:
            return None
        if order.status in (OrderStatus.PENDING, OrderStatus.SUBMITTED, OrderStatus.PARTIALLY_FILLED):
            order.cancel()
        return order

    def get_order(self, order_id: str) -> Order | None:
        return self._orders.get(order_id)

    @property
    def fills(self) -> list[Fill]:
        return list(self._fills)

    def _apply_slippage(self, price: Decimal, side: OrderSide) -> Decimal:
        base = positive_decimal(price, "price")
        factor = Decimal("1") + self.config.slippage_rate if side == OrderSide.BUY else Decimal("1") - self.config.slippage_rate
        return base * factor

    def _compute_commission(self, order: Order, fill_price: Decimal) -> Decimal:
        notional = positive_int(order.quantity, "quantity") * fill_price * order.instrument.multiplier
        return notional * self.config.commission_rate + self.config.commission_fixed