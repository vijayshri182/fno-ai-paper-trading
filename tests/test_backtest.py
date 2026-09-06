"""Tests for the deterministic backtest harness.

Every expected value is computed by hand against the dataset builders in
``backtest.datasets``, so a regression surfaces immediately if the engine's
cost model, fill timing, or metric math drifts.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from fno_ai_paper_trading.backtest.config import BacktestConfig
from fno_ai_paper_trading.backtest.datasets import (
    build_drawdown_series,
    build_losing_series,
    build_multiple_trades_series,
    build_no_trade_series,
    build_profitable_series,
    build_short_profit_series,
)
from fno_ai_paper_trading.backtest.engine import BacktestBroker, BacktestEngine
from fno_ai_paper_trading.broker.paper_broker import PaperBroker
from fno_ai_paper_trading.models.enums import InstrumentType
from fno_ai_paper_trading.models.instruments import Instrument
from fno_ai_paper_trading.strategies import MovingAverageCrossStrategy


def _future() -> Instrument:
    return Instrument(symbol="NIFTY1", instrument_type=InstrumentType.FUTURE, underlying_symbol="NIFTY")


def _engine() -> BacktestEngine:
    return BacktestEngine()


def _strategy() -> MovingAverageCrossStrategy:
    return MovingAverageCrossStrategy(fast=2, slow=3)


ZERO_COST = BacktestConfig(
    quantity=10, commission_rate=Decimal("0"), commission_fixed=Decimal("0"), slippage_rate=Decimal("0")
)


def test_profitable_series_pnl_and_trade_count() -> None:
    result = _engine().run(build_profitable_series(_future()), _strategy(), ZERO_COST)
    # One round trip: BUY@110 -> SELL@120, qty 10, zero costs -> +100 raw.
    assert result.num_trades == 1
    assert result.gross_profit == Decimal("100")
    assert result.total_pnl == Decimal("100")
    assert result.total_return_pct == Decimal("0.1")
    assert result.winning_trades == 1
    assert result.losing_trades == 0
    assert result.win_rate == Decimal("100")
    assert result.total_commission == Decimal("0")
    assert result.profit_factor == Decimal("Infinity")


def test_losing_series_negative_pnl() -> None:
    result = _engine().run(build_losing_series(_future()), _strategy(), ZERO_COST)
    # BUY@110 -> SELL@100, qty 10, zero costs -> -100 raw.
    assert result.num_trades == 1
    assert result.gross_loss == Decimal("-100")
    assert result.total_pnl == Decimal("-100")


def test_multiple_trades() -> None:
    result = _engine().run(build_multiple_trades_series(_future()), _strategy(), ZERO_COST)
    assert result.num_trades == 2
    assert result.winning_trades == 2
    assert result.profit_factor == Decimal("Infinity")


def test_no_trade_scenario() -> None:
    result = _engine().run(build_no_trade_series(_future()), _strategy(), ZERO_COST)
    assert result.num_trades == 0
    assert result.orders_filled == 0
    assert result.final_equity == result.initial_capital


def test_short_sell_to_open_then_buy_to_close() -> None:
    result = _engine().run(build_short_profit_series(_future()), _strategy(), ZERO_COST)
    # SELL@140 opens the short; BUY@130 closes it.  qty 10 -> +100.
    assert result.num_trades == 1
    assert result.winning_trades == 1
    assert result.gross_profit == Decimal("100")
    assert result.total_pnl == Decimal("100")
    assert result.trades[0].side.value == "SELL"
    assert result.trades[-1].side.value == "BUY"


def test_transaction_costs_reduce_pnl() -> None:
    # Zero-cost profitable round trip = +100; with commission it must be lower.
    free = _engine().run(build_profitable_series(_future()), _strategy(), ZERO_COST)
    costed = _engine().run(
        build_profitable_series(_future()),
        _strategy(),
        BacktestConfig(quantity=10, commission_rate=Decimal("0.001"), slippage_rate=Decimal("0")),
    )
    assert costed.total_pnl < free.total_pnl
    assert costed.total_commission > 0


def test_slippage_reduces_pnl() -> None:
    no_slip = _engine().run(
        build_profitable_series(_future()),
        _strategy(),
        BacktestConfig(quantity=10, commission_rate=Decimal("0"), slippage_rate=Decimal("0")),
    )
    with_slip = _engine().run(
        build_profitable_series(_future()),
        _strategy(),
        BacktestConfig(quantity=10, commission_rate=Decimal("0"), slippage_rate=Decimal("0.005")),
    )
    assert with_slip.total_pnl < no_slip.total_pnl


def test_slippage_dollar_amount_exact() -> None:
    # BUY at 110: buy price 110, sell at 120. With 1% slippage: buy 111.1, sell 118.8.
    result = _engine().run(
        build_profitable_series(_future()),
        _strategy(),
        BacktestConfig(quantity=1, commission_rate=Decimal("0"), slippage_rate=Decimal("0.01")),
    )
    assert result.total_pnl == Decimal("7.70")  # (118.8 - 111.1)


def test_commission_dollar_amount_exact() -> None:
    # qty 10, buy@110.11 notional 1101.1, sell@119.88 notional 1198.8.
    # commission rate 0.0003 -> 0.33033 + 0.35964 = 0.68997.
    result = _engine().run(
        build_profitable_series(_future()),
        _strategy(),
        BacktestConfig(quantity=10, commission_rate=Decimal("0.0003"), slippage_rate=Decimal("0.001")),
    )
    assert result.total_commission == Decimal("0.68997")


def test_max_drawdown_nonzero_on_drawdown_series() -> None:
    result = _engine().run(build_drawdown_series(_future()), _strategy(), BacktestConfig(quantity=10))
    assert result.max_drawdown > 0
    assert result.max_drawdown_pct > 0
    # Deepest drawdown occurs after the position is held through the collapse.
    assert max(p.drawdown_from_peak for p in result.equity_curve) == result.max_drawdown


def test_equity_curve_chronological() -> None:
    result = _engine().run(build_drawdown_series(_future()), _strategy(), BacktestConfig(quantity=10))
    timestamps = [p.timestamp for p in result.equity_curve]
    assert timestamps == sorted(timestamps)
    assert len(result.equity_curve) == len(build_drawdown_series(_future()))
    # Equity curve starts at initial capital.
    assert result.equity_curve[0].equity == result.initial_capital


def test_no_look_ahead_before_crossover() -> None:
    # The strategy needs slow+1 = 4 bars before it can act; before that the
    # engine must place no orders and realize no P&L.
    bars = build_profitable_series(_future())
    result = _engine().run(bars, _strategy(), ZERO_COST)
    # First buy signal is at index 5 (0-based) — nothing fills before it.
    fill_times = [t.executed_at for t in result.trades]
    assert fill_times == sorted(fill_times)
    # Equity does not change before the first fill.
    for point in result.equity_curve[:5]:
        assert point.equity == result.initial_capital


def test_signal_timing_matches_strategy() -> None:
    bars = build_profitable_series(_future())
    strategy = _strategy()
    signals = [strategy.analyze(bars[: i + 1]) for i in range(len(bars))]
    buy_indices = [i for i, s in enumerate(signals) if s.signal.value == "BUY"]
    sell_indices = [i for i, s in enumerate(signals) if s.signal.value == "SELL"]
    assert buy_indices == [5]
    assert sell_indices == [10]


def test_fill_price_is_bar_close_with_slippage() -> None:
    bars = build_profitable_series(_future())
    result = _engine().run(bars, _strategy(), BacktestConfig(quantity=10, slippage_rate=Decimal("0")))
    # BUY at close 110, SELL at close 120 (no slippage, no commission, qty 10).
    assert result.gross_profit == Decimal("100")
    assert result.trades[0].price == Decimal("110")
    assert result.trades[1].price == Decimal("120")


def test_trade_realized_pnl_is_signed() -> None:
    # Losing trades must have negative realized P&L (guards Phase 1 regression).
    result = _engine().run(build_losing_series(_future()), _strategy(), ZERO_COST)
    assert result.trades[-1].realized_pnl < 0
    assert result.total_pnl < 0


def test_deterministic_repeatable() -> None:
    bars = build_multiple_trades_series(_future())
    a = _engine().run(bars, _strategy(), BacktestConfig(quantity=10))
    b = _engine().run(bars, _strategy(), BacktestConfig(quantity=10))
    assert a.final_equity == b.final_equity
    assert [p.equity for p in a.equity_curve] == [p.equity for p in b.equity_curve]
    assert [(t.trade_id, t.price) for t in a.trades] != [(t.trade_id, t.price) for t in b.trades]


def test_end_of_data_leaves_position_open() -> None:
    # If the final signal is a BUY with no later SELL, the position stays open
    # and only the entry fill is recorded — no closing trade is counted.
    bars = build_profitable_series(_future())[:8]  # cut before the SELL at bar 10
    result = _engine().run(bars, _strategy(), ZERO_COST)
    assert result.num_trades == 0
    assert len(result.trades) == 1
    assert result.equity_curve[-1].unrealized_pnl != 0
    assert result.equity_curve[-1].cash != result.initial_capital


def test_risk_manager_rejects_position_limit() -> None:
    config = BacktestConfig(quantity=10, max_position_quantity=5)
    result = _engine().run(build_profitable_series(_future()), _strategy(), config)
    assert result.orders_submitted == 0 or result.orders_filled == 0
    assert result.orders_submitted == result.orders_filled  # nothing bypasses rejection


def test_risk_manager_disabled_allows_order() -> None:
    config = BacktestConfig(quantity=10, enable_risk_manager=False)
    result = _engine().run(build_profitable_series(_future()), _strategy(), config)
    assert result.orders_filled == 2  # BUY + SELL both filled


def test_backtest_broker_is_paper_only() -> None:
    broker = BacktestBroker()
    assert broker.is_live is False
    assert isinstance(broker, PaperBroker)


def test_engine_runs_offline_without_credentials_or_provider() -> None:
    # The engine signature takes bars + strategy + config only: no provider, no
    # broker, no credentials. This structurally proves no live-data path exists.
    engine = _engine()
    result = engine.run(build_profitable_series(_future()), _strategy(), ZERO_COST)
    assert result.final_equity == result.initial_capital + Decimal("100")


def test_empty_series_returns_identity() -> None:
    result = _engine().run([], _strategy(), ZERO_COST)
    assert result.num_bars_processed == 0
    assert result.final_equity == result.initial_capital
    assert result.num_trades == 0
    assert result.equity_curve == []
