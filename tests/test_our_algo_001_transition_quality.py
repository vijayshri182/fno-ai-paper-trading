"""Tests for OUR-ALGO-001 - REVERSAL-TRANSITION QUALITY GATE (pre-OOS A/B).

Artifact-integrity + pure-logic + synthetic-signal tests only.  No engine
re-run for tuning.  The reversal-transition entry gate is asserted on
engineered synthetic DailySeries to verify decision-time causality, entry-only
isolation, class semantics and byte-identical (gate=False) behavior versus the
Iteration-009 benchmark generator, plus the recorded artifact reproducibility
(transition cells must equal the Iteration-012 forensics cells on the frozen
pipeline).
"""
from __future__ import annotations

import json
from datetime import date, datetime, time as dtime, timedelta
from decimal import Decimal

from fno_ai_paper_trading.models.enums import InstrumentType, Signal
from fno_ai_paper_trading.models.instruments import Instrument
from fno_ai_paper_trading.models.market import MarketPrice
from fno_ai_paper_trading.research.our_algo_001_transition_quality import (
    FINGERPRINT_SHA256,
    ITER9_RESULT_SHA256,
    ITER9_VOL_LED_EXPECTED,
    ITER10_RESULT_SHA256,
    ITER11_RESULT_SHA256,
    ITER12_RESULT_SHA256,
    ITER12_VALIDATION_SHA256,
    OUT_FILE,
    REPO,
    _reversal_ok,
    _sha256,
    _sign_of,
    build_prior_sign_series,
    classify_research,
    ensemble_variant_transition_gate,
    evaluate_acceptance,
    transition_label,
)
from fno_ai_paper_trading.research.iteration008_component_regime_attribution import (
    ensemble_variant,
)
from fno_ai_paper_trading.research.iteration005_discovery import EnsembleParams

R = REPO / "runs" / "research" / "day_batch"

# Deterministic artifact sha recorded at execution time (run twice, identical).
OUR_ALGO_001_OUT_SHA256 = "e985e22e1936e7e3e91ec6f89ed33cd1909cfb38fab1d71fad04e55d5343873d"

# Recorded Iteration-012 forensics transition cells (the hypothesis basis).
ITER12_CELLS = {
    "REVERSAL_LONG": {"rt": 58, "net": "4487.621492640", "win_rate_pct": 68.97},
    "REVERSAL_SHORT": {"rt": 60, "net": "2912.772272930", "win_rate_pct": 56.67},
    "CONTINUATION_LONG": {"rt": 54, "net": "3089.525554375", "win_rate_pct": 50.00},
    "CONTINUATION_SHORT": {"rt": 54, "net": "1498.160595685", "win_rate_pct": 51.85},
}


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


def _continuation_denied_bars(sessions: int = 70) -> list[MarketPrice]:
    """s60 closes UP with a wide range; s61 closes UP again.  A VOL-led LONG
    benchmark entry fires at first-of-day s61 while the last completed session
    (s60) moved UP -> up->up CONTINUATION -> gate suppresses it."""

    def profile(s, b, n):
        if s == 59:
            return Decimal("100"), Decimal("100"), Decimal("0.1")
        if s == 60:
            return Decimal("101"), Decimal("102"), Decimal("1")
        if s == 61:
            return Decimal("102"), Decimal("104"), Decimal("0.5")
        return Decimal("100"), Decimal("100"), Decimal("0.25")

    return _mk_bars(sessions, profile)


def _reversal_kept_bars(sessions: int = 70) -> list[MarketPrice]:
    """s60 closes DOWN with a wide range; s61 closes UP.  VOL-led LONG entry
    at first-of-day s61 with last completed session (s60) DOWN -> down->up
    REVERSAL -> gate retains the entry."""

    def profile(s, b, n):
        if s == 59:
            return Decimal("100"), Decimal("100"), Decimal("0.1")
        if s == 60:
            return Decimal("99"), Decimal("98"), Decimal("1")
        if s == 61:
            return Decimal("98"), Decimal("100"), Decimal("0.5")
        return Decimal("100"), Decimal("100"), Decimal("0.25")

    return _mk_bars(sessions, profile)


def _first_bar(s: int) -> int:
    return s * 75  # first bar of session s


def _close_at(s: int, bars) -> Decimal:
    return bars[_first_bar(s)].close if s < len({_.timestamp.date() for _ in bars}) else Decimal("100")


# ---------------------------------------------------------------------------
# 1/2. causal prior-sign series and no future-bar access
# ---------------------------------------------------------------------------


def test_prior_sign_series_uses_last_completed_session():
    bars = _reversal_kept_bars()
    prior_sign, violations = build_prior_sign_series(bars)
    assert violations == []
    first = _first_bar(61)
    assert prior_sign[first] == -1  # sign(returns[60]) - last completed session


def test_prior_sign_continuation_profile():
    bars = _continuation_denied_bars()
    prior_sign, violations = build_prior_sign_series(bars)
    assert violations == []
    assert prior_sign[_first_bar(61)] == 1


def test_no_future_bar_used_synthetic():
    for b in (_continuation_denied_bars(), _reversal_kept_bars()):
        _, violations = build_prior_sign_series(b)
        assert violations == []


def test_prior_sign_first_two_sessions_undefined():
    bars = _reversal_kept_bars()
    prior_sign, _ = build_prior_sign_series(bars)
    assert prior_sign[0] is None
    assert prior_sign[1] is None


def test_prior_sign_deterministic():
    for b in (_continuation_denied_bars(), _reversal_kept_bars()):
        a, va = build_prior_sign_series(b)
        c, vc = build_prior_sign_series(b)
        assert a == c and va == vc == []


# ---------------------------------------------------------------------------
# 3. gate semantics
# ---------------------------------------------------------------------------


def test_sign_of():
    assert _sign_of(0.5) == 1
    assert _sign_of(-0.5) == -1
    assert _sign_of(0.0) == 0
    assert _sign_of(None) is None


def test_reversal_ok_matrix():
    assert _reversal_ok(-1, 1) is True        # down -> long (reversal)
    assert _reversal_ok(1, -1) is True        # up -> short (reversal)
    assert _reversal_ok(1, 1) is False        # up -> long (continuation)
    assert _reversal_ok(-1, -1) is False      # down -> short (continuation)
    assert _reversal_ok(0, 1) is False        # flat prior -> no evidence
    assert _reversal_ok(0, -1) is False
    assert _reversal_ok(None, 1) is False
    assert _reversal_ok(None, -1) is False


def test_transition_label_matrix():
    assert transition_label(-1, 1) == "REVERSAL_LONG"
    assert transition_label(1, -1) == "REVERSAL_SHORT"
    assert transition_label(1, 1) == "CONTINUATION_LONG"
    assert transition_label(-1, -1) == "CONTINUATION_SHORT"
    assert transition_label(0, 1) == "CONTINUATION_LONG"
    assert transition_label(None, None) == "UNDEFINED"


# ---------------------------------------------------------------------------
# 4. entry-only isolation (suppression) + retained entry
# ---------------------------------------------------------------------------


def test_continuation_suppresses_only_benchmark_entry():
    bars = _continuation_denied_bars()
    params = EnsembleParams()
    prior_sign, _ = build_prior_sign_series(bars)
    first = _first_bar(61)
    sigs_no, sup_no = ensemble_variant_transition_gate(bars, params, prior_sign, gate=False)
    sigs_yes, sup_yes = ensemble_variant_transition_gate(bars, params, prior_sign, gate=True)
    assert sup_no == []
    assert sigs_no[first].signal == Signal.BUY          # Iter-009 benchmark entry intact
    assert len(sup_yes) == 1 and sup_yes[0]["bar_index"] == first
    assert sup_yes[0]["side"] == "LONG"
    assert sup_yes[0]["prior_session_return_sign"] == 1  # up->up continuation
    assert sigs_yes[first].signal == Signal.HOLD
    assert "reversal transition required" in sigs_yes[first].reason
    assert sigs_yes[first].meta == sigs_no[first].meta    # decision-time meta preserved


def test_reversal_keeps_entry():
    bars = _reversal_kept_bars()
    params = EnsembleParams()
    prior_sign, _ = build_prior_sign_series(bars)
    first = _first_bar(61)
    sigs_no, sup_no = ensemble_variant_transition_gate(bars, params, prior_sign, gate=False)
    sigs_yes, sup_yes = ensemble_variant_transition_gate(bars, params, prior_sign, gate=True)
    assert sup_no == [] and sup_yes == []
    assert sigs_yes[first].signal == sigs_no[first].signal == Signal.BUY
    assert sigs_yes[first].meta == sigs_no[first].meta


# ---------------------------------------------------------------------------
# 5. Iter-009 benchmark conditions unchanged (gate=False byte-identical)
# ---------------------------------------------------------------------------


def test_gate_false_byte_identical_to_iter09_generator():
    from fno_ai_paper_trading.research.our_algo_001_transition_quality import (
        _streams_identical,
    )
    for bars in (_continuation_denied_bars(), _reversal_kept_bars()):
        params = EnsembleParams()
        prior_sign, _ = build_prior_sign_series(bars)
        sig_a, sup_a = ensemble_variant_transition_gate(bars, params, prior_sign, gate=False)
        assert sup_a == []
        bench = ensemble_variant(bars, params, use_trend=False, use_vol=True)
        assert _streams_identical(sig_a, bench)


def test_gate_true_invariant_structure():
    bars = _reversal_kept_bars()
    params = EnsembleParams()
    prior_sign, _ = build_prior_sign_series(bars)
    s1, v1 = ensemble_variant_transition_gate(bars, params, prior_sign, gate=True)
    s2, v2 = ensemble_variant_transition_gate(bars, params, prior_sign, gate=True)
    assert [(s.signal, s.reason, s.meta) for s in s1] == \
        [(s.signal, s.reason, s.meta) for s in s2]
    assert v1 == v2


# ---------------------------------------------------------------------------
# pure acceptance / classification logic
# ---------------------------------------------------------------------------


def _ident_all_true():
    return {"A": {"closed_identity_holds": True, "full_identity_holds": True},
            "B": {"closed_identity_holds": True, "full_identity_holds": True}}


def _base_res(anet="1000", bnet="1100", art=100, brt=80):
    return {
        "A": {"net": Decimal(anet), "rt": art, "max_dd": Decimal("100"),
              "coverage": 150.0, "win_rate": 60.0, "hv_net": Decimal("200"),
              "mix_net": Decimal("300"), "gross_loss": Decimal("-100"),
              "gross_profit": Decimal("1100")},
        "B": {"net": Decimal(bnet), "rt": brt, "max_dd": Decimal("120"),
              "coverage": 160.0, "win_rate": 62.0, "hv_net": Decimal("190"),
              "mix_net": Decimal("280"), "gross_loss": Decimal("-80"),
              "gross_profit": Decimal("1180")},
    }


def test_classify_research_labels():
    acc_all = {"all_acceptance_satisfied": True, "quality_improves": True,
               "net_improves": True, "failed": [], "satisfied": []}
    assert classify_research({"A": {"net": Decimal("100")}, "B": {"net": Decimal("200")}}, acc_all) \
        == "PROMISING RESEARCH RESULT"
    acc_net_no = {"all_acceptance_satisfied": True, "quality_improves": True,
                  "net_improves": False, "failed": [], "satisfied": []}
    assert classify_research({"A": {"net": Decimal("200")}, "B": {"net": Decimal("150")}}, acc_net_no) \
        == "INCONCLUSIVE"
    acc_bad = {"all_acceptance_satisfied": False, "quality_improves": True,
               "net_improves": False, "failed": ["I_repeatability"], "satisfied": []}
    assert classify_research({"A": {"net": Decimal("200")}, "B": {"net": Decimal("150")}}, acc_bad) \
        == "REJECTED"


def _halves_all():
    return {"h1_improves": True, "h2_improves": True}


def test_evaluate_acceptance_all_pass():
    a = evaluate_acceptance(_base_res(), _ident_all_true(), [], [], _halves_all(), 0, True, True)
    assert a["all_acceptance_satisfied"] is True
    assert a["quality_improves"] is True and a["net_improves"] is True
    assert sorted(a["satisfied"]) == sorted(("A_trade_quality_improves", "B_win_rate_improves",
                                             "C_drag_reduced", "D_drawdown_safe",
                                             "E_retained_meaningful", "F_accounting",
                                             "G_reconstruction", "H_causality",
                                             "I_repeatability", "J_no_opportunity_collapse"))
    assert a["failed"] == []


def test_evaluate_acceptance_wr_better_only():
    res = _base_res(bnet="900")
    a = evaluate_acceptance(res, _ident_all_true(), [], [], _halves_all(), 0, True, True)
    assert a["quality_improves"] is True        # per-RT up, WR up
    assert a["net_improves"] is False
    assert a["criteria"]["A_trade_quality_improves"] is True
    assert a["all_acceptance_satisfied"] is True
    assert classify_research(res, a) == "INCONCLUSIVE"


def test_evaluate_acceptance_wr_worse_fails_quality():
    res = _base_res(bnet="900", brt=90)
    res["B"]["win_rate"] = 55.0
    a = evaluate_acceptance(res, _ident_all_true(), [], [], _halves_all(), 0, True, True)
    assert a["quality_improves"] is False
    assert a["failed"] == sorted(("A_trade_quality_improves", "B_win_rate_improves"))
    assert classify_research(res, a) == "REJECTED"


# ---------------------------------------------------------------------------
# artifact integrity + recorded reproducibility
# ---------------------------------------------------------------------------


def test_artifact_exists_and_sha_fixed():
    assert OUT_FILE.exists()
    assert _sha256(OUT_FILE) == OUR_ALGO_001_OUT_SHA256


def test_artifact_benchmark_matches_iter09_record():
    out = _load()
    assert out["benchmark_frozen"]["guard_equal_iteration009"] is True
    for k, exp in ITER9_VOL_LED_EXPECTED.items():
        got = out["economics"]["A_vol_led"][k]
        assert str(got) == str(exp) or (isinstance(got, float) and got == exp), k


def test_artifact_provenance_artifacts_unchanged():
    for p, h in ((R / "iteration_006_n3_fingerprint.json", FINGERPRINT_SHA256),
                 (R / "iteration_009_vol_led.json", ITER9_RESULT_SHA256),
                 (R / "iteration_010_sideways_veto.json", ITER10_RESULT_SHA256),
                 (R / "iteration_011_vol_expansion_quality.json", ITER11_RESULT_SHA256),
                 (R / "iteration_012_trade_forensics.json", ITER12_RESULT_SHA256),
                 (R / "iteration_012_validation.json", ITER12_VALIDATION_SHA256)):
        assert p.exists() and _sha256(p) == h, p.name


def test_artifact_transition_a_reproduces_forensics():
    out = _load()
    for cell, exp in ITER12_CELLS.items():
        got = out["transition_analysis"]["A"].get(cell)
        assert got is not None, cell
        assert got["rt"] == exp["rt"], cell
        assert got["net"] == exp["net"], cell
        assert got["win_rate_pct"] == exp["win_rate_pct"], cell


def test_artifact_gate_contains_only_reversal_cells():
    out = _load()
    for cell, tbl in out["transition_analysis"]["B"].items():
        assert cell.startswith("REVERSAL_"), cell
    assert out["transition_gate"]["suppressed_by_prior_sign"] == {"-1": 73, "1": 66}
    assert out["transition_gate"]["suppressed_count"] == 139


def test_artifact_pre_registered_outcome_stats():
    out = _load()
    a, b = out["comparison"]["A"], out["comparison"]["B"]
    assert a["rt"] == "226" and b["rt"] == "136"
    assert int(Decimal(out["acceptance"]["trivial_guard"]["trades_removed"])) == 90
    assert out["acceptance"]["quality_improves"] is True
    assert out["acceptance"]["net_improves"] is False
    assert Decimal(b["net"]) < Decimal(a["net"])
    assert Decimal(b["win_rate"]) > Decimal(a["win_rate"])
    assert Decimal(b["max_dd"]) <= Decimal(a["max_dd"]) * Decimal("1.25")
    assert "I_repeatability" in out["acceptance"]["failed"]
    assert out["classification"] == "REJECTED"


def test_artifact_causality_and_firewall():
    out = _load()
    assert out["leakage_checks"]["causal_series_violations"] == 0
    assert out["leakage_checks"]["no_oos_in_research"] is True
    assert out["protected_oos_firewall"]["used_for_selection"] is False
    assert out["safety_state"]["algo_ready"] == "NO"
    assert out["safety_state"]["promotion"] == "NO"
    assert out["safety_state"]["live_gate"] == "CLOSED"