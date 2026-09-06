"""Broker abstraction.

Phase 1 ships only the paper broker. The abstract base defines the contract a
real broker adapter would need to satisfy in a later phase. All real-broker
integrations must remain disabled for live execution (see the project README's
paper-trading-only rule).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from decimal import Decimal

from fno_ai_paper_trading.models.enums import OrderSide
from fno_ai_paper_trading.models.market import MarketPrice
from fno_ai_paper_trading.models.order import Fill, Order


class Broker(ABC):
    """Contract any execution broker (paper or, later, real) must satisfy."""

    #: Hard toggle: the paper system never allows ``True`` here.
    is_live: bool = False

    @abstractmethod
    def place_order(self, order: Order, market_price: MarketPrice | None = None) -> Fill | None:
        """Submit an order and simulate/perform its execution.

        Returns the resulting :class:`Fill` when filled, else ``None``.
        """

    @abstractmethod
    def cancel_order(self, order_id: str) -> Order | None:
        """Cancel an open order, returning the updated order or ``None``."""

    @abstractmethod
    def get_order(self, order_id: str) -> Order | None:
        """Return the order with the given id, or ``None`` if unknown."""

    @staticmethod
    def _side_sign(side: OrderSide) -> int:
        return 1 if side == OrderSide.BUY else -1