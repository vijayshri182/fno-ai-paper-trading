"""Trading service: the public orchestration layer.

A `TradingService` wires together data → risk → broker → portfolio and exposes
a single `submit_order(...)` method. It never places a live order — the paper
broker is always used.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from fno_ai_paper_trading.broker.base import Broker
from fno_ai_paper_trading.data.provider import MarketDataProvider
from fno_ai_paper_trading.models.enums import OrderSide
from fno_ai_paper_trading.models.instruments import Instrument
from fno_ai_paper_trading.models.market import MarketPrice
from fno_ai_paper_trading.models.order import Fill, Order
from fno_ai_paper_trading.models.position import Position, Trade
from fno_ai_paper_trading.portfolio.portfolio import Portfolio
from fno_ai_paper_trading.risk.manager import RiskDecision, RiskManager
from fno_ai_paper_trading.risk.stop_loss import StopLossPolicy, enforce_stop
from fno_ai_paper_trading.utils.logging import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class OrderResult:
    """Everything the caller needs to know after submitting an order."""

    decision: RiskDecision
    order: Order
    fill: Fill | None = None
    trade: Trade | None = None
    reference_price: Decimal | None = None


class TradingService:
    """Orchestrates risk checks, paper broker execution and portfolio updates."""

    def __init__(
        self,
        settings,
        provider: MarketDataProvider,
        risk_manager: RiskManager,
        broker: Broker,
        portfolio: Portfolio,
    ) -> None:
        self.settings = settings
        self.provider = provider
        self.risk_manager = risk_manager
        self.broker = broker
        self.portfolio = portfolio

    def submit_order(
        self,
        instrument: Instrument,
        side: OrderSide,
        quantity: int,
        reference_price: Decimal | None = None,
        fill_bar: MarketPrice | None = None,
    ) -> OrderResult:
        """Validate, fill and record a single order.

        ``reference_price``: if ``None``, the last price from the data provider
        is used. A supplied price overrides the provider (useful for tests and
        backtesting).

        ``fill_bar``: an optional :class:`MarketPrice` whose ``close`` the broker
        fills against. When ``None``, the provider's latest bar is used. Supplying
        a specific completed bar keeps paper fills pinned to that bar's close
        (used by the deterministic paper session); the original behavior is the
        default.
        """
        price = reference_price if reference_price is not None else self.provider.get_last_price(instrument)
        order = Order(instrument=instrument, side=side, quantity=quantity)
        logger.info(
            "submit_order %s %s %d @ %s",
            side.value,
            instrument.symbol,
            quantity,
            price,
        )

        decision = self.risk_manager.evaluate(order, self.portfolio, price)
        if not decision.approved:
            order.reject(decision.summary)
            logger.warning("order rejected: %s", decision.summary)
            return OrderResult(decision=decision, order=order, reference_price=price)

        from fno_ai_paper_trading.broker.paper_broker import PaperBroker

        if not isinstance(self.broker, PaperBroker):
            raise RuntimeError("non-paper brokers are not supported in Phase 1")

        market_price = fill_bar if fill_bar is not None else self.provider.get_market_price(instrument)
        fill = self.broker.place_order(order, market_price)
        if fill is None:
            return OrderResult(decision=decision, order=order, reference_price=price)

        trade = self.portfolio.apply_fill(fill)
        logger.info("filled trade %s", trade.trade_id)
        return OrderResult(
            decision=decision,
            order=order,
            fill=fill,
            trade=trade,
            reference_price=price,
        )

    def protective_exit(
        self,
        position: Position,
        bar: MarketPrice,
        policy: StopLossPolicy | None = None,
    ) -> OrderResult | None:
        """Execute the protective stop-loss exit for an open long position.

        A thin delegate to :func:`enforce_stop` (the single authoritative stop
        executor). The ``RiskManager`` is deliberately **not** consulted — a
        protective exit must remain executable after the daily-loss limit is
        reached. Normal ``Order`` state transitions (PENDING → SUBMITTED →
        FILLED) and ``Portfolio.apply_fill`` accounting are preserved. Returns
        ``None`` when the policy produces no decision or the broker cannot fill.
        """
        result = enforce_stop(
            broker=self.broker,
            portfolio=self.portfolio,
            position=position,
            bar=bar,
            policy=policy,
        )
        if result is None or result.fill is None:
            return None
        return OrderResult(
            decision=RiskDecision(approved=True, reasons=["protective stop exit"]),
            order=result.order,
            fill=result.fill,
            trade=result.trade,
            reference_price=result.reference_price,
        )