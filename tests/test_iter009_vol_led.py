"""Tests for ITERATION 009 - VOL-LED STRUCTURAL HYPOTHESIS (pre-OOS A/B).

Pure-logic + artifact-integrity tests only.  No engine re-run for tuning.
"""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path

from fno_ai_paper_trading.research.iteration009_vol_led import (
    FINGERPRINT_SHA256,
    ITER6_RESULT_SHA256,
    ITER7_RESULT_SHA256,
    ITER8_RESULT_SHA256,
    OUT_FILE,
    _hold_bucket,
    _sha256,
    classify_candidate,
    economic_identity,
    engine_identity,
    trade_statistics,
)

REPO = Path(__file__).resolve().parents[1]
R = REPO / "runs" / "research" / "day_batch"


def _load():
    return json.loads(OUT_FILE.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# provenance guards
# ---------------------------------------------------------------------------


def test_iteration_provenance_artifacts_unchanged():
    pairs = [
        (R / "iteration_006_n3_fingerprint.json", FINGERPRINT_SHA256),
        (R / "iteration_006_protected_oos_n3.json", ITER6_RESULT_SHA256),
        (R / "iteration_007_robustness_audit_n3.json", ITER7_RESULT_SHA256),
        (R / "iteration_008_component_regime_attribution_n3.json", ITER8_RESULT_SHA256),
    ]
    for p, h in pairs:
        assert p.exists(), p
        assert _sha256(p) == h, p.name


def test_iter005_benchmark_source_bytes_kept():
    p = R / "iteration_005_economic_discovery.json"
    assert p.exists()


# ---------------------------------------------------------------------------
# A/B experiment guards
# ---------------------------------------------------------------------------


def test_artifact_has_expected_experiment_fields():
    d = _load()
    assert d["experiment"] == "ITERATION_009_VOL_LED_STRUCTURAL_HYPOTHESIS"
    method = d["method"]
    assert method["single_controlled_experiment"] is True
    assert method["structural_toggle_only"] is True
    assert method["no_parameter_optimization"] is True
    assert method["no_oos_for_selection"] is True


def test_firewall_and_research_domain_shape():
    d = _load()
    fw = d["protected_oos_firewall"]
    assert fw["window"] == ["2025-10-06", "2026-09-11"]
    assert fw["used_for_selection"] is False
    assert fw["oos_result_immutable"]["net"] == "+1710.004256155"
    rd = d["research_domain"]
    assert rd["window"] == ["2022-01-03", "2025-10-03"]
    assert rd["bars"] == 69781
    assert rd["days"] == 932


def test_benchmark_guard_equal_iteration005():
    d = _load()
    assert d["benchmark_frozen"]["benchmark_guard_equal_iteration005"] is True
    a = d["economics"]["A_benchmark"]
    assert a["round_trips"] == 108 and a["fills"] == 216
    assert a["total_pnl"] == "6648.798750230"
    assert a["gross_close_edge"] == "12534.10000"
    assert a["max_drawdown"] == "978.320204825"
    assert float(a["win_rate_pct"]) == 60.19


def test_candidate_defines_single_isolated_change():
    d = _load()
    c = d["candidate_definition"]
    assert "remove trend-level + trend-slope gating" in c["isolated_change"]
    assert "VOL_GATE" in c["isolated_change"]
    assert c["no_workaround"] is True
    for item in ("exits (provider ATR stop / max-hold / confluence-flat)",
                 "position sizing", "costs", "execution model", "causal state handling"):
        assert item in c["retained"]


def test_safety_state_unchanged():
    s = _load()["safety_state"]
    assert s["promotion"] == "NO"
    assert s["algo_ready"] == "NO"
    assert s["algorithm_health"] == "RED"
    assert s["scope.live_trading"] is False
    assert s["live_gate"] == "CLOSED"
    assert s["paper_only"] is True
    assert s["human_approval_required"] is True


# ---------------------------------------------------------------------------
# economics and identity
# ---------------------------------------------------------------------------


def test_a_and_b_economic_identity_hold():
    d = _load()
    for key in ("A", "B"):
        ident = d["economic_identity"][key]
        assert ident["closed_identity_holds"] is True, key
        assert ident["full_identity_holds"] is True, key
    assert d["economic_identity"]["B"]["open_at_close"] == 1


def test_comparison_metrics_consistent():
    d = _load()
    comp = d["comparison"]
    assert Decimal(comp["B"]["net"]) > Decimal(comp["A"]["net"])
    assert d["trade_statistics"]["B"]["round_trips"] == 226
    assert d["trade_statistics"]["A"]["round_trips"] == 108


def test_pre_registered_classification_result():
    d = _load()
    cls = d["classification"]
    assert set(cls["pre_registered_rules"]) == {
        "promising_min_net_ratio", "promising_coverage", "promising_dd",
        "promising_winrate_floor", "promising_loss_cap", "promising_pf_floor",
        "not_supported_net", "not_supported_loss", "not_supported_dd"}
    assert cls["result"] in ("PROMISING", "NOT_SUPPORTED", "INCONCLUSIVE")


def test_prohibited_conclusion_words_absent():
    cls = _load()["classification"]["result"]
    assert cls not in ("BEST", "WINNER", "ROBUST", "PRODUCTION_READY")
    assert "BEST" not in cls and "WINNER" not in cls


# ---------------------------------------------------------------------------
# statefulness trace
# ---------------------------------------------------------------------------


def test_trace_shows_vol_gating_only_in_b():
    d = _load()
    trace_a = d["statefulness"]["trace_A"]
    trace_b = d["statefulness"]["trace_B"]
    assert len(trace_a) == 108 and len(trace_b) == 226
    side_map = {"LONG": 1, "SHORT": -1}
    a_ok = all(r["raw_underlying_volmove"] == side_map[r["side"]]
               and r["raw_trend"] == side_map[r["side"]] for r in trace_a)
    b_ok = all(r["raw_underlying_volmove"] == side_map[r["side"]] for r in trace_b)
    b_vol_signed = all(r["vol_gate_state"] == "ACTIVE_RETAINED" for r in trace_b)
    assert a_ok and b_ok and b_vol_signed
    div = d["statefulness"]["divergence"]
    assert div["a_entries"] == 108 and div["b_entries"] == 226
    assert div["common_same_bar_same_side"] == 79


def test_path_divergence_not_assumed_one_to_one():
    div = _load()["statefulness"]["divergence"]
    assert div["a_only"] > 0 and div["b_only"] > 0


# ---------------------------------------------------------------------------
# pure helper logic
# ---------------------------------------------------------------------------


def test_hold_bucket_logic():
    assert _hold_bucket(1) == "intraday_or_1d"
    assert _hold_bucket(2) == "2d_5d"
    assert _hold_bucket(5) == "2d_5d"
    assert _hold_bucket(6) == "6d_15d"
    assert _hold_bucket(15) == "6d_15d"
    assert _hold_bucket(16) == "16d_25d"
    assert _hold_bucket(25) == "16d_25d"


def test_classify_candidate_branches():
    base = {
        "A": {"net": Decimal("1000"), "gross_loss": Decimal("300"),
              "max_dd": Decimal("100"), "coverage": 150.0, "win_rate": 60.0, "pf": 5.0},
        "B": {"net": Decimal("1000"), "gross_loss": Decimal("300"),
              "max_dd": Decimal("100"), "coverage": 150.0, "win_rate": 60.0, "pf": 5.0},
    }
    assert classify_candidate(base) == "NOT_SUPPORTED"  # net <= benchmark net
    better = {
        "A": {"net": Decimal("1000"), "gross_loss": Decimal("300"),
              "max_dd": Decimal("100"), "coverage": 150.0, "win_rate": 60.0, "pf": 5.0},
        "B": {"net": Decimal("1300"), "gross_loss": Decimal("400"),
              "max_dd": Decimal("150"), "coverage": 160.0, "win_rate": 58.0, "pf": 5.0},
    }
    assert classify_candidate(better) == "PROMISING"
    worse = {
        "A": {"net": Decimal("1000"), "gross_loss": Decimal("300"),
              "max_dd": Decimal("100"), "coverage": 150.0, "win_rate": 60.0, "pf": 5.0},
        "B": {"net": Decimal("800"), "gross_loss": Decimal("700"),
              "max_dd": Decimal("400"), "coverage": 120.0, "win_rate": 40.0, "pf": 2.0},
    }
    assert classify_candidate(worse) == "NOT_SUPPORTED"


def test_economic_identity_helper_precision():
    block = {
        "gross_close_edge": "12534.10000", "slippage": "4527.15770",
        "commission": "1358.143549770", "net_closed_rts": "6648.798750230",
        "carry_mtm": "0.00000", "open_at_close": 0, "total_pnl": "6648.798750230",
        "reconcile": {"reconcile_all": True, "economic_identity": True, "reconcile_note": None},
    }
    ident = economic_identity(block)
    assert ident["closed_identity_holds"] is True
    assert ident["full_identity_holds"] is True


def test_engine_identity_matches_engine_accounting():
    class R:
        gross_profit = Decimal("14493.0")
        gross_loss = Decimal("-1958.9")
        slippage_cost = Decimal("0")
        total_commission = Decimal("468.6400")
        transaction_costs = Decimal("468.6400")
        total_pnl = Decimal("12065.4600000")

    e = engine_identity(R())
    resid = Decimal(e["residual_total_pnl_minus_sum"])
    assert resid == -R.total_commission


def test_trade_statistics_shape():
    rts = [
        {"entry_day": "2022-01-03", "net": Decimal("100"), "gross_close": Decimal("120"),
         "hold_minutes": 1440, "realized": Decimal("100.5")},
        {"entry_day": "2022-01-04", "net": Decimal("-40"), "gross_close": Decimal("30"),
         "hold_minutes": 2880, "realized": Decimal("-39")},
    ]
    s = trade_statistics(rts, research_days=10)
    assert s["round_trips"] == 2
    assert s["wins"] == 1 and s["losses"] == 1
    assert s["active_days"] == 2 and s["inactive_days"] == 8
    assert s["expectancy_net_per_rt"] == 30.0
    assert s["median_hold_minutes"] == 2160.0