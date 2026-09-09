"""Tests for WS 6.4 automatic 2% stop-loss enforcement.

Covers the pure ``StopLossPolicy`` decision rule, the single authoritative
``enforce_stop`` executor, the ``TradingService.protective_exit`` delegate, and
the ``BacktestEngine`` wiring (signal-first, stop-second). Verifies the standard
slippage/commission model is reused, the ``Order`` state machine is preserved,
the ``RiskManager`` is bypassed for protective exits, and the WS 6.3 sizing
behavior is untouched.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from fno_ai_paper_trading.backtest.config import BacktestConfig
from fno_ai_paper_trading.backtest.engine import BacktestBroker, BacktestEngine
from fno_ai_paper_trading.broker.paper_broker import PaperBroker, PaperBrokerConfig
from fno_ai_paper_trading.config.settings import Environment, PaperSettings
from fno_ai_paper_trading.models.enums import (
    InstrumentType,
    OrderSide,
    OrderStatus,
    OrderType,
    Signal,
)
from fno_ai_paper_trading.models.instruments import Instrument
from fno_ai_paper_trading.models.market import MarketPrice
from fno_ai_paper_trading.models.position import Position
from fno_ai_paper_trading.portfolio.portfolio import Portfolio
from fno_ai_paper_trading.risk import RiskBasedPositionSizer, SizerConfig
from fno_ai_paper_trading.risk.manager import RiskManager
from fno_ai_paper_trading.risk.stop_loss import StopDecision, StopLossPolicy, enforce_stop
from fno_ai_paper_trading.services.trading_service import TradingService
from fno_ai_paper_trading.strategies.base import SignalResult, Strategy

BASE = datetime(2026, 9, 2, 9, 15)
ENTRY = Decimal("24000")
STOP = Decimal("23520")  # ENTRY * 0.98


def _ts(index: int) -> datetime:
    return BASE + timedelta(minutes=5 * index)


def _future() -> Instrument:
    return Instrument(symbol="NIFTY1", instrument_type=InstrumentType.FUTURE, underlying_symbol="NIFTY")


def _bar(
    index: int,
    *,
    open_: str = "24000",
    high: str = "24050",
    low: str = "23900",
    close: str = "24000",
) -> MarketPrice:
    return MarketPrice(
        instrument=_future(),
        timestamp=_ts(index),
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=500,
    )


def _long(entry: str = "24000", quantity: int = 5, opened_at: datetime | None = None) -> Position:
    return Position(
        instrument=_future(),
        quantity=quantity,
        average_entry_price=Decimal(entry),
        opened_at=_ts(0) if opened_at is None else opened_at,
    )


def _settings() -> PaperSettings:
    return PaperSettings(
        environment=Environment.TEST,
        initial_capital=Decimal("100000"),
        max_position_quantity=75,
        max_order_notional=Decimal("250000"),
        max_daily_loss=Decimal("1000"),
        commission_rate=Decimal("0.0003"),
        commission_fixed=Decimal("0"),
        slippage_rate=Decimal("0.001"),
    )


class _BuyAtBars(Strategy):
    """Deterministic strategy: BUY on the configured bar indices, HOLD elsewhere."""

    name = "buy-at-bars"

    def __init__(self, instrument: Instrument, indices: tuple[int, ...] = (0,)) -> None:
        self._instrument = instrument
        self._indices = set(indices)

    def analyze(self, bars: list[MarketPrice]) -> SignalResult:
        index = len(bars) - 1
        if index in self._indices:
            return SignalResult(
                signal=Signal.BUY,
                instrument=self._instrument,
                timestamp=bars[-1].timestamp,
                reason=f"buy at bar {index}",
            )
        return SignalResult(signal=Signal.HOLD, timestamp=bars[-1].timestamp)


# -----------------------------------------------------------------------------
# StopLossPolicy — decision rule
# -----------------------------------------------------------------------------


class TestStopLossPolicy:
    def test_no_stop_hit(self) -> None:
        policy = StopLossPolicy()
        bar = _bar(1, open_="24100", high="24150", low="23900", close="24000")
        assert policy.evaluate(position=_long(), bar=bar) is None

    def test_intrabar_stop_hit(self) -> None:
        policy = StopLossPolicy()
        bar = _bar(1, open_="23900", high="23950", low="23450", close="23500")
        decision = policy.evaluate(position=_long(), bar=bar)
        assert decision is not None
        assert decision.exit_price == STOP
        assert decision.branch == "stop"

    def test_gap_down_exits_at_open(self) -> None:
        policy = StopLossPolicy()
        bar = _bar(1, open_="22900", high="23100", low="22800", close="23000")
        decision = policy.evaluate(position=_long(), bar=bar)
        assert decision is not None
        assert decision.exit_price == Decimal("22900")
        assert decision.branch == "open"

    def test_open_equals_stop_is_inclusive(self) -> None:
        policy = StopLossPolicy()
        bar = _bar(1, open_="23520", high="23600", low="23490", close="23560")
        decision = policy.evaluate(position=_long(), bar=bar)
        assert decision is not None
        assert decision.exit_price == STOP
        assert decision.branch == "open"

    def test_low_equals_stop_is_inclusive(self) -> None:
        policy = StopLossPolicy()
        bar = _bar(1, open_="23900", high="23960", low="23520", close="23900")
        decision = policy.evaluate(position=_long(), bar=bar)
        assert decision is not None
        assert decision.exit_price == STOP
        assert decision.branch == "stop"

    def test_stop_price_is_two_percent_below_entry(self) -> None:
        policy = StopLossPolicy()
        assert policy.stop_price(_long()) == ENTRY * Decimal("0.98")

    def test_stop_anchored_to_post_slippage_average_entry(self) -> None:
        entry = ENTRY * Decimal("1.001")  # 24024: 24000 + 0.1% BUY slippage
        policy = StopLossPolicy()
        position = _long(entry=str(entry))
        assert policy.stop_price(position) == entry * Decimal("0.98")
        bar = _bar(1, open_="23900", high="23950", low="23450", close="23500")
        decision = policy.evaluate(position=position, bar=bar)
        assert decision is not None
        assert decision.exit_price == entry * Decimal("0.98")

    def test_entry_candle_is_excluded(self) -> None:
        policy = StopLossPolicy()
        bar = _bar(0, open_="23900", high="23950", low="23450", close="23500")
        position = _long(opened_at=_ts(0))  # opened on the same candle
        assert policy.evaluate(position=position, bar=bar) is None

    def test_future_opened_at_is_excluded(self) -> None:
        policy = StopLossPolicy()
        bar = _bar(1, open_="23900", high="23950", low="23450", close="23500")
        position = _long(opened_at=_ts(5))  # opened after the bar
        assert policy.evaluate(position=position, bar=bar) is None

    def test_flat_position_ignored(self) -> None:
        policy = StopLossPolicy()
        position = _long()
        position.quantity = 0  # emulate a fully closed position
        bar = _bar(1, open_="23900", high="23950", low="23450", close="23500")
        assert policy.evaluate(position=position, bar=bar) is None

    def test_short_position_ignored(self) -> None:
        policy = StopLossPolicy()
        position = _long(quantity=-5)
        bar = _bar(1, open_="23900", high="23950", low="23450", close="23500")
        assert policy.evaluate(position=position, bar=bar) is None

    @pytest.mark.parametrize("bad", ["0", "-0.02", "1", "2"])
    def test_invalid_stop_percentages_rejected(self, bad: str) -> None:
        with pytest.raises(ValueError):
            StopLossPolicy(stop_loss_pct=Decimal(bad))

    def test_stop_decision_validates_inputs(self) -> None:
        with pytest.raises(ValueError, match="branch"):
            StopDecision(exit_price=Decimal("23520"), branch="elsewhere")
        with pytest.raises(ValueError, match="> 0"):
            StopDecision(exit_price=Decimal("0"), branch="open")


# -----------------------------------------------------------------------------
# enforce_stop — single authoritative executor
# -----------------------------------------------------------------------------


class TestEnforceStop:
    def _portfolio(self) -> Portfolio:
        # The long position must be registered in the portfolio so that
        # ``Portfolio.apply_fill`` flattens it (enforce_stop closes via the
        # portfolio's own positions dict, mirroring the engine's lookup).
        return Portfolio(cash=Decimal("100000"), positions={"NIFTY1": _long()})

    def test_stop_order_type_side_and_quantity(self) -> None:
        result = enforce_stop(
            broker=PaperBroker(PaperBrokerConfig()),
            portfolio=self._portfolio(),
            position=_long(),
            bar=_bar(1, open_="23900", high="23950", low="23450", close="23500"),
        )
        assert result is not None
        assert result.order.order_type is OrderType.STOP
        assert result.order.side is OrderSide.SELL
        assert result.order.quantity == 5
        assert result.fill.quantity == 5

    def test_state_transitions_to_filled(self) -> None:
        result = enforce_stop(
            broker=PaperBroker(PaperBrokerConfig()),
            portfolio=self._portfolio(),
            position=_long(),
            bar=_bar(1, open_="23900", high="23950", low="23450", close="23500"),
        )
        assert result.order.status is OrderStatus.FILLED
        assert result.order.submitted_at is not None
        assert result.order.filled_at is not None

    def test_deterministic_backtest_broker_timestamp(self) -> None:
        broker = BacktestBroker(PaperBrokerConfig())
        broker.set_timestamp(_ts(1))
        result = enforce_stop(
            broker=broker,
            portfolio=self._portfolio(),
            position=_long(),
            bar=_bar(1, open_="23900", high="23950", low="23450", close="23500"),
        )
        assert result is not None and result.fill is not None and result.trade is not None
        assert result.order.filled_at == _ts(1)
        assert result.fill.filled_at == _ts(1)
        assert result.trade.executed_at == _ts(1)

    def test_reuses_existing_slippage(self) -> None:
        broker = PaperBroker(PaperBrokerConfig(slippage_rate=Decimal("0.001")))
        result = enforce_stop(
            broker=broker,
            portfolio=self._portfolio(),
            position=_long(),
            bar=_bar(1, open_="23900", high="23950", low="23450", close="23500"),
        )
        assert result is not None and result.fill is not None
        assert result.reference_price == STOP
        assert result.fill.price == STOP * (Decimal("1") - Decimal("0.001"))

    def test_reuses_existing_commission(self) -> None:
        broker = PaperBroker(PaperBrokerConfig(commission_rate=Decimal("0.0003"), commission_fixed=Decimal("0")))
        result = enforce_stop(
            broker=broker,
            portfolio=self._portfolio(),
            position=_long(),
            bar=_bar(1, open_="23900", high="23950", low="23450", close="23500"),
        )
        assert result is not None and result.fill is not None
        assert result.fill.commission == result.fill.notional * Decimal("0.0003")

    def test_portfolio_flattened_and_realized_pnl(self) -> None:
        broker = PaperBroker(PaperBrokerConfig(slippage_rate=Decimal("0.001"), commission_rate=Decimal("0")))
        portfolio = self._portfolio()
        result = enforce_stop(
            broker=broker,
            portfolio=portfolio,
            position=_long(),
            bar=_bar(1, open_="23900", high="23950", low="23450", close="23500"),
        )
        assert result is not None and result.trade is not None
        assert portfolio.open_positions() == {}
        expected = (result.fill.price - ENTRY) * 5 * 1
        assert result.trade.realized_pnl == expected
        assert result.trade.realized_pnl < 0
        assert result.trade.side is OrderSide.SELL

    def test_trade_generated(self) -> None:
        broker = PaperBroker(PaperBrokerConfig())
        portfolio = self._portfolio()
        enforce_stop(
            broker=broker,
            portfolio=portfolio,
            position=_long(),
            bar=_bar(1, open_="23900", high="23950", low="23450", close="23500"),
        )
        assert len(portfolio.trade_history) == 1
        assert broker.fills and len(broker.fills) == 1

    def test_risk_manager_is_bypassed(self) -> None:
        class _BoomRiskManager(RiskManager):
            def evaluate(self, order, portfolio, fill_price, realized_today=None):
                raise AssertionError("RiskManager.evaluate must never run for protective exits")

        settings = _settings()
        broker = PaperBroker(PaperBrokerConfig())
        portfolio = Portfolio(cash=Decimal("100000"), positions={"NIFTY1": _long()})
        service = TradingService(
            settings, provider=None, risk_manager=_BoomRiskManager(settings), broker=broker, portfolio=portfolio
        )
        result = service.protective_exit(
            _long(),
            _bar(1, open_="23900", high="23950", low="23450", close="23500"),
        )
        assert result is not None
        assert result.order.order_type is OrderType.STOP
        assert result.fill is not None
        assert portfolio.open_positions() == {}

    def test_no_trigger_produces_no_order(self) -> None:
        broker = PaperBroker(PaperBrokerConfig())
        portfolio = self._portfolio()
        bar = _bar(1, open_="24100", high="24150", low="23900", close="24000")
        result = enforce_stop(broker=broker, portfolio=portfolio, position=_long(), bar=bar)
        assert result is None
        assert broker.fills == []
        assert portfolio.trade_history == []
        assert portfolio.cash == Decimal("100000")


# -----------------------------------------------------------------------------
# BacktestEngine — signal-first, stop-second
# -----------------------------------------------------------------------------


class TestBacktestEngineStop:
    def _intrabar_breach_bars(self) -> list[MarketPrice]:
        return [
            _bar(0, open_="24000", high="24050", low="23900", close="24000"),  # BUY fills @ 24024
            _bar(1, open_="23800", high="23850", low="23400", close="23450"),  # intrabar breach of 23543.52
        ]

    def test_normal_intrabar_stop(self) -> None:
        bars = self._intrabar_breach_bars()
        result = BacktestEngine().run(bars, _BuyAtBars(_future()))
        entry = Decimal("24000") * Decimal("1.001")
        stop = entry * Decimal("0.98")
        fill = stop * Decimal("0.999")

        assert result.orders_filled == 2          # BUY + protective STOP
        assert result.orders_submitted == 2
        assert result.num_trades == 1             # one closing trade
        assert result.losing_trades == 1
        assert result.equity_curve[-1].unrealized_pnl == Decimal("0")
        trades = result.trades
        assert len(trades) == 2
        assert trades[0].side is OrderSide.BUY
        assert trades[0].price == entry
        assert trades[0].executed_at == _ts(0)
        assert trades[1].side is OrderSide.SELL
        assert trades[1].price == fill
        assert trades[1].executed_at == _ts(1)
        assert trades[1].realized_pnl == (fill - entry) * 1

    def test_gap_down_exits_at_open(self) -> None:
        bars = [
            _bar(0, open_="24000", high="24050", low="23900", close="24000"),  # entry 24024
            _bar(1, open_="23000", high="23200", low="22800", close="22900"),  # open < stop
        ]
        result = BacktestEngine().run(bars, _BuyAtBars(_future()))
        assert result.orders_filled == 2
        assert result.num_trades == 1
        sell = result.trades[-1]
        assert sell.price == Decimal("23000") * Decimal("0.999")
        assert abs(sell.realized_pnl) > Decimal("24000") * Decimal("0.02")  # beyond 2% (gap)

    def test_no_duplicate_exit(self) -> None:
        bars = [
            _bar(0, open_="24000", high="24050", low="23900", close="24000"),
            _bar(1, open_="23800", high="23850", low="23400", close="23450"),  # stop fires here
            _bar(2, open_="23200", high="23300", low="23000", close="23100"),  # flat: no re-trigger
            _bar(3, open_="22900", high="23000", low="22700", close="22800"),  # flat: no re-trigger
        ]
        result = BacktestEngine().run(bars, _BuyAtBars(_future()))
        assert result.orders_filled == 2
        assert result.orders_submitted == 2
        assert result.num_trades == 1

    def test_same_bar_entry_does_not_stop(self) -> None:
        bars = [
            _bar(0, open_="24000", high="24050", low="23000", close="24000"),  # entry candle, low breaches
            _bar(1, open_="23900", high="23950", low="23750", close="23800"),  # lows stay above stop
        ]
        result = BacktestEngine().run(bars, _BuyAtBars(_future()))
        assert result.orders_filled == 1          # entry only
        assert result.num_trades == 0             # position still open at end
        entry = Decimal("24000") * Decimal("1.001")
        assert result.equity_curve[-1].unrealized_pnl == (Decimal("23800") - entry) * 1

    def test_daily_loss_cap_does_not_block_protective_exit(self) -> None:
        # Entry bar 0, stop exit bar 1 (-~504 realized > cap 100); a second BUY at
        # bar 2 is then rejected by the daily-loss gate, while the protective
        # exit itself already executed unaffected.
        bars = [
            _bar(0, open_="24000", high="24050", low="23900", close="24000"),
            _bar(1, open_="23800", high="23850", low="23400", close="23450"),
            _bar(2, open_="24100", high="24150", low="24000", close="24100"),
        ]
        strategy = _BuyAtBars(_future(), indices=(0, 2))
        config = BacktestConfig(max_daily_loss=Decimal("100"))
        result = BacktestEngine().run(bars, strategy, config)
        assert result.signals_generated == 2      # bar 0 and bar 2 signals
        assert result.orders_submitted == 2       # BUY + STOP; bar-2 BUY rejected by cap
        assert result.orders_filled == 2          # protective stop was not blocked
        assert result.num_trades == 1             # the stop exit realized the loss

    def test_enable_stop_loss_false_preserves_previous_behavior(self) -> None:
        bars = self._intrabar_breach_bars()
        result = BacktestEngine().run(
            bars, _BuyAtBars(_future()), BacktestConfig(enable_stop_loss=False)
        )
        assert result.orders_filled == 1          # BUY only — no protective exit
        assert result.num_trades == 0
        assert result.equity_curve[-1].unrealized_pnl != Decimal("0")


# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------


class TestBacktestConfigStop:
    def test_defaults(self) -> None:
        config = BacktestConfig()
        assert config.enable_stop_loss is True
        assert config.stop_loss_pct == Decimal("0.02")

    def test_disabled_accepted(self) -> None:
        config = BacktestConfig(enable_stop_loss=False)
        assert config.enable_stop_loss is False

    def test_custom_stop_pct_accepted(self) -> None:
        config = BacktestConfig(stop_loss_pct=Decimal("0.05"))
        assert config.stop_loss_pct == Decimal("0.05")

    @pytest.mark.parametrize("bad", ["0", "-0.02", "1", "2"])
    def test_invalid_stop_pct_rejected(self, bad: str) -> None:
        with pytest.raises(ValueError):
            BacktestConfig(stop_loss_pct=Decimal(bad))


# -----------------------------------------------------------------------------
# WS 6.3 regression guard
# -----------------------------------------------------------------------------


class TestWS63RiskSizingUntouched:
    def test_sizer_stop_default_unchanged(self) -> None:
        assert SizerConfig().stop_loss_pct == Decimal("0.02")

    def test_sizer_still_resizes_and_skips_while_open(self) -> None:
        sizer = RiskBasedPositionSizer()
        instrument = _future()
        sizing = sizer.size(
            equity=Decimal("100000"),
            available_cash=Decimal("100000"),
            entry_price=Decimal("24000"),
            instrument=instrument,
            current_quantity=0,
        )
        assert sizing.approved
        assert sizing.stop_price == Decimal("24000") * Decimal("0.98")
        skipped = sizer.size(
            equity=Decimal("100000"),
            available_cash=Decimal("100000"),
            entry_price=Decimal("24000"),
            instrument=instrument,
            current_quantity=5,
        )
        assert not skipped.approved
        assert "position is already open" in skipped.skip_reason


def test_existing_losing_series_test_is_stop_loss_scoped() -> None:
    # Backward-compat proof: the legacy losing-series expectation (strategy SELL
    # at close 100, hand-verified in test_backtest.py) is preserved by
    # disable-stop-loss, i.e. the new default does not silently change that
    # cost-model result.
    from fno_ai_paper_trading.backtest.datasets import build_losing_series
    from fno_ai_paper_trading.strategies import MovingAverageCrossStrategy

    config = BacktestConfig(
        quantity=10,
        commission_rate=Decimal("0"),
        commission_fixed=Decimal("0"),
        slippage_rate=Decimal("0"),
        enable_stop_loss=False,
    )
    result = BacktestEngine().run(
        build_losing_series(_future()), MovingAverageCrossStrategy(fast=2, slow=3), config
    )
    assert result.num_trades == 1
    assert result.gross_loss == Decimal("-100")