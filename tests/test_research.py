"""Tests for the strategy research & robustness evaluation framework.

Every expected value is hand-verified against the documented, deterministic
inputs (cost schedule fractions, regime close formulas, split boundaries,
benchmark arithmetic), so a regression in cost math, dataset shaping, or
metric computation surfaces immediately.
"""
from __future__ import annotations

import json
from decimal import Decimal

import pytest

from fno_ai_paper_trading.backtest.config import BacktestConfig
from fno_ai_paper_trading.backtest.datasets import build_profitable_series
from fno_ai_paper_trading.backtest.engine import BacktestEngine
from fno_ai_paper_trading.models.enums import InstrumentType, OrderSide
from fno_ai_paper_trading.models.instruments import Instrument
from fno_ai_paper_trading.research.benchmark import BenchmarkResult, buy_and_hold
from fno_ai_paper_trading.research.costs import ChargeBreakdown, IndiaCostSchedule
from fno_ai_paper_trading.research.execution import ExecutionAssumptions
from fno_ai_paper_trading.research.experiment import (
    ExperimentConfig,
    ExperimentResult,
    run_experiment,
)
from fno_ai_paper_trading.research.metrics import PerformanceMetrics, compute_metrics
from fno_ai_paper_trading.research.regimes import (
    REGIME_BUILDERS,
    build_low_volatility,
    build_sideways_choppy,
    build_sustained_downtrend,
    build_sustained_uptrend,
    build_trend_reversal,
    build_volatile_market,
    regime_stats,
)
from fno_ai_paper_trading.research.sensitivity import (
    run_parameter_sensitivity,
    validate_ma_pairs,
)
from fno_ai_paper_trading.research.split import SplitScheme, split_bars, split_indices
from fno_ai_paper_trading.research.walkforward import plan_windows, run_walk_forward
from fno_ai_paper_trading.strategies import MovingAverageCrossStrategy


def _future() -> Instrument:
    return Instrument(symbol="NIFTY1", instrument_type=InstrumentType.FUTURE, underlying_symbol="NIFTY")


def _strategy() -> MovingAverageCrossStrategy:
    return MovingAverageCrossStrategy(fast=2, slow=3)


ZERO_COST = BacktestConfig(
    quantity=10, commission_rate=Decimal("0"), commission_fixed=Decimal("0"), slippage_rate=Decimal("0")
)


# ---------------------------------------------------------------------------
# Cost model
# ---------------------------------------------------------------------------

def test_cost_breakdown_sell_total_exact() -> None:
    s = IndiaCostSchedule.nse_fo_illustrative()
    b = s.compute(OrderSide.SELL, Decimal("100000"), 5)
    assert isinstance(b, ChargeBreakdown)
    assert b.side == OrderSide.SELL
    assert b.brokerage == Decimal("0")
    assert b.stt == Decimal("1.2500000")
    assert b.exchange_charges == Decimal("2.00000")
    assert b.sebi_charges == Decimal("0.100000")
    assert b.stamp_duty == Decimal("0")
    # GST base = brokerage + exchange + sebi (0.18 * (0 + 2 + 0.1) = 0.378).
    assert b.gst == Decimal("0.37800000")
    assert b.other_charges == Decimal("0")
    assert b.total == Decimal("3.72800000")


def test_cost_breakdown_buy_total_exact() -> None:
    s = IndiaCostSchedule.nse_fo_illustrative()
    b = s.compute(OrderSide.BUY, Decimal("100000"), 5)
    assert b.stt == Decimal("0")
    assert b.stamp_duty == Decimal("0.200000")
    assert b.exchange_charges == Decimal("2.00000")
    assert b.gst == Decimal("0.37800000")
    assert b.total == Decimal("2.67800000")


def test_cost_zero_notional_sell_has_gst_only() -> None:
    # With zero notional all fractions are zero; per-order flats still apply.
    s = IndiaCostSchedule(
        brokerage_per_order=Decimal("20"),
        other_per_order=Decimal("5"),
        gst_rate=Decimal("0.18"),
    )
    b = s.compute(OrderSide.SELL, Decimal("1000"), 1)
    # taxable base includes the flat brokerage: 0.18 * (20 + 0 + 0) = 3.6.
    assert b.brokerage == Decimal("20")
    assert b.other_charges == Decimal("5")
    assert b.gst == Decimal("3.60000")
    assert b.total == Decimal("28.60000")


def test_cost_component_totals_match_notional_fractions() -> None:
    s = IndiaCostSchedule(
        brokerage_fraction=Decimal("0.0005"),
        stt_sell_fraction=Decimal("0.0001"),
        exchange_txn_fraction=Decimal("0.00002"),
        sebi_fraction=Decimal("0.000001"),
        gst_rate=Decimal("0.18"),
    )
    notional = Decimal("500000")
    b = s.compute(OrderSide.SELL, notional, 3)
    assert b.brokerage == notional * Decimal("0.0005")
    assert b.stt == notional * Decimal("0.0001")
    assert b.gst == Decimal("0.18") * (b.brokerage + b.exchange_charges + b.sebi_charges)


def test_cost_schedule_validates_inputs() -> None:
    s = IndiaCostSchedule.nse_fo_illustrative()
    with pytest.raises(ValueError):
        s.compute(OrderSide.BUY, Decimal("-1"), 1)
    with pytest.raises(ValueError):
        s.compute(OrderSide.BUY, Decimal("100"), 0)
    with pytest.raises(ValueError):
        IndiaCostSchedule(gst_rate=Decimal("1.01"))


# ---------------------------------------------------------------------------
# Execution assumptions
# ---------------------------------------------------------------------------

def test_execution_assumptions_default_total_adverse_rate() -> None:
    e = ExecutionAssumptions()
    assert e.total_adverse_rate == Decimal("0.0005")


def test_execution_assumptions_sum_components() -> None:
    e = ExecutionAssumptions(
        slippage_rate=Decimal("0.0005"),
        half_spread_rate=Decimal("0.0002"),
        impact_rate=Decimal("0.0003"),
    )
    assert e.total_adverse_rate == Decimal("0.001")


def test_execution_assumptions_reject_negative_rates() -> None:
    with pytest.raises(ValueError):
        ExecutionAssumptions(slippage_rate=Decimal("-0.001"))


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def _profitable_result():
    return BacktestEngine().run(build_profitable_series(_future()), _strategy(), ZERO_COST)


def test_metrics_on_known_profitable_round_trip() -> None:
    m = compute_metrics(_profitable_result())
    assert isinstance(m, PerformanceMetrics)
    assert m.net_pnl == Decimal("100")
    assert m.net_return_pct == Decimal("0.1")
    assert m.num_trades == 1
    assert m.winning_trades == 1
    assert m.losing_trades == 0
    assert m.win_rate == Decimal("100")
    assert m.profit_factor == Decimal("Infinity")
    assert m.expectancy == Decimal("100")
    assert m.avg_winning_trade == Decimal("100")
    assert m.avg_losing_trade is None
    assert m.total_bars == 13
    assert m.bars_in_market == 5
    assert m.exposure_pct == Decimal("38.46153846153846153846153846")  # 5/13


def test_metrics_cagr_none_for_very_short_series() -> None:
    m = compute_metrics(_profitable_result())
    assert m.cagr_pct is None


def test_metrics_none_when_no_trades() -> None:
    m = compute_metrics(BacktestEngine().run([], _strategy(), ZERO_COST))
    assert m.net_pnl == Decimal("0")
    assert m.num_trades == 0
    assert m.expectancy is None
    assert m.avg_winning_trade is None
    assert m.sharpe_ratio is None
    assert m.exposure_pct == Decimal("0")
    assert m.bars_in_market == 0


def test_metrics_cagr_and_exposure_on_uptrend() -> None:
    bars = build_sustained_uptrend(_future(), bars=240)
    res = BacktestEngine().run(bars, MovingAverageCrossStrategy(fast=5, slow=21), ZERO_COST)
    m = compute_metrics(res)
    assert m.net_pnl == Decimal("6210")  # buy@71 x10, hold to close 692; equity 106210
    assert m.cagr_pct is not None and m.cagr_pct > 0
    assert m.bars_in_market == 208
    assert m.exposure_pct == Decimal("86.66666666666666666666666667")  # 208/240


def test_metrics_use_realized_and_unrealized_exposure() -> None:
    # A long held to end-of-data must still count as bar exposure even when no
    # closing trade exists (open position marked to market).
    bars = build_profitable_series(_future())[:8]  # cut before SELL
    res = BacktestEngine().run(bars, _strategy(), ZERO_COST)
    m = compute_metrics(res)
    assert m.bars_in_market >= 1
    assert res.equity_curve[-1].unrealized_pnl != Decimal("0")


# ---------------------------------------------------------------------------
# Benchmark
# ---------------------------------------------------------------------------

def test_buy_and_hold_flat_series_no_skin() -> None:
    bh = buy_and_hold(build_profitable_series(_future()), Decimal("100000"), 10)
    assert isinstance(bh, BenchmarkResult)
    assert bh.entry_price == Decimal("100")
    assert bh.final_price == Decimal("100")
    assert bh.final_equity == Decimal("100000")
    assert bh.total_return_pct == Decimal("0")
    assert bh.exposure_pct == Decimal("100")
    assert bh.is_benchmark is True


def test_buy_and_hold_uptrend_return_exact() -> None:
    bh = buy_and_hold(build_sustained_uptrend(_future(), bars=240), Decimal("250000"), 5)
    # entry 100 -> final 692, qty 5, multiplier 1 -> +2960 on 250000 = 1.184%.
    assert bh.total_return_pct == Decimal("1.184")
    assert bh.final_equity == Decimal("252960")


def test_buy_and_hold_unfunded_raises() -> None:
    with pytest.raises(ValueError):
        buy_and_hold(build_sustained_uptrend(_future(), bars=60), Decimal("1000"), 500000)


# ---------------------------------------------------------------------------
# Regime datasets  (exact close sequences — hand-verified)
# ---------------------------------------------------------------------------

def _closes(bars) -> list[Decimal]:
    return [b.close for b in bars]


def test_uptrend_exact_closes() -> None:
    assert _closes(build_sustained_uptrend(_future(), bars=6)) == [
        Decimal("100"), Decimal("98"), Decimal("96"), Decimal("94"), Decimal("92"), Decimal("90"),
    ]
    # warm=26 decline then sustained rise.
    assert _closes(build_sustained_uptrend(_future(), bars=30))[-5:] == [
        Decimal("50"), Decimal("53"), Decimal("56"), Decimal("59"), Decimal("62"),
    ]


def test_downtrend_exact_closes() -> None:
    assert _closes(build_sustained_downtrend(_future(), bars=8)) == [
        Decimal("300"), Decimal("302"), Decimal("304"), Decimal("306"),
        Decimal("308"), Decimal("310"), Decimal("312"), Decimal("314"),
    ]


def test_choppy_exact_closes() -> None:
    assert _closes(build_sideways_choppy(_future(), bars=12, level=Decimal("100"), amplitude=Decimal("2"))) == [
        Decimal("94"), Decimal("96"), Decimal("98"), Decimal("100"),
        Decimal("102"), Decimal("104"), Decimal("94"), Decimal("96"),
        Decimal("98"), Decimal("100"), Decimal("102"), Decimal("104"),
    ]


def test_volatile_exact_closes() -> None:
    assert _closes(build_volatile_market(_future(), bars=6, level=Decimal("100"), amplitude=Decimal("10"))) == [
        Decimal("90"), Decimal("90"), Decimal("110"), Decimal("110"), Decimal("90"), Decimal("90"),
    ]


def test_low_volatility_exact_closes() -> None:
    assert _closes(build_low_volatility(_future(), bars=6, level=Decimal("100"))) == [
        Decimal("99.5"), Decimal("100.5"), Decimal("99.5"), Decimal("100.5"), Decimal("99.5"), Decimal("100.5"),
    ]


def test_regime_stats_exact() -> None:
    stats = regime_stats(build_sustained_uptrend(_future(), bars=30))
    assert stats["first"] == Decimal("100")
    assert stats["last"] == Decimal("62")
    assert stats["min"] == Decimal("50")
    assert stats["max"] == Decimal("100")
    assert stats["range"] == Decimal("50")


def test_regime_builders_registry_keys() -> None:
    assert set(REGIME_BUILDERS) == {
        "sustained_uptrend",
        "sustained_downtrend",
        "sideways_choppy",
        "volatile_market",
        "trend_reversal",
        "low_volatility",
    }


def test_regimes_empty_and_deterministic() -> None:
    assert build_sustained_uptrend(_future(), bars=0) == []
    a = build_sideways_choppy(_future(), bars=120)
    b = build_sideways_choppy(_future(), bars=120)
    assert _closes(a) == _closes(b)  # fully reproducible


def test_uptrend_generates_single_buy_cross() -> None:
    # With fast=5, slow=21 the warm=26 regime produces exactly one BUY cross.
    bars = build_sustained_uptrend(_future(), bars=240)
    strat = MovingAverageCrossStrategy(fast=5, slow=21)
    signals = [strat.analyze(bars[: i + 1]) for i in range(len(bars))]
    buys = [i for i, s in enumerate(signals) if s.signal.value == "BUY"]
    sells = [i for i, s in enumerate(signals) if s.signal.value == "SELL"]
    assert buys == [32]
    assert sells == []


def test_trend_reversal_generates_buy_then_sell() -> None:
    bars = build_trend_reversal(_future(), bars=240)
    strat = MovingAverageCrossStrategy(fast=5, slow=21)
    signals = [strat.analyze(bars[: i + 1]) for i in range(len(bars))]
    buys = [i for i, s in enumerate(signals) if s.signal.value == "BUY"]
    sells = [i for i, s in enumerate(signals) if s.signal.value == "SELL"]
    assert buys == [32]
    assert sells == [123]


# ---------------------------------------------------------------------------
# Train/validation/test splits
# ---------------------------------------------------------------------------

def test_split_default_scheme_fractions() -> None:
    s = SplitScheme()
    assert s.train == Decimal("0.6")
    assert s.validation == Decimal("0.2")
    assert s.test == Decimal("0.2")


def test_split_scheme_must_sum_to_one() -> None:
    with pytest.raises(ValueError):
        SplitScheme(train=Decimal("0.5"), validation=Decimal("0.2"), test=Decimal("0.2"))


def test_split_indices_exact() -> None:
    # 30 bars / 60/20/20 floor semantics.
    assert split_indices(30) == (0, 18, 18, 24, 24, 30)


def test_split_bars_contiguous_and_conserves_order() -> None:
    bars = build_sustained_uptrend(_future(), bars=30)
    split = split_bars(bars)
    assert len(split.train) == 18
    assert len(split.validation) == 6
    assert len(split.test) == 6
    # concatenation reproduces the input exactly (chronological, contiguous).
    assert split.train + split.validation + split.test == bars
    # boundaries are derivable from ranges.
    assert split.boundaries == {"train_end": 18, "validation_end": 24, "test_end": 30}


# ---------------------------------------------------------------------------
# Walk-forward
# ---------------------------------------------------------------------------

def test_plan_windows_exact_layout() -> None:
    windows = plan_windows(100, train_size=40, test_size=20, step=20)
    layout = [(w.train_start, w.train_end, w.test_start, w.test_end) for w in windows]
    assert layout == [(0, 40, 40, 60), (20, 60, 60, 80), (40, 80, 80, 100)]
    assert all(w.test_end - w.test_start == 20 for w in windows)


def test_plan_windows_rejects_step_smaller_than_test() -> None:
    with pytest.raises(ValueError):
        plan_windows(100, train_size=40, test_size=20, step=10)


def test_walk_forward_test_windows_are_out_of_sample() -> None:
    bars = build_volatile_market(_future(), bars=240)
    config = BacktestConfig(quantity=5, commission_rate=Decimal("0"), slippage_rate=Decimal("0"))
    wf = run_walk_forward(
        bars,
        lambda train: MovingAverageCrossStrategy(fast=5, slow=21),
        config,
        train_size=120,
        test_size=60,
        step=60,
    )
    assert len(wf.steps) == 2
    assert wf.total_oos_bars == 120
    # Each step evaluated on its own test window only (no train leakage).
    for step_result in wf.per_step:
        assert step_result.num_bars_processed == 60
    assert wf.total_trades > 0


def test_walk_forward_build_strategy_receives_train_only() -> None:
    bars = build_sustained_uptrend(_future(), bars=240)
    seen: list[int] = []
    config = BacktestConfig(quantity=5, commission_rate=Decimal("0"), slippage_rate=Decimal("0"))

    def build(train):
        seen.append(len(train))
        return MovingAverageCrossStrategy(fast=5, slow=21)

    run_walk_forward(bars, build, config, train_size=120, test_size=60, step=60)
    assert seen == [120, 120]


# ---------------------------------------------------------------------------
# Parameter sensitivity
# ---------------------------------------------------------------------------

def test_validate_ma_pairs_filters_invalid_pairs() -> None:
    # Guard: the helper FILTERS invalid pairs rather than raising, because the
    # research loop treats them as skipped (never silently executed).
    valid = validate_ma_pairs([5, 10, 50], [21, 50, 10])
    assert (50, 10) not in valid and (50, 21) not in valid and (10, 10) not in valid
    assert (5, 21) in valid and (5, 50) in valid
    assert validate_ma_pairs([50], [21]) == []


def test_parameter_sensitivity_skips_invalid_pairs() -> None:
    bars = build_sideways_choppy(_future(), bars=120)
    sens = run_parameter_sensitivity(bars, ZERO_COST, [5, 20, 50], [10, 21])
    valid = {(5, 10), (5, 21), (20, 21)}
    skipped = {(20, 10), (50, 10), (50, 21)}
    assert {(r.fast, r.slow) for r in sens.rows} == valid
    assert {(s.fast, s.slow) for s in sens.skipped} == skipped
    # num_runs counts executed runs only; skipped pairs are reported separately.
    assert sens.num_runs == len(sens.rows)
    assert sens.num_skipped == len(sens.skipped)


def test_parameter_sensitivity_rows_black_box_match_engine() -> None:
    bars = build_sideways_choppy(_future(), bars=120)
    sens = run_parameter_sensitivity(bars, ZERO_COST, [5], [21])
    row = next(r for r in sens.rows if r.fast == 5 and r.slow == 21)
    direct = BacktestEngine().run(bars, MovingAverageCrossStrategy(fast=5, slow=21), ZERO_COST)
    assert row.num_trades == direct.num_trades
    assert row.net_pnl == direct.total_pnl
    assert row.max_drawdown_pct == direct.max_drawdown_pct


# ---------------------------------------------------------------------------
# Experiments
# ---------------------------------------------------------------------------

def test_experiment_config_metadata_and_hash() -> None:
    cfg = ExperimentConfig(
        name="ma_cross_demo",
        strategy_name="moving_average_cross",
        strategy_params={"fast": 5, "slow": 21},
        dataset_name="sideways_choppy",
    )
    data = json.loads(cfg.canonical_json())
    assert data["name"] == "ma_cross_demo"
    assert data["strategy_params"] == {"fast": 5, "slow": 21}
    assert data["framework_version"] == "research-1.0"
    assert len(cfg.config_hash) == 16
    same = ExperimentConfig(
        name="ma_cross_demo",
        strategy_name="moving_average_cross",
        strategy_params={"fast": 5, "slow": 21},
        dataset_name="sideways_choppy",
    )
    other = ExperimentConfig(
        name="ma_cross_demo",
        strategy_name="moving_average_cross",
        strategy_params={"fast": 10, "slow": 21},
        dataset_name="sideways_choppy",
    )
    assert cfg.config_hash == same.config_hash
    assert cfg.config_hash != other.config_hash


def test_run_experiment_returns_metrics_and_benchmark() -> None:
    bars = build_trend_reversal(_future(), bars=240)
    exp = run_experiment(
        bars,
        MovingAverageCrossStrategy(fast=5, slow=21),
        ZERO_COST,
        name="reversal",
        strategy_params={"fast": 5, "slow": 21},
        dataset_name="trend_reversal",
        benchmark_quantity=5,
    )
    assert isinstance(exp, ExperimentResult)
    assert exp.config.name == "reversal"
    assert exp.metrics.num_trades == 1
    assert exp.metrics.winning_trades == 1
    assert exp.benchmark is not None
    assert isinstance(exp.benchmark, BenchmarkResult)


def test_run_experiment_benchmark_optional() -> None:
    bars = build_trend_reversal(_future(), bars=240)
    exp = run_experiment(
        bars,
        MovingAverageCrossStrategy(fast=5, slow=21),
        ZERO_COST,
        name="reversal_no_benchmark",
        strategy_params={},
        dataset_name="trend_reversal",
        benchmark_quantity=None,
    )
    assert exp.benchmark is None


def test_run_experiment_unfunded_benchmark_degraded_to_none() -> None:
    bars = build_trend_reversal(_future(), bars=240)
    exp = run_experiment(
        bars,
        MovingAverageCrossStrategy(fast=5, slow=21),
        BacktestConfig(quantity=5, initial_capital=Decimal("1000")),
        name="unfunded_benchmark",
        strategy_params={},
        dataset_name="trend_reversal",
        benchmark_quantity=50000,
    )
    assert exp.benchmark is None


def test_run_experiment_is_deterministic() -> None:
    bars = build_sideways_choppy(_future(), bars=120)
    kw = dict(
        name="determinism",
        strategy_params={"fast": 5, "slow": 21},
        dataset_name="sideways_choppy",
        benchmark_quantity=None,
    )
    a = run_experiment(bars, MovingAverageCrossStrategy(fast=5, slow=21), ZERO_COST, **kw)
    b = run_experiment(bars, MovingAverageCrossStrategy(fast=5, slow=21), ZERO_COST, **kw)
    assert a.metrics.net_pnl == b.metrics.net_pnl
    assert a.config.config_hash == b.config.config_hash


# ---------------------------------------------------------------------------
# Engine wiring — cost schedule + execution assumptions
# ---------------------------------------------------------------------------

def test_engine_uses_cost_schedule_for_fill_charges() -> None:
    class FlatSchedule:
        def compute(self, side, notional, quantity):
            return ChargeBreakdown(
                side=side, notional=notional, quantity=quantity,
                brokerage=Decimal("1.23"), stt=Decimal("0"), exchange_charges=Decimal("0"),
                sebi_charges=Decimal("0"), stamp_duty=Decimal("0"), gst=Decimal("0"),
                other_charges=Decimal("0"),
            )

    config = BacktestConfig(quantity=10, cost_schedule=FlatSchedule())
    result = BacktestEngine().run(build_profitable_series(_future()), _strategy(), config)
    # Two fills (BUY + SELL), each charged a flat 1.23.
    assert result.orders_filled == 2
    assert result.total_commission == Decimal("2.46")


def test_engine_execution_assumption_overrides_slippage() -> None:
    # total_adverse_rate 0.05 replaces config slippage: BUY@110 -> 115.5, SELL@120 -> 114.
    execution = ExecutionAssumptions(
        slippage_rate=Decimal("0.05"), half_spread_rate=Decimal("0"), impact_rate=Decimal("0")
    )
    config = BacktestConfig(
        quantity=10, commission_rate=Decimal("0"), commission_fixed=Decimal("0"),
        slippage_rate=Decimal("0"), execution=execution,
    )
    result = BacktestEngine().run(build_profitable_series(_future()), _strategy(), config)
    assert result.total_pnl == Decimal("-15")  # (114 - 115.5) * 10


def test_cost_schedule_and_execution_compose_with_legacy_backwards_compat() -> None:
    # A config without the new fields behaves exactly as before.
    legacy = BacktestConfig(quantity=10, commission_rate=Decimal("0.0003"), slippage_rate=Decimal("0.001"))
    plain = BacktestConfig(quantity=10, commission_rate=Decimal("0.0003"), slippage_rate=Decimal("0.001"),
                           cost_schedule=None, execution=None)
    a = BacktestEngine().run(build_profitable_series(_future()), _strategy(), legacy)
    b = BacktestEngine().run(build_profitable_series(_future()), _strategy(), plain)
    assert a.total_commission == b.total_commission
    assert a.total_pnl == b.total_pnl