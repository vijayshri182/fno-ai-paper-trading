"""Iteration-007 robustness-audit tests.

Discipline: these tests NEVER re-execute the protected-OOS engine (no second
look for tuning).  They validate (a) the recorded audit artifact and its
provenance/consistency guards, (b) the pre-registered verdict logic on synthetic
inputs, (c) the concentration / jackknife / execution-stress helpers on small
deterministic inputs, and (d) the frozen-candidate invariants (fingerprint +
Iter-006 result hashes unchanged).
"""
import hashlib
import json
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fno_ai_paper_trading.research.iteration005_discovery import EnsembleParams
from fno_ai_paper_trading.research.iteration007_robustness_audit import (
    FINGERPRINT_SHA256,
    FROZEN_PARAMS,
    FROZEN_WARMUP,
    ITER6_RESULT_SHA256,
    SLIPPAGE_MULTIPLIERS,
    audit_consistency,
    day_concentration,
    execution_stress,
    group_stats,
    hhi,
    jackknife_total,
    month_concentration,
    robustness_verdict,
    _sha256_file,
    _top_shares,
)

REPO = Path(__file__).resolve().parents[1]
RUNS = REPO / "runs" / "research" / "day_batch"
FINGERPRINT = RUNS / "iteration_006_n3_fingerprint.json"
ITER6 = RUNS / "iteration_006_protected_oos_n3.json"
AUDIT = RUNS / "iteration_007_robustness_audit_n3.json"

VERDICT_CODES = {
    "ROBUST_DISTRIBUTED", "CONCENTRATED_TRADES", "CONCENTRATED_DATES",
    "CONCENTRATED_SIDE", "CONCENTRATED_REGIME", "EXECUTION_FRAGILE",
    "STATISTICALLY_WEAK", "MIXED_CONCENTRATION",
}


def _load_js(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# provenance / freeze invariance
# ---------------------------------------------------------------------------


def test_provenance_hashes_are_unchanged() -> None:
    assert _sha256_file(FINGERPRINT) == FINGERPRINT_SHA256
    assert _sha256_file(ITER6) == ITER6_RESULT_SHA256


def test_frozen_params_still_match_code_defaults() -> None:
    p = EnsembleParams()
    assert {
        "fast": p.fast, "slow": p.slow, "slope_window": p.slope_window,
        "lookback": p.lookback, "stop_atr_mult": p.stop_atr_mult,
        "max_hold_days": p.max_hold_days,
    } == FROZEN_PARAMS
    assert p.slow + p.slope_window + p.lookback + 3 == FROZEN_WARMUP == 54
    assert SLIPPAGE_MULTIPLIERS == (0.0, 0.5, 1.0, 2.0, 3.0)


# ---------------------------------------------------------------------------
# audit artifact
# ---------------------------------------------------------------------------


def test_audit_artifact_complete_and_guarded() -> None:
    a = _load_js(AUDIT)
    assert a["experiment"] == "ITERATION_007_ROBUSTNESS_AUDIT_n3_ensemble"
    assert a["audit_only"] is True
    assert a["candidate_freeze"]["parameters"] == FROZEN_PARAMS
    assert a["iter6_guard"]["byte_sha256_matches"] is True
    assert a["iter6_guard"]["economics_reconstructed_identically"] is True
    assert a["method"]["single_controlled_run"] is True
    assert a["method"]["pipeline_identical_to_iteration_006"] is True
    assert a["domain"]["not_expanded"] is True
    assert a["domain"]["oos_bars"] == 17412
    assert a["domain"]["oos_trading_days"] == 233
    assert a["domain"]["oos_window"] == ["2025-10-06", "2026-09-11"]
    assert a["verdict"]["code"] in VERDICT_CODES
    assert set(a["verdict"]["criteria"]["failing"]) <= {
        "R1_trades", "R2_dates", "R3_sides", "R4_regimes",
        "R5_execution", "R6_statistics",
    }


def test_audit_verdict_recomputes_from_persisted_stats() -> None:
    a = _load_js(AUDIT)
    recomputed = robustness_verdict(a["robustness"])
    assert recomputed["code"] == a["verdict"]["code"]
    assert recomputed["criteria"] == a["verdict"]["criteria"]


def test_audit_reported_trade_table_reconciles() -> None:
    a = _load_js(AUDIT)
    rows = a["attribution"]["trade_table"]
    assert len(rows) == a["robustness"]["statistics"]["round_trips"]
    net_sum = round(sum(r["net"] for r in rows), 6)
    assert net_sum == a["robustness"]["net_total"]
    for r in rows:
        assert sum(r[k] for k in ("slippage", "commission", "net")) <= r["gross"] + 1e-6
        assert r["side"] in {"LONG", "SHORT"}
        assert r["win"] is (r["net"] > 0)


# ---------------------------------------------------------------------------
# pre-registered verdict (pure logic on synthetic inputs)
# ---------------------------------------------------------------------------


def _passing_stats() -> dict:
    return {
        "trade_concentrations": {"top3_share_net": 0.5, "net_ex_top3": 100.0},
        "day_concentrations": {"top3_days_share": 0.5, "net_ex_best_day": 100.0},
        "months": {"net_ex_best_month": 100.0},
        "sides": {"min_side_net": 100.0},
        "regimes": {"positive_regime_count": 3, "top_regime_share_net": 0.5},
        "vol_buckets": {"positive_bucket_count": 2},
        "execution": {"net_at_slip2x": 100.0, "break_even_extra_slippage_bps_per_fill": 10.0},
        "statistics": {"t_gross_per_rt": 3.9, "win_rate_pct": 64.52},
    }


def test_verdict_all_codes_mapped() -> None:
    assert robustness_verdict(_passing_stats())["code"] == "ROBUST_DISTRIBUTED"

    s = _passing_stats(); s["trade_concentrations"]["top3_share_net"] = 0.9
    assert robustness_verdict(s)["code"] == "CONCENTRATED_TRADES"

    s = _passing_stats(); s["day_concentrations"]["top3_days_share"] = 0.8
    assert robustness_verdict(s)["code"] == "CONCENTRATED_DATES"

    s = _passing_stats(); s["sides"]["min_side_net"] = -1.0
    assert robustness_verdict(s)["code"] == "CONCENTRATED_SIDE"

    s = _passing_stats(); s["regimes"]["top_regime_share_net"] = 0.95
    assert robustness_verdict(s)["code"] == "CONCENTRATED_REGIME"

    s = _passing_stats(); s["execution"]["net_at_slip2x"] = -10.0
    assert robustness_verdict(s)["code"] == "EXECUTION_FRAGILE"

    s = _passing_stats(); s["statistics"]["t_gross_per_rt"] = 1.0
    assert robustness_verdict(s)["code"] == "STATISTICALLY_WEAK"

    s = _passing_stats()
    s["trade_concentrations"]["top3_share_net"] = 0.9
    s["regimes"]["top_regime_share_net"] = 0.95
    v = robustness_verdict(s)
    assert v["code"] == "MIXED_CONCENTRATION"
    assert v["criteria"]["failing"] == ["R1_trades", "R4_regimes"]


def test_verdict_reports_failing_reasonably() -> None:
    s = _passing_stats(); s["execution"]["net_at_slip2x"] = -1.0
    v = robustness_verdict(s)
    assert v["reason"]  # non-empty reason
    assert "R5_execution" in v["criteria"]["failing"]


# ---------------------------------------------------------------------------
# pure helpers on deterministic inputs
# ---------------------------------------------------------------------------


def test_top_shares_and_hhi() -> None:
    vals = [100.0, 60.0, 40.0, 0.0]
    t3 = _top_shares(vals, 3)
    assert t3["top_share"] == 1.0
    assert t3["ex_top_share_amount"] == 0.0
    h = hhi(vals)
    assert 0 < h < 1
    flat = hhi([10.0, 10.0, 10.0])
    assert round(flat, 6) == round(1 / 3, 6)
    assert hhi([0.0, 0.0]) is None


def test_jackknife_total() -> None:
    jk = jackknife_total([100.0, 50.0, -20.0])
    assert jk["total"] == 130.0
    assert jk["drop_best"] == 30.0
    assert jk["drop_worst"] == 150.0
    assert jk["min_impact"] == 30.0
    assert jk["max_impact"] == 150.0


def test_group_and_day_concentration() -> None:
    rows = [
        {"exit_day": "2025-10-06", "net": 100.0, "gross": 120.0, "win": True},
        {"exit_day": "2025-10-06", "net": -50.0, "gross": -40.0, "win": False},
        {"exit_day": "2025-10-07", "net": 200.0, "gross": 220.0, "win": True},
    ]
    dc = day_concentration(rows)
    assert dc["relative_days"] == 2
    assert dc["best_day"] == "2025-10-07"
    assert dc["best_day_net"] == 200.0
    assert dc["net_ex_best_day"] == 50.0
    assert dc["active_positive_days"] == 2
    assert dc["top3_days_share"] == 1.0
    g = group_stats(rows, "exit_day", "day")
    assert {x["day"] for x in g} == {"2025-10-06", "2025-10-07"}
    assert g[0]["share_net_pct"] == 20.0  # 50/250
    assert g[1]["share_net_pct"] == 80.0


def test_month_concentration_split() -> None:
    rows = [
        {"exit_day": "2025-10-06", "net": 100.0},
        {"exit_day": "2026-03-31", "net": 50.0},
        {"exit_day": "2026-04-02", "net": 30.0},
    ]
    m = month_concentration(rows, "2026-04")
    assert m["month_count"] == 3
    assert m["positive_months"] == 3
    assert m["best_month"] == "2025-10"
    assert round(m["h1_net"], 6) == 150.0
    assert round(m["h2_net"], 6) == 30.0
    assert m["net_ex_best_month"] == 80.0


def test_execution_stress_break_even_formula() -> None:
    frames = [
        {"gross": 100.0, "slippage": 10.0, "commission": 6.0},
        {"gross": 60.0, "slippage": 8.0, "commission": 4.0},
    ]
    es = execution_stress(frames, avg_fill_price=1000.0)
    net_1x = 100.0 + 60.0 - 18.0 - 10.0
    assert es["net_1x"] == net_1x
    assert es["fills"] == 4
    breakeven = net_1x / (0.0001 * 1000.0 * 4)
    assert round(es["break_even_extra_slippage_bps_per_fill"], 4) == round(breakeven, 4)
    assert es["net_at_slip2x"] == net_1x - 18.0
    assert es["extra_slippage_bps_per_fill"]["extra_10_bps_per_fill"] == round(net_1x - 4.0, 6)


def test_audit_consistency_guard() -> None:
    economc = {
        "round_trips": 31, "fills": 62, "wins": 20, "losses": 11,
        "total_pnl": "1.0", "gross_close_edge": "3.0", "slippage": "1.0",
        "commission": "1.0", "max_drawdown": "0.5",
        "t_gross_per_rt": 3.9, "carry_share_pct": 100.0,
        "reconcile": {"reconcile_all": True},
    }
    it6 = {
        "economics": dict(economc),
        "mandatory_metrics": {"net_pnl": "1.0", "ending_equity": "100001.0"},
    }
    ok, mismatches = audit_consistency(economc, it6)
    assert ok and not mismatches
    economc["total_pnl"] = "2.0"
    ok, mismatches = audit_consistency(economc, it6)
    assert not ok and any("total_pnl" in m for m in mismatches)