"""Strategy service: turns strategy signals into paper orders.

This is the only place signals become orders. It delegates execution to
:class:`TradingService`, which already enforces that the broker is a
``PaperBroker`` and that every order passes through the ``RiskManager`` —
strategies can never execute on their own.

When a :class:`RiskBasedPositionSizer` is supplied, ``BUY`` entry quantities
are sized before the order is built; a sizing rejection records a
``skip_reason`` and submits no order. Without a sizer the fixed ``quantity``
path is unchanged.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from fno_ai_paper_trading.broker.base import Broker
from fno_ai_paper_trading.data.provider import MarketDataProvider
from fno_ai_paper_trading.models.enums import OrderSide, Signal
from fno_ai_paper_trading.models.instruments import Instrument
from fno_ai_paper_trading.models.market import MarketPrice
from fno_ai_paper_trading.portfolio.portfolio import Portfolio
from fno_ai_paper_trading.risk.manager import RiskManager
from fno_ai_paper_trading.risk.sizer import RiskBasedPositionSizer, SizingResult
from fno_ai_paper_trading.services.trading_service import OrderResult, TradingService
from fno_ai_paper_trading.strategies.base import SignalResult, Strategy
from fno_ai_paper_trading.strategies.engine import StrategyEngine
from fno_ai_paper_trading.utils.functions import positive_int


@dataclass(frozen=True)
class SignalDecision:
    """One bar's strategy output plus the paper-execution outcome (if any)."""

    result: SignalResult
    order_result: OrderResult | None = None
    executed: bool = False
    sizing: SizingResult | None = None


class StrategyService:
    """Runs a strategy over bars; optionally submits paper orders per signal."""

    def __init__(
        self,
        settings,
        provider: MarketDataProvider,
        risk_manager: RiskManager,
        broker: Broker,
        portfolio: Portfolio,
        strategy: Strategy,
        quantity: int = 1,
        sizer: RiskBasedPositionSizer | None = None,
    ) -> None:
        self._trading = TradingService(settings, provider, risk_manager, broker, portfolio)
        self._engine = StrategyEngine(strategy)
        self.strategy = strategy
        self.quantity = positive_int(quantity, "quantity")
        self.sizer = sizer

    def evaluate(self, bars: list[MarketPrice]) -> list[SignalDecision]:
        """Generate signals only — no orders are submitted."""
        return [SignalDecision(result=result) for result in self._engine.evaluate(bars)]

    def run(self, bars: list[MarketPrice]) -> list[SignalDecision]:
        """Evaluate the strategy and submit paper orders for BUY/SELL signals.

        Every order goes through ``RiskManager`` and is executed by the paper
        broker only. Signals that are not actionable or that have no instrument
        are recorded without an order. When a sizer is supplied, ``BUY`` signals
        are sized first: a rejected sizing submits no order and is recorded with
        its ``skip_reason``.
        """
        decisions: list[SignalDecision] = []
        for bar, result in zip(bars, self._engine.evaluate(bars)):
            if not result.actionable or result.instrument is None:
                decisions.append(SignalDecision(result=result))
                continue

            side = OrderSide.BUY if result.signal is Signal.BUY else OrderSide.SELL
            sizing: SizingResult | None = None
            quantity = self.quantity

            if side is OrderSide.BUY and self.sizer is not None:
                sizing = self.sizer.size(
                    equity=self._decision_equity(result.instrument, bar.close),
                    available_cash=self._trading.portfolio.cash,
                    entry_price=bar.close,
                    instrument=result.instrument,
                    current_quantity=self._trading.portfolio.current_quantity(
                        result.instrument.symbol
                    ),
                )
                if not sizing.approved:
                    decisions.append(SignalDecision(result=result, sizing=sizing))
                    continue
                quantity = sizing.quantity

            order_result = self._trading.submit_order(
                result.instrument,
                side,
                quantity,
                reference_price=bar.close,
            )
            decisions.append(
                SignalDecision(
                    result=result,
                    order_result=order_result,
                    executed=order_result.decision.approved,
                    sizing=sizing,
                )
            )
        return decisions

    def _decision_equity(self, instrument: Instrument, close_price) -> Decimal:
        """Marked-to-market equity at the completed-bar close.

        Returns an explicit numeric value (never the account itself) by valuing
        open positions at ``close_price`` for the decision instrument and the
        provider's last price otherwise, then summing with cash.
        """
        prices: dict[str, Decimal] = {}
        for symbol, position in self._trading.portfolio.open_positions().items():
            prices[symbol] = (
                close_price
                if symbol == instrument.symbol
                else self._trading.provider.get_last_price(position.instrument)
            )
        return self._trading.portfolio.total_value(prices)