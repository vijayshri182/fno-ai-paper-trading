"""Tests for the machine-readable scoreboard (evaluation/scoreboard.py).

Builds the scoreboard from *synthetic* recorded artifacts and checks the
structure, the champion row, insufficient-data handling, research-allocation
and ensemble contracts.  A reconciliation test against the real recorded
artifacts is skipped when they are absent (offline/CI safety).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from fno_ai_paper_trading.evaluation.scoreboard import (
    DEFAULT_PATHS,
    build_scoreboard,
    compact_for_state,
    load_scoreboard,
    write_scoreboard,
)
from fno_ai_paper_trading.strategies.registry import discover


@pytest.fixture
def recorded(tmp_path: Path) -> dict[str, Path]:
    candidates_eval = tmp_path / "candidates_eval.json"
    candidates_eval.write_text(
        json.dumps({
            "schema_version": "1",
            "split_dates": {"design_end": "2025-07-01T00:00:00", "validation_end": "2026-01-01T00:00:00"},
            "champion_design": {"net_pnl": "-100", "num_trades": "100"},
            "champion_validation": {"net_pnl": "-80", "num_trades": "90"},
            "candidates": [
                {
                    "candidate": "c2_slow_long_only_ma_cross",
                    "design": {"net_pnl": "-90"},
                    "validation": {"net_pnl": "-70"},
                    "params": {"fast": 20, "slow": 50},
                    "rationale": "slow long-only",
                }
            ],
        }),
        encoding="utf-8",
    )
    oos = tmp_path / "oos_confirmation.json"
    oos.write_text(
        json.dumps({
            "bar_counts": {"protected_oos": 1000},
            "verdicts": {
                "c2_slow_long_only_ma_cross": {
                    "protected_oos": {"net_pnl": "-6000", "num_trades": "130", "max_drawdown_pct": "6.9"},
                    "oos_per_trade_slippage_adj_pre_commission": {"mean": "-50", "t": "-2.0", "trades": 130},
                    "gate_credible_positive_oos": {
                        "decision": "REJECT",
                        "evidence": {
                            "out_of_sample": {
                                "champion_net_pnl": "-21759",
                                "champion_max_drawdown_pct": "21.8",
                                "period": "out_of_sample",
                            }
                        },
                    },
                }
            },
        }),
        encoding="utf-8",
    )
    ledger = tmp_path / "trade_ledger.json"
    ledger.write_text(
        json.dumps({
            "versions": {"algorithm_version": "v1-baseline-ma521", "configuration_version": "v1-paper-defaults"},
            "cross_checks": {"oos_trade_count": 384},
        }),
        encoding="utf-8",
    )
    return {"candidates_eval": candidates_eval, "oos_confirmation": oos, "trade_ledger": ledger}


def test_build_scoreboard_structure(recorded):
    registry = discover()
    board = build_scoreboard(registry, recorded)
    assert board["schema_version"] == "1"
    assert board["deliverable"] == "algorithm_research_scoreboard"
    assert board["champion"]["strategy_id"] == "moving_average_cross"
    assert len(board["registry_catalog"]) == 7
    assert set(board["families"]) == {"TREND_FOLLOWING", "MOMENTUM", "BREAKOUT", "REGIME_SWITCHING"}
    comp = board["competition"]
    assert comp["best_tested_present"] is False
    assert comp["best_tested"] is None


def test_champion_oos_attached_from_evidence(recorded):
    registry = discover()
    board = build_scoreboard(registry, recorded)
    comp = board["competition"]
    champ = comp["criteria"]["moving_average_cross"]
    assert champ["oos_net_pnl"] == "-21759"
    assert champ["trade_count"] == 384
    champ_lb = next(r for r in comp["leaderboard"]["TREND_FOLLOWING"] if r["strategy_id"] == "moving_average_cross")
    assert champ_lb["tier"] == "E"


def test_insufficient_data_labels_when_artifacts_missing(tmp_path):
    registry = discover()
    empty = {"candidates_eval": tmp_path / "missing.json", "oos_confirmation": tmp_path / "missing2.json", "trade_ledger": tmp_path / "missing3.json"}
    board = build_scoreboard(registry, empty)
    comp = board["competition"]
    assert comp["best_tested_present"] is False
    assert "moving_average_cross" in board["insufficient_data"]
    assert board["insufficient_data"]["moving_average_cross"] == "protected OOS not consumed; cannot rank"


def test_research_allocation_is_equal_not_capital():
    board = build_scoreboard(discover(), {
        "candidates_eval": Path("does-not-exist.json"),
        "oos_confirmation": Path("does-not-exist.json"),
        "trade_ledger": Path("does-not-exist.json"),
    })
    alloc = board["research_allocation"]
    assert alloc["model_equal_allocation"] is True
    assert "never a capital allocation" in alloc["note"]


def test_ensemble_is_not_deployed():
    board = build_scoreboard(discover(), {
        "candidates_eval": Path("does-not-exist.json"),
        "oos_confirmation": Path("does-not-exist.json"),
        "trade_ledger": Path("does-not-exist.json"),
    })
    status = board["ensemble_status"]["status"]
    assert status == "ARCHITECTURE_ONLY_NOT_DEPLOYED"


def test_compact_for_state(recorded):
    registry = discover()
    board = build_scoreboard(registry, recorded)
    compact = compact_for_state(board)
    assert compact["current_champion"] == "moving_average_cross"
    assert compact["champion_family"] == "TREND_FOLLOWING"
    assert compact["best_tested_present"] is False
    assert compact["research_allocation"] is True
    assert compact["ensemble_status"] == "ARCHITECTURE_ONLY_NOT_DEPLOYED"


def test_write_and_load(tmp_path):
    board = build_scoreboard(discover(), {
        "candidates_eval": Path("does-not-exist.json"),
        "oos_confirmation": Path("does-not-exist.json"),
        "trade_ledger": Path("does-not-exist.json"),
    })
    path = tmp_path / "sb.json"
    write_scoreboard(path, board)
    loaded = load_scoreboard(path)
    assert loaded["champion"]["strategy_id"] == "moving_average_cross"


def test_recorded_reconciliation(recorded):
    """Point the build at the real recorded artifacts (skipped offline)."""
    real = {k: Path(v) for k, v in DEFAULT_PATHS.items()}
    if not (real["candidates_eval"].exists() and real["oos_confirmation"].exists()):
        pytest.skip("recorded artifacts not present (offline)")
    board = build_scoreboard(discover(), {**recorded, **real})
    comp = board["competition"]
    assert comp["best_tested_present"] is False
    assert comp["headline"]
    champ_lb = next(r for r in comp["leaderboard"]["TREND_FOLLOWING"] if r["strategy_id"] == "moving_average_cross")
    assert champ_lb["tier"] == "E"
    assert float(champ_lb["oos_net_pnl"]) < 0