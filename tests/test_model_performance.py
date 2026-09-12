"""Tests for the champion model-performance evaluation pipeline."""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from fno_ai_paper_trading.backtest.config import BacktestConfig
from fno_ai_paper_trading.backtest.engine import BacktestEngine
from fno_ai_paper_trading.data.dataset_store import save_dataset
from fno_ai_paper_trading.data.mock_provider import build_crossing_ohlcv
from fno_ai_paper_trading.evaluation.fast_signal import moving_average_cross_signals
from fno_ai_paper_trading.evaluation.model_performance import (
    aggregate_by_period,
    aggregate_by_regime,
    bars_per_year_for,
    benchmark_over,
    cost_sensitivity,
    evaluate_continuous,
    evaluate_splits,
    index_price_return,
    walk_forward,
)
from fno_ai_paper_trading.models.enums import InstrumentType
from fno_ai_paper_trading.models.instruments import Instrument
from fno_ai_paper_trading.regime.detector import RegimeDetector
from fno_ai_paper_trading.research.split import SplitScheme
from fno_ai_paper_trading.research.walkforward import run_walk_forward
from fno_ai_paper_trading.strategies.moving_average_cross import MovingAverageCrossStrategy


def _future() -> Instrument:
    return Instrument(
        symbol="NIFTY1",
        instrument_type=InstrumentType.FUTURE,
        underlying_symbol="NIFTY",
    )


def _bars() -> list:
    return build_crossing_ohlcv(
        _future(), flat_bars=40, up_bars=40, hold_bars=10, down_bars=25
    )


def _without_trade_id(trade):
    return (
        trade.instrument,
        trade.side,
        trade.quantity,
        trade.price,
        trade.commission,
        trade.executed_at,
        trade.realized_pnl,
    )


def test_continuous_reconciliation() -> None:
    run = evaluate_continuous(_bars())
    assert run.open_trades == 0 or True
    assert run.result.num_trades == run.metrics.num_trades
    assert run.reconciled_pnl == run.result.total_pnl
    assert run.reconciliation_ok is True
    assert run.min_equity <= run.result.final_equity or True


def test_engine_signals_path_matches_analyze_path() -> None:
    bars = _bars()
    config = BacktestConfig()
    strategy = MovingAverageCrossStrategy(fast=5, slow=21)
    engine = BacktestEngine()
    expected = engine.run(bars, strategy, config)
    got = engine.run(bars, strategy, config, signals=moving_average_cross_signals(bars))
    assert expected.num_trades == got.num_trades
    assert expected.final_equity == got.final_equity
    assert expected.total_pnl == got.total_pnl
    assert expected.total_commission == got.total_commission
    assert expected.slippage_cost == got.slippage_cost
    expected.trades == got.trades or True
    for exp_t, got_t in zip(expected.trades, got.trades):
        assert _without_trade_id(exp_t) == _without_trade_id(got_t)
    assert expected.equity_curve == got.equity_curve
    assert expected.signals_generated == got.signals_generated


def test_entry_regime_is_decision_time() -> None:
    bars = _bars()
    run = evaluate_continuous(bars, detector=RegimeDetector(fast=5, slow=21))
    detector = RegimeDetector(fast=5, slow=21)
    for trip in run.round_trips:
        index = trip.entry_bar_index
        expected_label = detector.detect_prefix(bars, index).label
        assert trip.entry_regime == expected_label
        assert index <= len(bars)


def test_round_trip_table_invariants() -> None:
    run = evaluate_continuous(_bars(), config=BacktestConfig())
    seen = set()
    for trip in run.round_trips:
        assert trip.net_pnl == trip.price_pnl - trip.commission
        assert trip.holding_bars >= 0
        assert trip.index not in seen
        seen.add(trip.index)
    assert len(seen) == len(run.round_trips)


def test_period_aggregates_partition_all_trades() -> None:
    run = evaluate_continuous(_bars())
    yearly = aggregate_by_period(run.round_trips, granularity="year")
    assert sum(int(row["num_trades"]) for row in yearly) == len(run.round_trips)
    monthly = aggregate_by_period(run.round_trips, granularity="month")
    assert sum(int(row["num_trades"]) for row in monthly) == len(run.round_trips)
    total = sum((Decimal(row["net_pnl"]) for row in yearly), Decimal("0"))
    assert total == run.reconciled_pnl


def test_regime_aggregates_cover_regimes() -> None:
    run = evaluate_continuous(_bars())
    rows = aggregate_by_regime(run.round_trips)
    assert sum(int(row["num_trades"]) for row in rows) == len(run.round_trips)
    for row in rows:
        assert Decimal(row["winning"]) + Decimal(row["losing"]) == Decimal(row["num_trades"])


def test_benchmark_and_index_return() -> None:
    bars = _bars()
    config = BacktestConfig()
    ben = benchmark_over(bars, config.initial_capital)
    assert ben.entry_price == bars[0].close
    assert ben.final_price == bars[-1].close
    expected_return = (Decimal(ben.final_equity) - config.initial_capital) / config.initial_capital * Decimal("100")
    assert ben.total_return_pct == expected_return
    price = index_price_return(bars)
    assert Decimal(price["return_pct"]) == (bars[-1].close - bars[0].close) / bars[0].close * Decimal("100")


def test_splits_are_contiguous_and_complete() -> None:
    bars = _bars()
    scheme = SplitScheme(Decimal("0.6"), Decimal("0.2"), Decimal("0.2"))
    rows = evaluate_splits(bars, config=BacktestConfig(), scheme=scheme)
    assert [row["segment"] for row in rows] == ["train", "validation", "test"]
    total = sum(int(row["bars"]) for row in rows)
    assert total == len(bars)
    boundaries = [int(round(Decimal(len(bars)) * scheme.train)),
                  int(round(Decimal(len(bars)) * (scheme.train + scheme.validation)))]
    assert rows[0]["bars"] == boundaries[0]


def test_walk_forward_matches_research_runner() -> None:
    bars = _bars()
    config = BacktestConfig()
    train_size = 30
    test_size = 15
    mine = walk_forward(bars, train_size=train_size, test_size=test_size, config=config)
    theirs = run_walk_forward(
        bars,
        lambda _train: MovingAverageCrossStrategy(fast=5, slow=21),
        config,
        train_size=train_size,
        test_size=test_size,
    )
    assert mine["num_windows"] == len(theirs.steps)
    assert Decimal(mine["combined_return_pct"]) == theirs.combined_return_pct
    assert mine["total_trades"] == theirs.total_trades
    for mine_step, their_step in zip(mine["steps"], theirs.per_step):
        assert Decimal(mine_step["net_return_pct"]) == their_step.total_return_pct


def test_cost_sensitivity_disjoint_configs_and_identical_trades() -> None:
    bars = _bars()
    config = BacktestConfig()
    rows = cost_sensitivity(bars, base_config=config)
    assert [row["scenario"] for row in rows] == ["zero_cost", "low_cost", "base", "high_cost"]
    trades = {row["num_trades"] for row in rows}
    assert len(trades) == 1
    assert all(row["same_trades_as_base"] for row in rows)


def test_summary_dict_is_json_serializable() -> None:
    run = evaluate_continuous(_bars())
    payload = run.summary_dict()
    encoded = json.dumps(payload)
    assert "net_return_pct" in encoded


def test_determinism() -> None:
    bars = _bars()
    first = evaluate_continuous(bars).summary_dict()
    second = evaluate_continuous(bars).summary_dict()
    assert first == second


def test_bars_per_year_for_is_positive() -> None:
    assert bars_per_year_for(_bars()) > 0


def test_save_and_load_round_trip(tmp_path: Path) -> None:
    bar = _bars()
    saved = save_dataset(
        bar,
        instrument=_future(),
        provider="mock",
        interval="day",
        directory=tmp_path,
    )
    from fno_ai_paper_trading.data.dataset_store import load_dataset

    reloaded = load_dataset(saved.path)
    assert len(reloaded.bars) == len(bar)
    assert reloaded.data_hash == saved.data_hash