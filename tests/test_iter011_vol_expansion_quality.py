"""Tests for ITERATION 011 - VOLATILITY-EXPANSION QUALITY CONFIRMATION (pre-OOS A/B).

Artifact-integrity + pure-logic + synthetic-signal tests only.  No engine
re-run for tuning.  The vol-quality confirmation is asserted on engineered
synthetic DailySeries to verify decision-time causality, entry-only isolation,
class semantics and byte-identical (confirm=False) behavior versus the
Iteration-009 benchmark generator.
"""
from __future__ import annotations

import json
from datetime import date, datetime, time as dtime, timedelta
from decimal import Decimal

from fno_ai_paper_trading.models.enums import InstrumentType, Signal
from fno_ai_paper_trading.models.instruments import Instrument
from fno_ai_paper_trading.models.market import MarketPrice
from fno_ai_paper_trading.research.iteration011_vol_expansion_quality import (
    FINGERPRINT_SHA256,
    ITER6_RESULT_SHA256,
    ITER7_RESULT_SHA256,
    ITER8_RESULT_SHA256,
    ITER9_RESULT_SHA256,
    ITER9_VOL_LED_EXPECTED,
    ITER10_RESULT_SHA256,
    OUT_FILE,
    REPO,
    _sha256,
    build_vol_quality_series,
    classify_research,
    confirmation_passes,
    ensemble_variant_vol_quality,
    evaluate_acceptance,
    volmove_quality_class,
)
from fno_ai_paper_trading.research.iteration008_component_regime_attribution import (
    ensemble_variant,
)
from fno_ai_paper_trading.research.iteration005_discovery import EnsembleParams

R = REPO / "runs" / "research" / "day_batch"

# Deterministic artifact sha recorded at execution time (run twice, identical).
ITERATION_011_OUT_SHA256 = "f303a8be97852592269b3fa8416756507534b1dab220657888f402b54207aea4"


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
    """Session 59 = wide-range expansion; cur (returns[59]) = 0 -> confirmation
    ZERO_UNDEFINED, while the Iter-009 benchmark still fires a LONG entry."""

    def profile(s, b, n):
        if s == 59:
            return Decimal("100"), Decimal("100"), Decimal("2")
        if s >= 60:
            return Decimal("101"), Decimal("101"), Decimal("0.5")
        return Decimal("100"), Decimal("100"), Decimal("0.25")

    return _mk_bars(sessions, profile)


def _persistent_bars(sessions: int = 70) -> list[MarketPrice]:
    """s58 closes +1% and s59 closes +2% (wide expansion); s60 closes up again.
    The ramp sessions s57/s58 are narrow so the ONLY benchmark entry after
    warmup fires at the first bar of session 60 where cur = returns[59] > 0 and
    prev = returns[58] > 0 with |cur| >= |prev| -> confirmation PASSES
    (SAME_SIGN_AND_EXPANDING) and the entry is retained."""

    def profile(s, b, n):
        if s == 59:
            return Decimal("100"), Decimal("103"), Decimal("2")
        if s == 60:
            return Decimal("103"), Decimal("105"), Decimal("0.25")
        if s == 58:
            return Decimal("100"), Decimal("101"), Decimal("0.1")
        if s == 57:
            return Decimal("100"), Decimal("100"), Decimal("0.1")
        return Decimal("100"), Decimal("100"), Decimal("1.5")

    return _mk_bars(sessions, profile)


def _decision_first_bar(sessions: int = 70) -> int:
    return 60 * 75  # first bar of session 60


def _persistent_close(s: int) -> Decimal:
    """Session close prices of ``_persistent_bars`` (k -> close[k])."""
    if s == 59:
        return Decimal("103")
    if s == 60:
        return Decimal("105")
    if s == 58:
        return Decimal("101")
    if s >= 61:
        return Decimal("105")
    return Decimal("100")


def _c(p: int) -> float:
    """Expected fractional return of session ``p`` for ``_persistent_bars``."""
    return float((_persistent_close(p) - _persistent_close(p - 1)) / _persistent_close(p - 1))


# ---------------------------------------------------------------------------
# 1/2. causal previous-bar reference and no future-bar access
# ---------------------------------------------------------------------------


def test_vol_quality_series_uses_last_two_completed_sessions():
    bars = _persistent_bars()
    cur, prev, violations = build_vol_quality_series(bars)
    first = _decision_first_bar()
    assert violations == []
    k = 59
    assert abs(cur[first] - _c(k)) < 1e-12
    assert abs(prev[first] - _c(k - 1)) < 1e-12
    # bars inside session 60 use sessions 58/59 (strictly before), never returns[60]
    assert cur[first] != 0 and prev[first] != 0
    assert 0 < cur[first] and 0 < prev[first] and abs(cur[first]) >= abs(prev[first])


def test_no_future_bar_used_synthetic():
    for bars in (_expansion_bars(), _persistent_bars()):
        _c_, _p_, violations = build_vol_quality_series(bars)
        assert violations == []
    # a crafted violation would be caught: a session whose last bar is NOT
    # strictly before the referencing bar is reported (empty here by design).


def test_vol_quality_series_deterministic():
    bars = _persistent_bars()
    a = build_vol_quality_series(bars)
    b2 = build_vol_quality_series(bars)
    assert a[0] == b2[0] and a[1] == b2[1] and a[2] == b2[2] == []
    e = _expansion_bars()
    assert build_vol_quality_series(e)[2] == []


def test_first_session_undefined():
    bars = _persistent_bars()
    cur, prev, _ = build_vol_quality_series(bars)
    assert cur[0] is None and prev[0] is None


# ---------------------------------------------------------------------------
# 3. five-class semantics
# ---------------------------------------------------------------------------


def test_volmove_quality_class_matrix():
    c = volmove_quality_class
    assert c(1.0, 2.0) == "SAME_SIGN_AND_CONTRACTING"
    assert c(1.0, 1.0) == "SAME_SIGN_AND_FLAT"
    assert c(2.0, 1.0) == "SAME_SIGN_AND_EXPANDING"
    assert c(-2.0, -1.0) == "SAME_SIGN_AND_EXPANDING"
    assert c(-1.0, -2.0) == "SAME_SIGN_AND_CONTRACTING"
    assert c(1.0, -1.0) == "SIGN_FLIP"
    assert c(-1.0, 1.0) == "SIGN_FLIP"
    assert c(0.0, 1.0) == "ZERO_UNDEFINED"
    assert c(1.0, 0.0) == "ZERO_UNDEFINED"
    assert c(None, 1.0) == "ZERO_UNDEFINED"
    assert c(1.0, None) == "ZERO_UNDEFINED"
    assert c(0.0, 0.0) == "ZERO_UNDEFINED"


def test_flat_is_not_expanding():
    assert volmove_quality_class(1.0, 1.0) == "SAME_SIGN_AND_FLAT"
    assert volmove_quality_class(2.0, 2.0) == "SAME_SIGN_AND_FLAT"


# ---------------------------------------------------------------------------
# 4. confirmation pass / reject semantics
# ---------------------------------------------------------------------------


def test_confirmation_passes_long():
    p = confirmation_passes
    assert p(1.0, 1.0, 1) is True          # equal magnitude passes (non-shrinking)
    assert p(2.0, 1.0, 1) is True          # expanding
    assert p(1.0, 2.0, 1) is False         # contracting
    assert p(1.0, -1.0, 1) is False        # sign flip
    assert p(-1.0, 1.0, 1) is False        # sign flip
    assert p(0.0, 1.0, 1) is False         # zero
    assert p(None, 1.0, 1) is False        # undefined
    assert p(1.0, None, 1) is False        # undefined
    assert p(1.0, 1.0, 0) is False         # no target
    assert p(1.0, 1.0, -1) is False        # opposite side


def test_confirmation_passes_short():
    p = confirmation_passes
    assert p(-1.0, -1.0, -1) is True        # equal magnitude passes
    assert p(-2.0, -1.0, -1) is True        # expanding down
    assert p(-1.0, -2.0, -1) is False       # contracting
    assert p(-1.0, 1.0, -1) is False        # sign flip
    assert p(0.0, -1.0, -1) is False        # zero
    assert p(None, -1.0, -1) is False       # undefined


# ---------------------------------------------------------------------------
# 5. entry-only isolation (suppression) + retained entry
# ---------------------------------------------------------------------------


def test_confirmation_denied_suppresses_only_benchmark_entry():
    bars = _expansion_bars()
    params = EnsembleParams()
    cur, prev, _ = build_vol_quality_series(bars)
    first = _decision_first_bar()
    sigs_no, sup_no = ensemble_variant_vol_quality(bars, params, cur, prev, confirm=False)
    sigs_yes, sup_yes = ensemble_variant_vol_quality(bars, params, cur, prev, confirm=True)
    assert sup_no == []
    assert sigs_no[first].signal == Signal.BUY  # Iter-009 benchmark entry intact
    assert len(sup_yes) == 1 and sup_yes[0]["bar_index"] == first
    assert sup_yes[0]["side"] == "LONG"
    assert sup_yes[0]["quality_class"] == "ZERO_UNDEFINED"
    assert sup_yes[0]["cur_volmove"] == 0
    assert sigs_yes[first].signal == Signal.HOLD
    assert "vol-quality confirmation denied" in sigs_yes[first].reason
    assert sigs_yes[first].meta == sigs_no[first].meta  # decision-time meta preserved


def test_confirmation_pass_keeps_entry():
    bars = _persistent_bars()
    params = EnsembleParams()
    cur, prev, _ = build_vol_quality_series(bars)
    first = _decision_first_bar()
    sigs_no, sup_no = ensemble_variant_vol_quality(bars, params, cur, prev, confirm=False)
    sigs_yes, sup_yes = ensemble_variant_vol_quality(bars, params, cur, prev, confirm=True)
    assert sup_no == [] and sup_yes == []
    assert sigs_yes[first].signal == sigs_no[first].signal == Signal.BUY
    assert sigs_yes[first].meta == sigs_no[first].meta


# ---------------------------------------------------------------------------
# 6. Iter-009 benchmark conditions unchanged (confirm=False byte-identical)
# ---------------------------------------------------------------------------


def test_confirm_false_byte_identical_to_iter09_generator():
    from fno_ai_paper_trading.research.iteration011_vol_expansion_quality import (
        _streams_identical,
    )
    for bars in (_expansion_bars(), _persistent_bars()):
        params = EnsembleParams()
        cur, prev, _ = build_vol_quality_series(bars)
        sig_a, sup_a = ensemble_variant_vol_quality(bars, params, cur, prev, confirm=False)
        assert sup_a == []
        bench = ensemble_variant(bars, params, use_trend=False, use_vol=True)
        assert _streams_identical(sig_a, bench)


def test_confirm_true_invariant_structure():
    bars = _expansion_bars()
    params = EnsembleParams()
    cur, prev, _ = build_vol_quality_series(bars)
    s1, v1 = ensemble_variant_vol_quality(bars, params, cur, prev, confirm=True)
    s2, v2 = ensemble_variant_vol_quality(bars, params, cur, prev, confirm=True)
    assert [(s.signal, s.reason, s.meta) for s in s1] == \
        [(s.signal, s.reason, s.meta) for s in s2]
    assert v1 == v2
    started = False
    for s in s1:
        if s is not None and "enter" in s.reason:
            assert started is False
        if s is not None and "exit" in s.reason:
            assert started


# ---------------------------------------------------------------------------
# pure acceptance / classification logic
# ---------------------------------------------------------------------------


def _ident_all_true():
    return {"A": {"closed_identity_holds": True, "full_identity_holds": True},
            "B": {"closed_identity_holds": True, "full_identity_holds": True}}


def test_classify_research_labels():
    acc = {"all_acceptance_satisfied": True, "failed": [], "satisfied": []}
    assert classify_research({"A": {"net": Decimal("100")}, "B": {"net": Decimal("200")}}, acc) \
        == "PROMISING RESEARCH RESULT"
    assert classify_research({"A": {"net": Decimal("200")}, "B": {"net": Decimal("200")}}, acc) \
        == "REJECTED"
    assert classify_research({"A": {"net": Decimal("200")}, "B": {"net": Decimal("150")}}, acc) \
        == "REJECTED"
    acc_bad = {"all_acceptance_satisfied": False, "failed": ["A_net_improves"], "satisfied": []}
    assert classify_research({"A": {"net": Decimal("100")}, "B": {"net": Decimal("150")}}, acc_bad) \
        == "INCONCLUSIVE"


def _base_res(anet="1000", bnet="1100", art=100, brt=80):
    return {
        "A": {"net": Decimal(anet), "rt": art, "max_dd": Decimal("100"),
              "coverage": 150.0, "win_rate": 60.0, "hv_net": Decimal("200"),
              "mix_net": Decimal("300"), "gross_loss": Decimal("-100"),
              "gross_profit": Decimal("1100")},
        "B": {"net": Decimal(bnet), "rt": brt, "max_dd": Decimal("120"),
              "coverage": 160.0, "win_rate": 60.0, "hv_net": Decimal("190"),
              "mix_net": Decimal("280"), "gross_loss": Decimal("-80"),
              "gross_profit": Decimal("1180")},
    }


def test_evaluate_acceptance_all_pass():
    halves = {"h1_improves": True, "h2_improves": True}
    a = evaluate_acceptance(_base_res(), _ident_all_true(), [], [], halves, 0, True, True)
    assert a["all_acceptance_satisfied"] is True
    assert sorted(a["satisfied"]) == sorted(("A_net_improves", "B_no_opportunity_collapse",
                                             "C_high_vol_retention", "D_mixed_retention",
                                             "E_cost_coverage", "F_drawdown",
                                             "G_repeatability", "H_causality",
                                             "I_accounting", "J_reconstruction"))
    assert a["failed"] == []


def test_evaluate_acceptance_net_worse_fails_a():
    halves = {"h1_improves": True, "h2_improves": True}
    a = evaluate_acceptance(_base_res(bnet="900"), _ident_all_true(), [], [], halves, 0, True, True)
    assert a["criteria"]["A_net_improves"] is False
    assert "A_net_improves" in a["failed"]


def test_evaluate_acceptance_opportunity_collapse():
    halves = {"h1_improves": True, "h2_improves": True}
    # B removed >60% of A's trades and adds nothing -> collapse guard trips
    res = _base_res(art=100, brt=30)
    a = evaluate_acceptance(res, _ident_all_true(), [], [], halves, 0, True, True)
    assert a["criteria"]["B_no_opportunity_collapse"] is False
    assert a["trivial_guard"]["opportunity_collapse"] is True


def test_evaluate_acceptance_trivial_guard_not_tripped():
    a = evaluate_acceptance(_base_res(art=100, brt=89), _ident_all_true(), [], [],
                            {"h1_improves": True, "h2_improves": True}, 0, True, True)
    assert a["trivial_guard"]["weak_evidence"] is False
    assert a["trivial_guard"]["trades_removed"] == 11


# ---------------------------------------------------------------------------
# provenance guards
# ---------------------------------------------------------------------------


def test_iteration_provenance_artifacts_unchanged():
    pairs = [
        (R / "iteration_006_n3_fingerprint.json", FINGERPRINT_SHA256),
        (R / "iteration_006_protected_oos_n3.json", ITER6_RESULT_SHA256),
        (R / "iteration_007_robustness_audit_n3.json", ITER7_RESULT_SHA256),
        (R / "iteration_008_component_regime_attribution_n3.json", ITER8_RESULT_SHA256),
        (R / "iteration_009_vol_led.json", ITER9_RESULT_SHA256),
        (R / "iteration_010_sideways_veto.json", ITER10_RESULT_SHA256),
    ]
    for p, h in pairs:
        assert p.exists(), p
        assert _sha256(p) == h, p.name


def test_iteration_011_artifact_deterministic_sha():
    assert _sha256(OUT_FILE) == ITERATION_011_OUT_SHA256


# ---------------------------------------------------------------------------
# artifact experiment / firewall / method shape
# ---------------------------------------------------------------------------


def test_artifact_has_expected_experiment_fields():
    d = _load()
    assert d["experiment"] == "ITERATION_011_VOL_EXPANSION_QUALITY"
    assert d["method"]["single_controlled_experiment"] is True
    assert d["method"]["one_structural_change"] is True
    assert d["method"]["no_parameter_optimization"] is True
    assert d["method"]["no_oos_for_selection"] is True
    assert d["method"]["b_independently_replayed"] is True


def test_firewall_and_research_domain_shape():
    d = _load()
    assert d["protected_oos_firewall"]["window"] == ["2025-10-06", "2026-09-11"]
    assert d["protected_oos_firewall"]["used_for_selection"] is False
    assert d["protected_oos_firewall"]["engine_replays_forced_onto_research_bars_only"] is True
    assert d["research_domain"]["window"] == ["2022-01-03", "2025-10-03"]
    assert d["research_domain"]["bars"] == 69781
    assert d["research_domain"]["days"] == 932
    assert d["data_fingerprint"] == "6c400b016c1c6d3c99a80db9a8355bc3e195e17c43537bcb93b7e198e85b7f5c"


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
    # byte-for-byte parity with the recorded Iter-009 block
    for k, exp in ITER9_VOL_LED_EXPECTED.items():
        assert str(a[k]) == str(exp) or (isinstance(a[k], float) and a[k] == exp), k


def test_candidate_defines_single_isolated_change():
    d = _load()
    c = d["candidate_definition"]
    assert "VOLATILITY-EXPANSION PERSISTENCE CONFIRMATION" in c["id"]
    assert "two most recent COMPLETED sessions" in c["isolated_change"]
    assert c["no_workaround"] is True and c["no_second_filter"] is True
    assert c["no_new_indicator"] is True
    assert c["no_volatility_threshold_added"] is True
    assert c["no_parameter_sweep"] is True
    for item in ("VOL signal", "VOL_GATE", "exits (provider ATR stop / max-hold / "
                                            "confluence-flat / confluence-broken)",
                 "stop-loss", "position sizing", "capital", "commission", "slippage",
                 "warmup", "execution", "multiplier", "dataset", "evaluation configuration"):
        assert item in c["retained"]


# ---------------------------------------------------------------------------
# 8. determinism (pure helpers + pinned artifact sha)
# ---------------------------------------------------------------------------


def test_artifact_repeatability_blob_stable():
    d = _load()
    assert d["repeatability"]["split"]["days_half1"] == 466
    assert d["repeatability"]["split"]["days_half2"] == 466
    for hkey in ("half1", "half2"):
        h = d["repeatability"][hkey]
        assert h["causality_violations"] == 0
        assert h["suppressed"] > 0
        assert Decimal(h["candidate"]["net"]) < Decimal(h["benchmark"]["net"])
        assert h["improves"] is False


def test_repeatability_failed_in_both_halves():
    d = _load()
    assert d["repeatability"]["h1_improves"] is False
    assert d["repeatability"]["h2_improves"] is False
    assert Decimal(d["repeatability"]["half1"]["candidate"]["net"]) \
        < Decimal(d["repeatability"]["half1"]["benchmark"]["net"])
    assert Decimal(d["repeatability"]["half2"]["candidate"]["net"]) \
        < Decimal(d["repeatability"]["half2"]["benchmark"]["net"])


def test_engine_replay_deterministic():
    d = _load()
    det = d["determinism"]
    assert det["b_signals_generated_once"] is True
    assert det["b_engine_replay_identical"] is True


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
        block = d["economics"]["A_vol_led"] if key == "A" else d["economics"]["B_vol_quality"]
        assert stats["gross_close_sum"] == block["gross_close_edge"]


# ---------------------------------------------------------------------------
# result-specific audits (documented negative result)
# ---------------------------------------------------------------------------


def test_result_classification_rejected():
    d = _load()
    assert d["classification"] == "REJECTED"
    assert Decimal(d["comparison"]["B"]["net"]) <= Decimal(d["comparison"]["A"]["net"])
    assert d["comparison"]["net_delta_B_minus_A"] == "-7755.711397720"


def test_acceptance_failed_and_satisfied_lists_exact():
    d = _load()
    ac = d["acceptance"]
    assert sorted(ac["satisfied"]) == sorted(("E_cost_coverage", "F_drawdown", "H_causality",
                                              "I_accounting", "J_reconstruction"))
    assert sorted(ac["failed"]) == sorted(("A_net_improves", "B_no_opportunity_collapse",
                                           "C_high_vol_retention", "D_mixed_retention",
                                           "G_repeatability"))
    assert ac["all_acceptance_satisfied"] is False


def test_retention_failed_despite_positive_b_capture():
    d = _load()
    assert Decimal(d["comparison"]["B"]["hv_net"]) > 0
    assert Decimal(d["comparison"]["B"]["mix_net"]) > 0
    assert Decimal(d["comparison"]["B"]["net"]) > 0
    # but retention ratios are far below the 0.90 threshold
    assert float(d["comparison"]["retention_high_vol"]) < 0.90
    assert float(d["comparison"]["retention_mixed"]) < 0.90


def test_trivial_removal_guard_not_tripped():
    d = _load()
    tg = d["acceptance"]["trivial_guard"]
    assert tg["trades_removed"] == 175
    assert float(tg["trades_removed_pct"]) == 77.43
    assert tg["weak_evidence"] is False
    assert tg["opportunity_collapse"] is False
    assert Decimal(tg["net_of_path_additions_b_only"]) > 0  # B-only additions were positive


def test_b_capture_and_metrics():
    d = _load()
    b = d["economics"]["B_vol_quality"]
    assert b["round_trips"] == 51 and b["fills"] == 102
    assert b["opens"] == 51 and b["closes"] == 51
    assert b["wins"] == 30 and b["losses"] == 21
    assert float(b["win_rate_pct"]) == 58.82
    assert b["total_pnl"] == "4310.090517395"
    assert b["gross_close_edge"] == "7174.65000"
    assert b["slippage"] == "2203.50895"
    assert b["commission"] == "661.050532605"
    assert b["max_drawdown"] == "768.994120425"
    assert b["open_at_close"] == 0
    assert b["carry_share_pct"] == 100.0
    assert b["net_closed_rts"] == "4310.090517395"
    cm = d["candidate_metrics"]
    assert cm["net"] == "4310.090517395" and cm["round_trips"] == 51
    assert cm["high_vol_net"] == "3073.211365705"
    assert cm["mixed_net"] == "1668.241625180"


def test_suppression_shape_and_quality_classes():
    d = _load()
    s = d["suppression"]
    assert s["suppressed_count"] == 272
    assert s["direct_benchmark_entries_suppressed"] == 191
    assert s["secondary_path_suppressions"] == 81
    assert s["suppressed_by_quality_class"] == {
        "SAME_SIGN_AND_CONTRACTING": 35, "SAME_SIGN_AND_EXPANDING": 46, "SIGN_FLIP": 191}
    recs = s["suppressed_records"]
    assert len(recs) == 272
    for r in recs:
        assert r["quality_class"] in ("SAME_SIGN_AND_CONTRACTING", "SAME_SIGN_AND_EXPANDING",
                                      "SIGN_FLIP", "ZERO_UNDEFINED")
        assert r["side"] in ("LONG", "SHORT")
        assert "vol-quality confirmation denied" in r["decision_candidate"]


def test_path_analysis_shape():
    d = _load()
    pa = d["path_analysis"]
    assert pa["common_same_bar_same_side"] == 36
    assert pa["a_only"] == 190
    assert pa["b_only"] == 15
    assert pa["stateful_path_change"] == "SUPPRESSION_PLUS_DOWNSTREAM"
    assert pa["exit_machinery_unchanged_shared_positions_identical"] is True
    assert pa["a_only_summary"]["confirmation_denied"] == 190
    assert pa["a_only_summary"]["displaced_by_b_position"] == 0
    assert pa["a_only_summary"]["winners"] == 111 and pa["a_only_summary"]["losers"] == 79
    # the suppressed A-only pool was strongly net-positive -> suppression was costly
    assert Decimal(pa["a_only_summary"]["net"]) > 0


def test_opportunity_retention_documented():
    d = _load()
    opp = d["opportunity_retention"]
    assert opp["a_entries"] == 226 and opp["b_entries"] == 51
    assert opp["direct_suppressions"] == 191
    assert opp["secondary_suppressions"] == 81
    assert float(opp["retention_gross_pct"]) == 29.46
    assert float(opp["retention_net_pct"]) == 35.95


def test_volatility_quality_attribution_shape():
    d = _load()
    vq = d["volatility_quality_attribution"]
    classes = {row["vol_quality_class"] for row in vq["A"]}
    assert classes == {"SIGN_FLIP", "SAME_SIGN_AND_CONTRACTING", "SAME_SIGN_AND_EXPANDING"}
    a_pivot = {row["vol_quality_class"]: row for row in vq["A"]}
    assert a_pivot["SIGN_FLIP"]["count"] == 133
    assert a_pivot["SAME_SIGN_AND_CONTRACTING"]["count"] == 21
    assert a_pivot["SAME_SIGN_AND_EXPANDING"]["count"] == 72
    # B retains only confirmed (non-shrinking same-sign) trades
    b_pivot = vq["B"]
    assert len(b_pivot) == 1
    assert b_pivot[0]["vol_quality_class"] == "SAME_SIGN_AND_EXPANDING"
    assert b_pivot[0]["count"] == 51
    hist = vq["per_bar_class_histogram"]
    assert sum(hist.values()) == 69781
    assert hist["ZERO_UNDEFINED"] == 375


def test_leakage_checks_clean():
    d = _load()
    lc = d["leakage_checks"]
    assert lc["causal_series_violations"] == 0
    assert lc["confirmation_uses_only_completed_sessions"] is True
    assert lc["no_oos_in_research"] is True
    assert lc["signal_streams_research_only"] is True
    assert lc["benchmark_artifact_byte_unchanged"] is True
    iso = lc["entry_change_isolation"]
    assert iso["direct_benchmark_entries_suppressed"] == 191
    assert iso["candidate_open_isolation"]["unexplained_or_conflicting"] == []


def test_trace_records_causal_volmove_and_classes():
    d = _load()
    side_map = {"LONG": 1, "SHORT": -1}
    assert len(d["statefulness"]["trace_A"]) == 226
    assert len(d["statefulness"]["trace_B"]) == 51
    for key in ("A", "B"):
        for r in d["statefulness"]["trace_" + key]:
            assert r["raw_underlying_volmove"] == side_map[r["side"]]
            assert r["vol_gate_state"] == "ACTIVE_RETAINED"
            assert r["conf_cur_volmove"] is not None and r["conf_prev_volmove"] is not None
            assert r["vol_quality_class"] in ("SAME_SIGN_AND_CONTRACTING",
                                              "SAME_SIGN_AND_EXPANDING", "SIGN_FLIP",
                                              "ZERO_UNDEFINED")
    # every B decision passed the confirmation -> expanding class only
    assert {r["vol_quality_class"] for r in d["statefulness"]["trace_B"]} \
        == {"SAME_SIGN_AND_EXPANDING"}


def test_safety_state_unchanged():
    s = _load()["safety_state"]
    assert s["promotion"] == "NO"
    assert s["algo_ready"] == "NO"
    assert s["algorithm_health"] == "RED"
    assert s["scope.live_trading"] is False
    assert s["live_gate"] == "CLOSED"
    assert s["paper_only"] is True
    assert s["human_approval_required"] is True
    assert s["model_0_frozen"] is True


def test_prohibited_conclusion_words_absent():
    cls = _load()["classification"]
    assert cls not in ("BEST", "WINNER", "ROBUST", "VALIDATED", "PRODUCTION_READY",
                       "PROMOTED", "LIVE_READY", "RECOMMENDED")
    assert all(w not in cls for w in ("PROMISING", "VALIDATED", "BEST"))