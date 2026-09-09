"""Integration tests for the strategy service.

Verifies that strategy signals become paper orders ONLY through TradingService
(RiskManager -> PaperBroker -> Portfolio), and that evaluate() never executes.
"""
from __future__ import annotations

from decimal import Decimal

from fno_ai_paper_trading.broker.paper_broker import PaperBroker, PaperBrokerConfig
from fno_ai_paper_trading.config.settings import Environment, PaperSettings
from fno_ai_paper_trading.data.mock_provider import (
    InMemoryMarketDataProvider,
    build_crossing_ohlcv,
)
from fno_ai_paper_trading.models.enums import InstrumentType, OrderStatus, Signal
from fno_ai_paper_trading.models.instruments import Instrument
from fno_ai_paper_trading.models.market import MarketPrice
from fno_ai_paper_trading.portfolio.portfolio import Portfolio
from fno_ai_paper_trading.risk.manager import RiskManager
from fno_ai_paper_trading.risk.sizer import RiskBasedPositionSizer
from fno_ai_paper_trading.services.strategy_service import StrategyService
from fno_ai_paper_trading.strategies import MovingAverageCrossStrategy


def _settings() -> PaperSettings:
    return PaperSettings(
        environment=Environment.PAPER,
        initial_capital=Decimal("100000"),
        max_position_quantity=10,
        max_order_notional=Decimal("200000"),
        max_daily_loss=Decimal("5000"),
        commission_rate=Decimal("0"),
        commission_fixed=Decimal("0"),
        slippage_rate=Decimal("0"),
    )


def _future() -> Instrument:
    return InMemoryMarketDataProvider().get_instruments()[0]


def _index() -> Instrument:
    """Lot-size-1 index instrument so a 1% risk / 2% stop sizes to 2 lots."""
    return Instrument(
        symbol="NIFTY_INDEX",
        instrument_type=InstrumentType.INDEX,
        underlying_symbol="NIFTY",
    )


def _index_provider() -> InMemoryMarketDataProvider:
    index = _index()
    return InMemoryMarketDataProvider(
        instruments=[index],
        history={index.symbol: build_crossing_ohlcv(index)},
    )


def _bars() -> list[MarketPrice]:
    return build_crossing_ohlcv(_future())


def _service(**kwargs) -> tuple[StrategyService, Portfolio]:
    settings = _settings()
    provider = kwargs.pop("provider", InMemoryMarketDataProvider())
    broker = PaperBroker(PaperBrokerConfig())
    portfolio = Portfolio(settings.initial_capital)
    risk_manager = RiskManager(settings)
    service = StrategyService(
        settings, provider, risk_manager, broker, portfolio,
        strategy=MovingAverageCrossStrategy(fast=5, slow=21),
        quantity=kwargs.pop("quantity", 5),
        sizer=kwargs.pop("sizer", None),
    )
    return service, portfolio


class TestEvaluate:
    def test_evaluate_creates_no_orders(self) -> None:
        service, portfolio = _service()
        bars = _bars()
        decisions = service.evaluate(bars)

        assert len(decisions) == len(bars)
        assert all(d.order_result is None and not d.executed for d in decisions)
        assert any(d.result.actionable for d in decisions)
        assert portfolio.cash == portfolio.initial_cash
        assert portfolio.open_positions() == {}


class TestRun:
    def test_buy_and_sell_signals_execute_paper_orders(self) -> None:
        service, portfolio = _service()
        decisions = service.run(_bars())

        actionable = [d for d in decisions if d.result.actionable]
        assert actionable, "expected BUY then SELL"
        assert all(d.executed for d in actionable)
        assert all(d.order_result.fill is not None for d in actionable)
        assert all(d.order_result.order.status is OrderStatus.FILLED for d in actionable)

        assert actionable[0].result.signal is Signal.BUY
        assert actionable[-1].result.signal is Signal.SELL

        # SELL closed the long back to flat.
        assert portfolio.current_quantity("NIFTY1") == 0
        assert portfolio.open_positions() == {}
        assert len(portfolio.trade_history) == 2
        assert portfolio.cash != portfolio.initial_cash

    def test_hold_signals_never_submit_orders(self) -> None:
        service, portfolio = _service()
        decisions = service.run(_bars())
        holds = [d for d in decisions if not d.result.actionable]
        assert holds
        assert all(d.order_result is None for d in holds)

    def test_risk_manager_gates_strategy_orders(self) -> None:
        # A quantity above the position limit must be rejected by RiskManager.
        settings = _settings()
        provider = InMemoryMarketDataProvider()
        broker = PaperBroker(PaperBrokerConfig())
        portfolio = Portfolio(settings.initial_capital)
        risk_manager = RiskManager(settings)
        service = StrategyService(
            settings, provider, risk_manager, broker, portfolio,
            strategy=MovingAverageCrossStrategy(fast=5, slow=21),
            quantity=99,  # > max_position_quantity=10
        )
        decisions = service.run(_bars())
        actionable = [d for d in decisions if d.result.actionable]
        assert actionable
        assert all(d.order_result is not None for d in actionable)
        assert all(not d.executed for d in actionable)
        assert all(d.order_result.order.rejection_reason is not None for d in actionable)


class TestRunWithSizer:
    def test_fixed_quantity_path_is_unchanged_without_sizer(self) -> None:
        service, _ = _service(quantity=5)
        decisions = service.run(_bars())
        actionable = [d for d in decisions if d.result.actionable]
        assert actionable
        assert all(d.sizing is None for d in decisions)
        assert all(d.executed for d in actionable)
        assert all(d.order_result.order.quantity == 5 for d in actionable)

    def test_sizer_sizes_buy_and_leaves_sell_fixed(self) -> None:
        # Lot-size-1 index: 1% risk / 2% stop at the BUY close sizes to 2 lots.
        service, portfolio = _service(
            provider=_index_provider(),
            quantity=2,
            sizer=RiskBasedPositionSizer(),
        )
        decisions = service.run(build_crossing_ohlcv(_index()))

        actionable = [d for d in decisions if d.result.actionable]
        assert len(actionable) >= 2
        buy, sell = actionable[0], actionable[-1]
        assert buy.result.signal is Signal.BUY
        assert sell.result.signal is Signal.SELL

        assert buy.sizing is not None
        assert buy.sizing.approved
        assert buy.sizing.quantity == 2
        assert buy.order_result.order.quantity == 2
        assert buy.executed and buy.order_result.fill is not None

        assert sell.sizing is None  # SELL is never sized
        assert sell.order_result.order.quantity == 2  # fixed quantity path unchanged
        assert sell.executed and sell.order_result.fill is not None

        # SELL closed the sized long back to flat.
        assert portfolio.current_quantity("NIFTY_INDEX") == 0
        assert portfolio.open_positions() == {}

    def test_rejected_sizing_submits_no_order(self) -> None:
        # Lot-size-75 future: the 1% risk basis is beneath one lot, so the BUY
        # is skipped and recorded without an order.
        service, _ = _service(sizer=RiskBasedPositionSizer())
        decisions = service.run(_bars())

        buys = [
            d for d in decisions
            if d.result.actionable and d.result.signal is Signal.BUY and d.sizing is not None
        ]
        assert len(buys) >= 1
        buy = buys[0]
        assert not buy.sizing.approved
        assert "below the minimum" in buy.sizing.skip_reason
        assert buy.order_result is None
        assert not buy.executed

    def test_risk_manager_still_gates_sized_order(self) -> None:
        # The sizer approves 2 lots (notional 48,450), but RiskManager rejects
        # the order against the tighter notional limit — it stays authoritative.
        settings = PaperSettings(
            environment=Environment.PAPER,
            initial_capital=Decimal("100000"),
            max_position_quantity=10,
            max_order_notional=Decimal("30000"),
            max_daily_loss=Decimal("5000"),
            commission_rate=Decimal("0"),
            commission_fixed=Decimal("0"),
            slippage_rate=Decimal("0"),
        )
        provider = _index_provider()
        broker = PaperBroker(PaperBrokerConfig())
        portfolio = Portfolio(settings.initial_capital)
        risk_manager = RiskManager(settings)
        service = StrategyService(
            settings, provider, risk_manager, broker, portfolio,
            strategy=MovingAverageCrossStrategy(fast=5, slow=21),
            quantity=2,
            sizer=RiskBasedPositionSizer(),
        )
        decisions = service.run(build_crossing_ohlcv(_index()))

        buys = [
            d for d in decisions
            if d.result.actionable and d.result.signal is Signal.BUY and d.sizing is not None
        ]
        assert len(buys) >= 1
        buy = buys[0]
        assert buy.sizing.approved  # approved by the sizer...
        assert not buy.executed  # ...blocked by the risk manager.
        assert buy.order_result is not None
        assert buy.order_result.order.quantity == 2
        assert buy.order_result.order.rejection_reason is not None
        assert any("MAX_ORDER_NOTIONAL" in r for r in buy.order_result.decision.reasons)
        assert buy.order_result.fill is None