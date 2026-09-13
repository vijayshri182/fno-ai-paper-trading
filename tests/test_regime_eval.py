"""Tests for the WS 7.6 regime-aware evaluation module.

Pure math is tested with synthetic recorded trades; a reconciliation test that
needs the git-ignored recorded champion replay is skipped when the file is
absent (offline/CI safety).
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from fno_ai_paper_trading.evaluation.regime_eval import (
    HYPOTHESES,
    GroupStats,
    RecordedTrade,
    compute_stats,
    evaluate_recorded_regime_hypotheses,
    group_stats,
    load_recorded_trades,
    long_entry_count,
    residual_after_holding,
    split_protected,
    trend_group,
    volatility_group,
)


def _t(
    net: str,
    regime: str = "sideways_normal",
    side: str = "SELL",
    entry: str = "2022-01-04T09:15:00",
) -> RecordedTrade:
    value = Decimal(net)
    return RecordedTrade(
        entry_time=entry,
        exit_time="2022-01-04T09:20:00",
        side=side,
        quantity="1",
        entry_price="100.0",
        exit_price="100.0",
        entry_regime=regime,
        exit_regime=regime,
        holding_bars="1",
        price_pnl=value,
        commission=Decimal("0"),
        net_pnl=value,
    )


def test_trend_and_volatility_grouping():
    assert trend_group("up_high") == "up"
    assert volatility_group("sideways_low") == "low"
    assert volatility_group("unknown") == "unknown"


def test_split_protected_partition_and_boundary():
    safe = [_t("1.0", entry="2025-12-31T09:15:00") for _ in range(3)]
    oos = [_t("2.0", entry="2026-01-01T10:15:00") for _ in range(4)]
    before_boundary = [_t("3.0", entry="2025-12-31T15:20:00")]
    trades = safe + oos + before_boundary
    safe_out, oos_out, total = split_protected(trades)
    assert total == 8
    assert len(safe_out) == 4
    assert len(oos_out) == 4
    assert all(t.entry_time >= "2026-01-01" for t in oos_out)
    assert all(t.entry_time < "2026-01-01" for t in safe_out)


def test_compute_stats_math():
    trades = [_t("10"), _t("-5"), _t("10"), _t("-5")]
    stats = compute_stats(trades, "g")
    assert stats.count == 4
    assert stats.winning == 2
    assert stats.losing == 2
    assert stats.win_rate_pct == "50.0000"
    assert stats.net_pnl == "10.00"
    assert stats.expectancy == "2.50"


def test_compute_stats_empty():
    stats = compute_stats([], "empty")
    assert stats.count == 0
    assert stats.winning == 0
    assert stats.losing == 0
    assert stats.win_rate_pct == "0.0000"
    assert stats.net_pnl == "0.00"
    assert stats.expectancy == "0.00"


def test_group_stats_partition_and_order():
    trades = [
        _t("1", regime="sideways_low"),
        _t("2", regime="down_high"),
        _t("3", regime="sideways_low"),
        _t("4", regime="up_normal"),
    ]
    rows = group_stats(trades, lambda t: trend_group(t.entry_regime))
    labels = [r.label for r in rows]
    assert labels == ["sideways", "down", "up"]
    sideways = rows[0]
    assert sideways.count == 2
    assert sideways.net_pnl == "4.00"
    assert sideways.expectancy == "2.00"
    assert sum(r.count for r in rows) == 4


def test_long_entry_count():
    trades = [_t("1", side="SELL"), _t("2", side="BUY"), _t("3", side="SELL")]
    assert long_entry_count(trades) == 1
    assert long_entry_count(trades[:1]) == 0


def test_residual_after_holding():
    trades = [
        _t("1", regime="sideways_low"),
        _t("2", regime="down_low"),
        _t("3", regime="sideways_high"),
        _t("-10", regime="down_high"),
    ]
    residual, removed = residual_after_holding(trades, frozenset({"sideways"}))
    assert removed == 2
    assert residual.count == 2
    assert residual.expectancy == "-4.00"
    assert residual.net_pnl == "-8.00"


def test_residual_holding_all_is_empty():
    trades = [_t("1", regime="sideways_low"), _t("2", regime="down_low")]
    residual, removed = residual_after_holding(
        trades, frozenset({"sideways", "down", "up"})
    )
    assert removed == 2
    assert residual.count == 0
    assert residual.net_pnl == "0.00"


def test_baseline_equal_to_up_hold_gate_with_no_up_entries():
    trades = [_t("1", regime="sideways_low"), _t("-2", regime="down_low")]
    baseline = compute_stats(trades, "baseline")
    residual, removed = residual_after_holding(trades, frozenset({"up"}))
    assert removed == 0
    assert residual.count == baseline.count
    assert residual.net_pnl == baseline.net_pnl
    assert residual.expectancy == baseline.expectancy
    assert residual.win_rate_pct == baseline.win_rate_pct


def test_evaluate_structure_and_oos_exclusion():
    trades = [_t("1", regime="sideways_low") for _ in range(3)] + [
        _t("9", regime="down_high", entry="2026-02-01T09:15:00")
    ]
    evaluation = evaluate_recorded_regime_hypotheses_from(trades)
    assert evaluation.total_trades == 4
    assert evaluation.safe_trades == 3
    assert evaluation.protected_separated == 1
    assert evaluation.long_entries_safe == 0
    assert evaluation.baseline.count == 3
    assert sum(r.count for r in evaluation.by_regime) == 3
    assert len(evaluation.gates) == 4
    for label, _stats, _removed in evaluation.gates:
        assert _stats.count <= 3


def test_hypotheses_registry_complete():
    ids = {h.hypothesis_id for h in HYPOTHESES}
    assert ids == {"R1", "R2", "R3", "R4", "R5"}
    for hypothesis in HYPOTHESES:
        assert hypothesis.status in (
            "structural_no_op",
            "rejected",
            "not_testable_recorded",
            "verified",
        )
        assert hypothesis.verdict
        assert isinstance(hypothesis.verified, bool)
    by_id = {h.hypothesis_id: h for h in HYPOTHESES}
    assert by_id["R1"].status == "structural_no_op"
    assert by_id["R2"].status == "rejected"
    assert by_id["R3"].status == "rejected"
    assert by_id["R4"].status == "not_testable_recorded"
    assert by_id["R5"].status == "not_testable_recorded"


def evaluate_recorded_regime_hypotheses_from(trades):
    import json
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "trades.csv"
        fieldnames = [
            "entry_time",
            "exit_time",
            "side",
            "quantity",
            "entry_price",
            "exit_price",
            "entry_regime",
            "exit_regime",
            "holding_bars",
            "price_pnl",
            "commission",
            "net_pnl",
        ]
        with open(path, "w", encoding="utf-8", newline="") as handle:
            import csv

            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            for trade in trades:
                writer.writerow(
                    {
                        "entry_time": trade.entry_time,
                        "exit_time": trade.exit_time,
                        "side": trade.side,
                        "quantity": trade.quantity,
                        "entry_price": trade.entry_price,
                        "exit_price": trade.exit_price,
                        "entry_regime": trade.entry_regime,
                        "exit_regime": trade.exit_regime,
                        "holding_bars": trade.holding_bars,
                        "price_pnl": str(trade.price_pnl),
                        "commission": str(trade.commission),
                        "net_pnl": str(trade.net_pnl),
                    }
                )
        del json
        return evaluate_recorded_regime_hypotheses(str(path))


def test_record_and_round_trip_synthetic():
    trades = [
        _t("1", regime="sideways_low"),
        _t("-2", regime="down_high", entry="2026-01-02T09:15:00"),
        _t("3", regime="sideways_low"),
    ]
    evaluation = evaluate_recorded_regime_hypotheses_from(trades)
    assert evaluation.safe_trades == 2
    assert evaluation.protected_separated == 1
    assert evaluation.by_regime[0].label == "sideways_low"


@pytest.mark.skipif(
    not __import__("pathlib").Path(
        "reports/model_performance/trades.csv"
    ).exists(),
    reason="recorded champion replay not present (git-ignored offline artifact)",
)
def test_recorded_reconciliation():
    evaluation = evaluate_recorded_regime_hypotheses(
        "reports/model_performance/trades.csv"
    )
    assert evaluation.total_trades == 2601
    assert evaluation.safe_trades == 2216
    assert evaluation.protected_separated == 385
    assert evaluation.long_entries_total == 0
    assert evaluation.baseline.net_pnl == "-121332.89"
    assert evaluation.baseline.expectancy == "-54.75"
    assert sum(r.count for r in evaluation.by_regime) == 2216
    assert sum(Decimal(r.net_pnl) for r in evaluation.by_regime) == Decimal(
        "-121332.89"
    )
    assert sum(r.count for r in evaluation.by_trend) == 2216
    assert sum(r.count for r in evaluation.by_volatility) == 2216


def test_load_recorded_trades_parses_decimals():
    trades = [_t("1.25", regime="up_low")]
    import csv
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "trades.csv"
        with open(path, "w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(
                [
                    "entry_time",
                    "exit_time",
                    "side",
                    "quantity",
                    "entry_price",
                    "exit_price",
                    "entry_regime",
                    "exit_regime",
                    "holding_bars",
                    "price_pnl",
                    "commission",
                    "net_pnl",
                ]
            )
            writer.writerow(
                [
                    "2022-01-04T09:15:00",
                    "2022-01-04T09:20:00",
                    "SELL",
                    "1",
                    "100.0",
                    "99.0",
                    "up_low",
                    "up_low",
                    "1",
                    "1.25",
                    "0.0",
                    "1.25",
                ]
            )
        loaded = load_recorded_trades(str(path))
    assert len(loaded) == 1
    assert loaded[0].net_pnl == Decimal("1.25")
    assert trend_group(loaded[0].entry_regime) == "up"