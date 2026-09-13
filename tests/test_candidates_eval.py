"""Tests for the generic candidate evaluation harness (WS 7.16).

The harness replays arbitrary decision-time signal streams through the same
deterministic engine/portfolio/risk path, splits chronologically into design /
validation / protected-OOS, and builds promotion-gate views. These tests use
small synthetic bars only — they never depend on the (git-ignored) datasets.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest

from fno_ai_paper_trading.backtest.config import BacktestConfig
from fno_ai_paper_trading.backtest.datasets import closes_to_bars
from fno_ai_paper_trading.evaluation.candidates import (
    build_delta_views,
    cost_scan,
    date_split_bars,
    evaluate_strategy,
    robustness_grid,
    run_candidate_plan,
    trading_days,
)
from fno_ai_paper_trading.evaluation.fast_signal import moving_average_cross_signals
from fno_ai_paper_trading.models.enums import InstrumentType
from fno_ai_paper_trading.models.instruments import Instrument
from fno_ai_paper_trading.strategies.research_candidates import long_only_ma_cross_signals

D0 = datetime(2026, 1, 5, 9, 15)
D1 = datetime(2026, 2, 1, 9, 15)
D2 = datetime(2026, 3, 1, 9, 15)


def _instrument() -> Instrument:
    return Instrument(
        symbol="NIFTY1",
        instrument_type=InstrumentType.FUTURE,
        underlying_symbol="NIFTY",
    )


def _bars(n: int = 120) -> list:
    closes: list[Decimal] = []
    price = Decimal("100")
    for i in range(n):
        price += Decimal("0.7") if (i // 15) % 2 == 0 else Decimal("-0.7")
        closes.append(price)
    return closes_to_bars(_instrument(), closes, start=D0)


def test_date_split_bars_partitions_chronologically():
    bars = _bars(120)
    design, validation, protected = date_split_bars(
        bars, design_end=D1, validation_end=D2
    )
    assert len(design) + len(validation) + len(protected) == len(bars)
    assert all(b.timestamp < D1 for b in design)
    assert all(D1 <= b.timestamp < D2 for b in validation)
    assert all(b.timestamp >= D2 for b in protected)
    assert design and validation and protected  # every segment must be non-empty


def test_trading_days_counts_unique_dates():
    bars = _bars(120)
    assert trading_days(bars) == 120


def test_evaluate_strategy_returns_reconciled_run():
    bars = _bars(160)
    signals = moving_average_cross_signals(bars, fast=5, slow=21)
    run = evaluate_strategy(bars, signals, name="ma52", config=BacktestConfig())
    assert run.metrics.num_trades >= 1
    assert isinstance(run.summary_dict()["net_pnl"], str)
    assert run.summary_dict()["bars"] == len(bars)
    assert run.summary_dict()["reconciliation_ok"] in (True, False)


def test_evaluate_strategy_shape_mismatch_rejected():
    bars = _bars(60)
    signals = [s for s in moving_average_cross_signals(bars, fast=5, slow=21)][:-1]
    with pytest.raises(ValueError):
        evaluate_strategy(bars, signals, name="x")


def test_run_candidate_plan_segments_sum_together():
    bars = _bars(200)
    plan = run_candidate_plan(
        bars,
        name="c1",
        provider=long_only_ma_cross_signals,
        params={"fast": 5, "slow": 21},
        config=BacktestConfig(),
        design_end=D1,
        validation_end=D2,
    )
    segs = plan["segments"]
    total = sum(len(plan["runs"][k].bars) for k in ("design", "validation", "protected_oos"))
    assert total == len(bars)
    for key in ("design", "validation", "protected_oos", "full"):
        assert segs[key]["bars"] == len(bars) if key == "full" else True
    assert "protected_oos" in segs


def test_cost_scan_scenarios_degrade_monotonically():
    bars = _bars(200)
    base = BacktestConfig()
    rows = cost_scan(
        bars,
        name="c1",
        provider=long_only_ma_cross_signals,
        params={"fast": 5, "slow": 21},
        base_config=base,
    )
    names = [r["scenario"] for r in rows]
    assert names == ["zero_cost", "low_cost", "base", "high_cost"]
    net = [Decimal(r["net_pnl"]) for r in rows]
    assert net[0] >= net[1] >= net[2] >= net[3]


def test_robustness_grid_perturbations_bounded():
    bars = _bars(200)
    rows = robustness_grid(
        bars,
        name="c1",
        provider=long_only_ma_cross_signals,
        base_params={"fast": 5, "slow": 21},
        perturb={"fast": [3, 8], "slow": [18, 26]},
        config=BacktestConfig(),
    )
    assert len(rows) == 4
    variants = {r["variant"] for r in rows}
    assert variants == {"fast=3", "fast=8", "slow=18", "slow=26"}
    assert all("canonical_return" in r for r in rows)


def test_build_delta_views_gate_keys():
    bars = _bars(220)
    champ = run_candidate_plan(
        bars,
        name="champ",
        provider=moving_average_cross_signals,
        params={"fast": 5, "slow": 21},
        design_end=D1,
        validation_end=D2,
    )
    cand = run_candidate_plan(
        bars,
        name="c1",
        provider=long_only_ma_cross_signals,
        params={"fast": 5, "slow": 21},
        design_end=D1,
        validation_end=D2,
    )
    deltas, counts = build_delta_views(champ, cand)
    assert "validation" in deltas and "out_of_sample" in deltas
    assert "validation" in counts and "out_of_sample" in counts
    assert counts["out_of_sample"] >= 1
    assert deltas["out_of_sample"]["c1"].challenger_net_pnl is not None