"""Tests for the family competition classifier (evaluation/family_competition.py).

Focus: A/B/C/D/E correctness, the absolute rule that a least-negative
strategy is never the winner, preregistered drops, and 21-criteria coverage.
"""
from __future__ import annotations

from fno_ai_paper_trading.evaluation.family_competition import (
    CompetitionEntry,
    best_tested,
    build_competition,
    classify,
    criteria,
    family_leaderboard,
    label_text,
    oos_net,
    oos_return,
    oos_t,
    oos_trades,
)


def _entry(
    strategy_id="s",
    family="TREND_FOLLOWING",
    oos_net_pnl=None,
    oos_trades=None,
    oos_t_value=None,
    gate=None,
    design_net="-100",
    validation_net="-80",
    **extra,
) -> CompetitionEntry:
    protected = {}
    if oos_net_pnl is not None:
        protected = {
            "net_pnl": oos_net_pnl,
            "net_return_pct": oos_net_pnl,
            "num_trades": oos_trades or 100,
            "win_rate_pct": "30",
            "profit_factor": "0.9",
            "transaction_costs": "123",
            "per_trade_cost": "1.23",
            "max_drawdown_pct": "20",
            "expectancy_net": oos_net_pnl,
            "avg_win": "50",
            "avg_loss": "-50",
            "exposure_pct": "95",
            "reconciliation_ok": True,
            "end": "2026-09-11T15:25:00",
        }
    oos_per_trade = {}
    if oos_t_value is not None:
        oos_per_trade = {"mean": "1.0", "t": oos_t_value, "trades": oos_trades or 100}
    return CompetitionEntry(
        strategy_id=strategy_id,
        strategy_family=family,
        strategy_name=strategy_id,
        version="2.0.0",
        configuration_version="abc",
        design={"net_pnl": design_net},
        validation={"net_pnl": validation_net},
        protected_oos=protected,
        oos_per_trade=oos_per_trade,
        preregistered_note=extra.get("note", ""),
        recorded_gate_decision=gate,
    )


def test_negative_oos_is_rejected():
    assert classify(_entry(oos_net_pnl="-10")) == "E"


def test_zero_oos_is_rejected():
    assert classify(_entry(oos_net_pnl="0")) == "E"


def test_no_oos_is_insufficient_data():
    assert classify(_entry()) == "D"


def test_too_few_oos_trades_is_insufficient_data():
    entry = _entry(oos_net_pnl="100", oos_trades=5, oos_t_value="3")
    assert classify(entry) == "D"


def test_positive_oos_with_high_t_is_promotable():
    entry = _entry(oos_net_pnl="10000", oos_trades=120, oos_t_value="2.5")
    assert classify(entry) == "C"


def test_positive_oos_with_low_t_is_credible_candidate():
    entry = _entry(oos_net_pnl="10000", oos_trades=120, oos_t_value="1.2")
    assert classify(entry) == "B"


def test_recorded_reject_wins_over_data():
    entry = _entry(
        oos_net_pnl="10000", oos_trades=120, oos_t_value="3.0", gate="REJECT"
    )
    assert classify(entry) == "E"


def test_least_negative_is_never_best_tested():
    entries = [
        _entry("a", oos_net_pnl="-1000", oos_trades=100, oos_t_value="-2"),
        _entry("b", oos_net_pnl="-10", oos_trades=100, oos_t_value="-0.5"),
    ]
    assert best_tested(entries) is None
    tiers = [label_text(classify(e)) for e in entries]
    assert all("E REJECTED" in t for t in tiers)
    assert all("BEST TESTED" not in t for t in tiers)


def test_best_tested_only_with_b_or_c():
    entries = [
        _entry("a", oos_net_pnl="-10", oos_trades=100, oos_t_value="-1"),
        _entry("b", oos_net_pnl="5000", oos_trades=100, oos_t_value="2.9"),
    ]
    top = best_tested(entries)
    assert top is not None
    assert top["strategy_id"] == "b"
    assert top["tier"] == "A"
    assert "BEST TESTED" in top["tier_label"]


def test_label_text_mapping():
    assert label_text("A") == "A BEST TESTED"
    assert label_text("B") == "B CREDIBLE CANDIDATE"
    assert label_text("C") == "C PROMOTABLE"
    assert label_text("D") == "D INSUFFICIENT DATA"
    assert label_text("E") == "E REJECTED"


def test_criteria_has_expected_fields():
    entry = _entry("s", oos_net_pnl="-50", oos_trades=100, oos_t_value="-2")
    row = criteria(entry)
    for key in (
        "oos_net_pnl", "oos_return_pct", "expectancy_per_trade", "profit_factor",
        "win_rate_pct", "trade_count", "max_drawdown_pct", "avg_win_pct",
        "avg_loss_pct", "per_trade_t", "transaction_costs", "per_trade_cost",
        "perturbation_robustness", "oos_cost_scan_rows", "reconciliation_ok",
        "risk_violations", "design_validation_stability", "active_version_at_oos",
        "regime_consistency", "data_thru", "exposure_pct",
    ):
        assert key in row
    assert row["trade_count"] == 100
    assert row["per_trade_t"] == "-2"


def test_leaderboard_groups_by_family_and_ranks():
    entries = [
        _entry("a", family="MOMENTUM", oos_net_pnl="-5", oos_trades=50, oos_t_value="-1"),
        _entry("b", family="TREND_FOLLOWING", oos_net_pnl="-50", oos_trades=60, oos_t_value="-2"),
        _entry("c", family="TREND_FOLLOWING"),
    ]
    board = family_leaderboard(entries)
    assert set(board) == {"MOMENTUM", "TREND_FOLLOWING"}
    assert len(board["TREND_FOLLOWING"]) == 2


def test_preregistered_drop_is_even_without_oos():
    entry = _entry("c5", gate="rejected")
    assert classify(entry) == "E"


def test_oos_accessors():
    entry = _entry(oos_net_pnl="-50", oos_trades=100, oos_t_value="-2")
    assert oos_net(entry) == -50
    assert oos_trades(entry) == 100
    assert oos_t(entry) == -2
    assert abs(oos_return(entry)) > 0


def test_build_competition_integration_with_synthetic_artifacts():
    from fno_ai_paper_trading.strategies.registry import StrategyRegistry
    from fno_ai_paper_trading.strategies.spec import StrategySpec, TREND_FOLLOWING

    spec = StrategySpec(
        strategy_id="moving_average_cross", family=TREND_FOLLOWING,
        strategy_name="moving_average_cross", version="1", parameters={"fast": 5, "slow": 21},
    )
    registry = StrategyRegistry(specs=[spec])
    recorded = {
        "champion_design": {"net_pnl": "-100"},
        "champion_validation": {"net_pnl": "-80"},
        "candidates": [],
    }
    oos_confirmation = {
        "bar_counts": {"protected_oos": 1000},
        "verdicts": {
            "other": {
                "gate_credible_positive_oos": {
                    "decision": "REJECT",
                    "evidence": {
                        "out_of_sample": {
                            "champion_net_pnl": "-21759.0",
                            "champion_max_drawdown_pct": "21.8",
                            "period": "out_of_sample",
                        }
                    },
                },
                "protected_oos": {"net_pnl": "-5", "num_trades": "9"},
            }
        },
    }
    comp = build_competition(registry, recorded, oos_confirmation, champion_oos_trades=384)
    assert comp["best_tested_present"] is False
    champ_row = next(r for r in comp["leaderboard"]["TREND_FOLLOWING"] if r["strategy_id"] == "moving_average_cross")
    assert champ_row["tier"] == "E"
    assert abs(float(champ_row["oos_net_pnl"]) + 21759.0) < 1e-6