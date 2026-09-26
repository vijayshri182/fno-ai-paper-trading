"""Tests for ITERATION 010 - SIDEWAYS-VETO ECONOMIC TEST (pre-OOS A/B).

Artifact-integrity + pure-logic + synthetic-signal tests only.  No engine
re-run for tuning; the veto strategy is asserted on engineered synthetic
DailySeries to verify decision-time causality and entry-only isolation.
"""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, time as dtime, timedelta
from decimal import Decimal
from pathlib import Path

from fno_ai_paper_trading.models.enums import InstrumentType, Signal
from fno_ai_paper_trading.models.instruments import Instrument
from fno_ai_paper_trading.models.market import MarketPrice
from fno_ai_paper_trading.research.iteration005_discovery import (
    EnsembleParams,
    classify_day,
    day_stats,
)
from fno_ai_paper_trading.research.iteration010_sideways_veto import (
    FINGERPRINT_SHA256,
    ITER6_RESULT_SHA256,
    ITER7_RESULT_SHA256,
    ITER8_RESULT_SHA256,
    ITER9_RESULT_SHA256,
    OUT_FILE,
    REPO,
    _entry_suppressions_only,
    _sha256,
    causal_prior_regime_series,
    chronological_split,
    classify_research,
    ensemble_variant_sideways_veto,
    evaluate_acceptance,
    sideways_loss,
)

R = REPO / "runs" / "research" / "day_batch"

# Deterministic artifact sha recorded at execution time (run twice, identical).
ITERATION_010_OUT_SHA256 = "1dc3f7fabe65e42ed72ce507193f9307c4ab6bd7cde8350ca3724556c3d0f8bf"


def _load():
    return json.loads(OUT_FILE.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# synthetic bars
# ---------------------------------------------------------------------------


def _mk_bars(sessions: int, profile) -> list[MarketPrice]:
    """``profile(s, b, n) -> (open, close, half_range)`` per bar."""
    inst = Instrument(symbol="NIFTY", instrument_type=InstrumentType.INDEX,
                      underlying_symbol="NIFTY50")
    per = 75
    day0 = date(2022, 1, 3)
    bars = []
    for s in range(sessions):
        d = day0 + timedelta(days=s)
        for b in range(per):
            open_, close_, half = profile(s, b, per)
            high = max(open_, close_) + half
            low = min(open_, close_) - half
            ts = datetime.combine(d, dtime(9, 15)) + timedelta(minutes=5 * b)
            bars.append(MarketPrice(inst, ts, open_, high, low, close_))
    return bars


def _expansion_bars(sessions: int = 70) -> list[MarketPrice]:
    """Session 59 = wide-range expansion, session 60+ closes +1% (target +1)."""

    def profile(s, b, n):
        if s == 59:
            return Decimal("100"), Decimal("100"), Decimal("2")
        if s >= 60:
            return Decimal("101"), Decimal("101"), Decimal("0.5")
        return Decimal("100"), Decimal("100"), Decimal("0.25")

    return _mk_bars(sessions, profile)


def _flat_bars(sessions: int = 70) -> list[MarketPrice]:
    """All sideways except session 5 which is strongly trending up."""

    def profile(s, b, n):
        if s == 5:
            c = Decimal("100") + Decimal("0.04") * (b + 1)  # close 100 -> ~103
            return c, c, Decimal("0.55")
        return Decimal("100"), Decimal("100"), Decimal("0.25")

    return _mk_bars(sessions, profile)


def _decision_first_bar(sessions: int = 70) -> int:
    return 60 * 75  # first bar of session 60


# ---------------------------------------------------------------------------
# 1. SIDEWAYS + valid entry -> vetoed; 2. non-SIDEWAYS unchanged
# ---------------------------------------------------------------------------


def test_sideways_prior_session_vetoes_valid_entry():
    bars = _expansion_bars()
    params = EnsembleParams()
    prior = ["sideways"] * len(bars)
    first = _decision_first_bar()
    sigs_no, veto_no = ensemble_variant_sideways_veto(bars, params, prior, veto=False)
    sigs_yes, veto = ensemble_variant_sideways_veto(bars, params, prior, veto=True)
    assert veto_no == []
    assert sigs_no[first].signal == Signal.BUY  # valid VOL-led entry without veto
    assert len(veto) == 1 and veto[0]["bar_index"] == first
    assert veto[0]["side"] == "LONG"
    assert veto[0]["causal_prior_session_regime"] == "sideways"
    assert sigs_yes[first].signal == Signal.HOLD
    assert "veto" in sigs_yes[first].reason
    assert sigs_yes[first].meta == sigs_no[first].meta  # decision-time meta preserved


def test_non_sideways_session_unchanged():
    bars = _expansion_bars()
    params = EnsembleParams()
    prior = ["trending_up"] * len(bars)
    first = _decision_first_bar()
    sigs_no, veto_no = ensemble_variant_sideways_veto(bars, params, prior, veto=False)
    sigs_yes, veto = ensemble_variant_sideways_veto(bars, params, prior, veto=True)
    assert veto == []
    assert sigs_yes[first].signal == sigs_no[first].signal == Signal.BUY


def test_veto_applies_only_to_flagged_regime_slots():
    bars = _expansion_bars()
    params = EnsembleParams()
    prior = ["sideways"] * len(bars)
    first = _decision_first_bar()
    prior[first] = "mixed"  # only the decision bar says non-sideways
    sigs, veto = ensemble_variant_sideways_veto(bars, params, prior, veto=True)
    assert veto == [] and sigs[first].signal == Signal.BUY


# ---------------------------------------------------------------------------
# 5/6. causal regime input and no look-ahead
# ---------------------------------------------------------------------------


def test_causal_series_uses_only_last_completed_session():
    bars = _flat_bars()
    prior, labels, violations = causal_prior_regime_series(bars)
    assert violations == []
    first_idx = 5 * 75  # first bar of the trending session 5
    last_completed = bars[:first_idx][-1].timestamp.date()
    assert prior[first_idx] == classify_day(day_stats(
        [b for b in bars if b.timestamp.date() == last_completed]))
    # session 5 itself is trending_up (EOD), but bars IN session 5 must use
    # the PRIOR (sideways) session - not their own end-of-day label.
    assert prior[first_idx] == "sideways"
    nxt = 6 * 75
    assert prior[nxt] == "trending_up"
    # bars of the very first session have no completed prior session.
    assert prior[0] is None


def test_no_lookahead_causal_series_deterministic():
    bars = _flat_bars()
    p1, l1, v1 = causal_prior_regime_series(bars)
    p2, l2, v2 = causal_prior_regime_series(bars)
    assert p1 == p2 and l1 == l2 and v1 == v2 == []


def test_generator_deterministic_and_exits_untouched_in_structure():
    bars = _expansion_bars()
    params = EnsembleParams()
    prior = ["sideways"] * len(bars)
    s1, v1 = ensemble_variant_sideways_veto(bars, params, prior, veto=True)
    s2, v2 = ensemble_variant_sideways_veto(bars, params, prior, veto=True)
    assert [(s.signal, s.reason, s.meta) for s in s1] == [(s.signal, s.reason, s.meta) for s in s2]
    assert v1 == v2
    # Any exit emitted by the candidate must target a position it actually holds:
    # no exit signal is ever emitted by the veto arm before its own entry.
    started = False
    for s in s1:
        if s is not None and "enter" in s.reason:
            assert started is False  # candidate enters at most once on this flat series
        if s is not None and "exit" in s.reason:
            assert started


# ---------------------------------------------------------------------------
# path isolation (entry-only change + downstream reorders)
# ---------------------------------------------------------------------------


class _Sig:
    def __init__(self, sig, reason):
        self.signal, self.reason = sig, reason


def _sig_list(pairs):
    return [_Sig(*p) for p in pairs]


def test_entry_suppression_isolation_direct():
    bench = _sig_list([(Signal.BUY, "ensemble confluence up - enter long")])
    cand = _sig_list([(Signal.HOLD, "FLAT - sideways veto (research)")])
    ok, info = _entry_suppressions_only(cand, bench, [(0, "LONG")])
    assert ok
    assert info["direct_benchmark_entries_suppressed"] == 1
    assert info["suppression_set_exact"] is True
    assert info["candidate_open_isolation"]["unexplained_or_conflicting"] == []


def test_entry_suppression_allows_downstream_reorder():
    bench = _sig_list([(Signal.BUY, "ensemble confluence up - enter long"),
                       (Signal.SELL, "ensemble confluence broken - exits")])
    cand = _sig_list([(Signal.HOLD, "FLAT - sideways veto (research)"),
                      (Signal.BUY, "ensemble confluence up - enter long")])
    ok, info = _entry_suppressions_only(cand, bench, [(0, "LONG")])
    assert ok
    assert info["candidate_open_isolation"]["reorder_after_vetoed_hold_count"] == 1


def test_crafted_open_is_rejected_as_leak():
    bench = _sig_list([])
    cand = _sig_list([(Signal.BUY, "ensemble confluence up - enter long")])
    ok, info = _entry_suppressions_only(cand, bench, [])
    assert ok is False
    assert info["candidate_open_isolation"]["unexplained_or_conflicting"]


# ---------------------------------------------------------------------------
# pure acceptance / classification logic
# ---------------------------------------------------------------------------


def test_sideways_loss_definition():
    assert sideways_loss(Decimal("-100")) == Decimal("100")
    assert sideways_loss(Decimal("50")) == Decimal("0")
    assert sideways_loss(Decimal("0")) == Decimal("0")


def test_classify_research_labels():
    acc = {"all_acceptance_satisfied": True, "failed": [], "satisfied": []}
    assert classify_research({"A": {"net": Decimal("100")}, "B": {"net": Decimal("200")}}, acc) \
        == "PROMISING RESEARCH RESULT"
    assert classify_research({"A": {"net": Decimal("200")}, "B": {"net": Decimal("200")}}, acc) \
        == "REJECTED"
    assert classify_research({"A": {"net": Decimal("200")}, "B": {"net": Decimal("150")}}, acc) \
        == "REJECTED"
    acc_bad = {"all_acceptance_satisfied": False, "failed": ["A_sideways_damage"], "satisfied": []}
    assert classify_research({"A": {"net": Decimal("100")}, "B": {"net": Decimal("150")}}, acc_bad) \
        == "INCONCLUSIVE"


def _ident_all_true():
    return {"A": {"closed_identity_holds": True, "full_identity_holds": True},
            "B": {"closed_identity_holds": True, "full_identity_holds": True}}


def test_evaluate_acceptance_all_pass():
    res = {
        "A": {"net": Decimal("1000"), "sideways_net": Decimal("-100"), "hv_net": Decimal("200"),
              "mix_net": Decimal("300"), "coverage": 150.0, "max_dd": Decimal("100"), "rt": 10},
        "B": {"net": Decimal("1100"), "sideways_net": Decimal("-40"), "hv_net": Decimal("180"),
              "mix_net": Decimal("270"), "coverage": 160.0, "max_dd": Decimal("120"), "rt": 8},
    }
    a = evaluate_acceptance(res, _ident_all_true(), [], [])
    assert a["all_acceptance_satisfied"] is True
    assert sorted(a["satisfied"]) == sorted(("A_sideways_damage", "B_high_vol_retention",
                                             "C_mixed_retention", "D_cost_coverage",
                                             "E_drawdown", "F_accounting"))


def test_evaluate_acceptance_sideways_worse_fails_a():
    res = {
        "A": {"net": Decimal("1000"), "sideways_net": Decimal("-100"), "hv_net": Decimal("200"),
              "mix_net": Decimal("300"), "coverage": 150.0, "max_dd": Decimal("100"), "rt": 10},
        "B": {"net": Decimal("1100"), "sideways_net": Decimal("-130"), "hv_net": Decimal("180"),
              "mix_net": Decimal("270"), "coverage": 160.0, "max_dd": Decimal("120"), "rt": 8},
    }
    a = evaluate_acceptance(res, _ident_all_true(), [], [])
    assert a["criteria"]["A_sideways_damage"] is False
    assert "A_sideways_damage" in a["failed"]


def test_evaluate_acceptance_trivial_guard_not_tripped():
    res = {
        "A": {"net": Decimal("1000"), "sideways_net": Decimal("-100"), "hv_net": Decimal("200"),
              "mix_net": Decimal("300"), "coverage": 150.0, "max_dd": Decimal("100"), "rt": 100},
        "B": {"net": Decimal("900"), "sideways_net": Decimal("-60"), "hv_net": Decimal("170"),
              "mix_net": Decimal("260"), "coverage": 140.0, "max_dd": Decimal("90"), "rt": 89},
    }
    a = evaluate_acceptance(res, _ident_all_true(), [], [])
    assert a["trivial_guard"]["weak_evidence"] is False


# ---------------------------------------------------------------------------
# repeatability split
# ---------------------------------------------------------------------------


def test_chronological_split_pre_registered_days():
    bars = _expansion_bars(sessions=70)
    head, tail, d1, d2 = chronological_split(bars, 30)
    assert len(d1) == 30 and len(d2) == 40
    assert head[-1].timestamp.date() == d1[-1]
    assert tail[0].timestamp.date() == d2[0]
    assert head[-1].timestamp.date() < tail[0].timestamp.date()


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


def test_iteration_009_benchmark_artifact_unchanged():
    p = R / "iteration_009_vol_led.json"
    assert p.exists() and _sha256(p) == ITER9_RESULT_SHA256


def test_iteration_010_artifact_deterministic_sha():
    assert _sha256(OUT_FILE) == ITERATION_010_OUT_SHA256


# ---------------------------------------------------------------------------
# artifact experiment / firewall / method shape
# ---------------------------------------------------------------------------


def test_artifact_has_expected_experiment_fields():
    d = _load()
    assert d["experiment"] == "ITERATION_010_SIDEWAYS_VETO_ECONOMIC_TEST"
    assert d["method"]["single_controlled_experiment"] is True
    assert d["method"]["one_structural_change"] is True
    assert d["method"]["no_parameter_optimization"] is True
    assert d["method"]["no_oos_for_selection"] is True


def test_firewall_and_research_domain_shape():
    d = _load()
    assert d["protected_oos_firewall"]["window"] == ["2025-10-06", "2026-09-11"]
    assert d["protected_oos_firewall"]["used_for_selection"] is False
    assert d["research_domain"]["window"] == ["2022-01-03", "2025-10-03"]
    assert d["research_domain"]["bars"] == 69781
    assert d["research_domain"]["days"] == 932


# ---------------------------------------------------------------------------
# 7. Iter-009 benchmark reproducibility (provenance/guard constants)
# ---------------------------------------------------------------------------


def test_benchmark_guard_equal_iteration009():
    d = _load()
    assert d["benchmark_frozen"]["guard_equal_iteration009"] is True
    assert d["benchmark_frozen"]["iteration_009_artifact_sha256"] == ITER9_RESULT_SHA256
    a = d["economics"]["A_vol_led"]
    assert a["round_trips"] == 226 and a["fills"] == 453
    assert a["opens"] == 227 and a["closes"] == 226
    assert a["wins"] == 136 and a["losses"] == 90
    assert a["total_pnl"] == "12065.801915115"
    assert a["gross_close_edge"] == "24352.10000"
    assert a["max_drawdown"] == "2388.664922445"
    assert a["net_closed_rts"] == "11988.079915630"
    assert a["open_at_close"] == 1
    assert float(a["win_rate_pct"]) == 60.18


def test_candidate_defines_single_isolated_change():
    d = _load()
    c = d["candidate_definition"]
    assert "SIDEWAYS ENTRY VETO" in c["id"]
    assert "last completed session is SIDEWAYS" in c["isolated_change"]
    assert c["no_workaround"] is True and c["no_second_filter"] is True
    for item in ("exits (provider ATR stop / max-hold / confluence-flat / confluence-broken)",
                 "stop-loss", "position sizing", "capital", "commission", "slippage",
                 "warmup", "execution", "multiplier", "dataset", "evaluation configuration"):
        assert item in c["retained"]


# ---------------------------------------------------------------------------
# 8. determinism (pure helpers + pinned artifact sha)
# ---------------------------------------------------------------------------


def test_causal_series_deterministic_across_calls():
    bars = _flat_bars()
    a = causal_prior_regime_series(bars)
    b2 = causal_prior_regime_series(bars)
    assert a[0] == b2[0] and a[2] == b2[2]


def test_artifact_repeatability_blob_stable():
    d = _load()
    assert d["repeatability"]["split"]["days_half1"] == 466
    assert d["repeatability"]["split"]["days_half2"] == 466
    for hkey in ("half1", "half2"):
        h = d["repeatability"][hkey]
        assert h["causality_violations"] == 0
        assert h["vetoed"] > 0
        assert Decimal(h["candidate"]["net"]) < Decimal(h["benchmark"]["net"])
        assert h["sideways_loss_reduction_pct"] is not None
        assert h["sideways_loss_reduction_pct"] < 0  # veto fails in BOTH halves


# ---------------------------------------------------------------------------
# 9. accounting valid
# ---------------------------------------------------------------------------


def test_a_and_b_economic_identity_hold():
    d = _load()
    for key in ("A", "B"):
        ident = d["economic_identity"][key]
        assert ident["closed_identity_holds"] is True, key
        assert ident["full_identity_holds"] is True, key
    assert d["economic_identity"]["A"]["reconcile_all"] is True


def test_gross_reconstruction_matches_round_trips():
    d = _load()
    for key in ("A", "B"):
        stats = d["trade_statistics"][key]
        block = d["economics"]["A_vol_led"] if key == "A" else d["economics"]["B_sideways_veto"]
        assert stats["gross_close_sum"] == block["gross_close_edge"]


# ---------------------------------------------------------------------------
# result-specific audits (documented negative result)
# ---------------------------------------------------------------------------


def test_result_classification_rejected():
    d = _load()
    assert d["classification"] == "REJECTED"
    assert Decimal(d["comparison"]["B"]["net"]) <= Decimal(d["comparison"]["A"]["net"])


def test_sideways_veto_failed_its_primary_criterion():
    d = _load()
    ac = d["acceptance"]
    assert ac["criteria"]["A_sideways_damage"] is False
    assert ac["failed"] == ["A_sideways_damage"]
    assert "A_sideways_damage" not in ac["satisfied"]
    assert Decimal(ac["sideways_reduction"]) < 0  # sideways loss actually got worse
    assert Decimal(d["comparison"]["B"]["sideways_net"]) < Decimal(d["comparison"]["A"]["sideways_net"])


def test_other_acceptance_criteria_satisfied():
    d = _load()
    ac = d["acceptance"]
    for crit in ("B_high_vol_retention", "C_mixed_retention", "D_cost_coverage",
                 "E_drawdown", "F_accounting"):
        assert ac["criteria"][crit] is True, crit


def test_trivial_removal_guard_not_tripped():
    d = _load()
    tg = d["acceptance"]["trivial_guard"]
    assert tg["weak_evidence"] is False
    assert tg["trades_removed"] == 11
    assert tg["trades_removed_pct"] == 4.87


def test_path_analysis_shape_and_removed_pool():
    d = _load()
    pa = d["path_analysis"]
    assert pa["common_same_bar_same_side"] == 208
    assert pa["a_only"] == 18
    assert pa["a_only_summary"]["vetoed"] == 16
    assert pa["a_only_summary"]["displaced_by_b_position"] == 2
    assert pa["b_only"] == 7
    # removed pool was net-positive -> suppressing it was costly
    assert Decimal(pa["a_only_summary"]["net"]) > 0


def test_veto_records_are_causal_and_classified():
    d = _load()
    v = d["veto"]
    assert v["suppressed_count"] == 19
    assert v["direct_benchmark_entries_suppressed"] == 16
    assert v["secondary_path_suppressions"] == 3
    recs = v["suppressed_records"]
    assert all(r["causal_prior_session_regime"] == "sideways" for r in recs)
    assert all(r["benchmark_position_realized_net"] for r in recs)


def test_leakage_checks_clean():
    d = _load()
    lc = d["leakage_checks"]
    assert lc["causal_series_violations"] == 0
    assert lc["veto_uses_only_prior_session"] is True
    isolation = lc["entry_change_isolation"]
    assert isolation["direct_benchmark_entries_suppressed"] == 16
    assert isolation["candidate_open_isolation"]["unexplained_or_conflicting"] == []


def test_safety_state_unchanged():
    s = _load()["safety_state"]
    assert s["promotion"] == "NO"
    assert s["algo_ready"] == "NO"
    assert s["algorithm_health"] == "RED"
    assert s["scope.live_trading"] is False
    assert s["live_gate"] == "CLOSED"
    assert s["paper_only"] is True
    assert s["human_approval_required"] is True


def test_prohibited_conclusion_words_absent():
    cls = _load()["classification"]
    assert cls not in ("BEST", "WINNER", "ROBUST", "VALIDATED", "PRODUCTION_READY",
                       "PROMOTED", "LIVE_READY", "RECOMMENDED")
    assert all(w not in cls for w in ("PROMISING", "VALIDATED", "BEST"))


def test_trace_records_causal_regime_and_volmove_alignment():
    d = _load()
    side_map = {"LONG": 1, "SHORT": -1}
    for key in ("A", "B"):
        for r in d["statefulness"]["trace_" + key]:
            assert r["raw_underlying_volmove"] == side_map[r["side"]]
            assert r["causal_prior_session_regime"] in (
                "sideways", "trending_up", "trending_down", "mixed", "low_vol", "volatile", None)