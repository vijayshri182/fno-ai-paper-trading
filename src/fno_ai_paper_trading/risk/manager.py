"""Pre-trade risk management.

The risk manager is the gatekeeper between strategy/order intent and the paper
broker. It validates an order against configured limits and returns an explicit
approval decision. It never mutates orders or touches a broker; callers decide
what to do with a rejection.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from fno_ai_paper_trading.config.settings import PaperSettings
from fno_ai_paper_trading.models.enums import OrderSide, RejectionReason
from fno_ai_paper_trading.models.order import Order
from fno_ai_paper_trading.portfolio.portfolio import Portfolio
from fno_ai_paper_trading.utils.functions import positive_decimal


@dataclass(frozen=True)
class RiskDecision:
    """Result of a pre-trade risk evaluation."""

    approved: bool
    reasons: list[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        if self.approved:
            return "approved"
        return "rejected: " + "; ".join(self.reasons)


class RiskManager:
    """Applies pre-trade checks against :class:`PaperSettings` limits."""

    def __init__(self, settings: PaperSettings) -> None:
        self.settings = settings

    def evaluate(
        self,
        order: Order,
        portfolio: Portfolio,
        fill_price: Decimal,
        realized_today: Decimal | None = None,
    ) -> RiskDecision:
        """Evaluate ``order`` against all configured limits.

        ``fill_price`` is the expected reference price used for notional checks.
        ``realized_today`` defaults to the portfolio's realized P&L for today.
        """
        price = positive_decimal(fill_price, "fill_price")
        reasons: list[str] = []

        if not order.instrument.symbol.strip():
            reasons.append(RejectionReason.UNKNOWN_INSTRUMENT.value)

        resulting = self._resulting_quantity(order, portfolio)
        if abs(resulting) > self.settings.max_position_quantity:
            reasons.append(RejectionReason.MAX_POSITION_QUANTITY_EXCEEDED.value)

        order_notional = price * order.quantity * order.instrument.multiplier
        if order_notional > self.settings.max_order_notional:
            reasons.append(RejectionReason.MAX_ORDER_NOTIONAL_EXCEEDED.value)

        if realized_today is None:
            realized_today = portfolio.realized_pnl_today()
        if realized_today <= -self.settings.max_daily_loss:
            reasons.append(RejectionReason.DAILY_LOSS_LIMIT_REACHED.value)

        return RiskDecision(approved=not reasons, reasons=reasons)

    @staticmethod
    def _resulting_quantity(order: Order, portfolio: Portfolio) -> int:
        current = portfolio.current_quantity(order.instrument.symbol)
        if order.side == OrderSide.BUY:
            return current + order.quantity
        return current - order.quantity