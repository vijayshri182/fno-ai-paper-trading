"""Tests for OUR-ALGO-004 - COHERENT single-slot re-test of the reversal-flat-hold
treatment (post-Entry 003 protective-stop contamination finding).

Artifact-integrity + pure-logic tests only.  No engine re-run for tuning.  The
objective is to lock the Amendment-1 mechanism-purity form of criterion C, the
coherent-single-slot acceptance result, and the recorded artifact reproducibility.

Coverage:
  * criterion_c_amended pure logic (suppressed_holds, no_invented_flat,
    extension-bar semantics, displaced-leg exit attribution, unexplained-removal
    rejection, partition reconciliation);
  * amended acceptance wiring (C now honoured while the ORIGINAL OUR-ALGO-003
    flat_suppression report is still recorded with its impossible sub-checks);
  * artifact sha pin + provenance artifacts unchanged + benchmark replay equals
    the frozen Iter-009 record;
  * safety / firewall / one-structural-change guarantees.
"""
from __future__ import annotations

from datetime import date, datetime, time as dtime, timedelta
from decimal import Decimal

from fno_ai_paper_trading.models.enums import Signal
from fno_ai_paper_trading.models.market import MarketPrice
from fno_ai_paper_trading.research.iteration005_discovery import (
    _base_meta,
    _close_signal,
    _hold,
    _open_signal,
    EnsembleParams,
)
from fno_ai_paper_trading.research.our_algo_004_reversal_hold_coherent import (
    OUT_FILE,
    REPO,
    criterion_c_amended,
)
from fno_ai_paper_trading.research.our_algo_003_single_slot_reversal_hold import (
    FLAT_REASON,
    ITER9_RESULT_SHA256,
    ITER9_VOL_LED_EXPECTED,
    OUR_ALGO_001_SHA256,
    OUR_ALGO_002_SHA256,
    OUT_FILE as OUT_003,
    SUPPRESS_REASON,
    _sha256,
)

R = REPO / "runs" / "research" / "day_batch"

# Deterministic artifact sha recorded at execution time (double-run identical).
OUR_ALGO_004_OUT_SHA256 = "bd523b653a227eea582ab10e05b2a677d3f962f4a16fe82bbf2d45a5113e5ad7"
OUT_FILE_003_SHA = "5775c3ec16e1f5f056a157c8cd90477739910e5ee33a413ecde1d1693acbdd87"
PER = 75


def _load():
    import json
    return json.loads(OUT_FILE.read_text(encoding="utf-8"))


def _mk_bars(sessions: int, profile) -> list[MarketPrice]:
    from fno_ai_paper_trading.models.enums import InstrumentType
    from fno_ai_paper_trading.models.instruments import Instrument
    inst = Instrument(symbol="NIFTY", instrument_type=InstrumentType.INDEX,
                      underlying_symbol="NIFTY50")
    day0 = date(2022, 1, 3)
    bars = []
    for s in range(sessions):
        d = day0 + timedelta(days=s)
        for b in range(PER):
            open_, close_, half = profile(s, b, PER)
            high = max(open_, close_) + half
            low = min(open_, close_) - half
            ts = datetime.combine(d, dtime(9, 15)) + timedelta(minutes=5 * b)
            bars.append(MarketPrice(inst, ts, open_, high, low, close_))
    return bars


def _plain_profile(s, b, n):
    return Decimal("100"), Decimal("100"), Decimal("0.25")


def _bars(sessions: int = 12) -> list[MarketPrice]:
    return _mk_bars(sessions, _plain_profile)


def _sig(bars, opens, flats) -> list:
    n = max([*opens, *flats], default=0) + 10
    out = [None] * n
    for i in opens:
        out[i] = _open_signal(bars[i], 1,
                              "ensemble confluence up - enter long",
                              _base_meta(bars[i], i, n))
    for i in flats:
        out[i] = _close_signal(bars[i], 1, FLAT_REASON, _base_meta(bars[i], i, n))
    return out


def _rt(entry_index, exit_index, side="LONG"):
    return {"entry": {"entry_index": entry_index}, "exit_index": exit_index, "side": side}


# ---------------------------------------------------------------------------
# 1. criterion_c_amended - pure logic
# ---------------------------------------------------------------------------


def test_amended_passes_suppressed_displacement_partition():
    bars = _bars()
    bench = _sig(bars, [54, 56], [55, 57])
    var = _sig(bars, [54, 56], [57])
    var[55] = _hold(bars[55], SUPPRESS_REASON, _base_meta(bars[55], 55, len(bench)))
    sup = [{"bar_index": 55, "entry_index": 54}]
    rts_a = [_rt(54, 55), _rt(56, 57)]
    c = criterion_c_amended(bench, var, sup, rts_a)
    assert c["verified"] is True
    assert c["suppressed_holds"] is True and c["no_invented_flat"] is True
    assert c["unexplained_flat_exits_removed"] == 0
    assert c["unexplained_nonflat_exits_removed"] == 0
    assert c["partition_spot_check"] is True


def test_amended_extension_bars_do_not_break_purity():
    """Multi-day hold: suppression on EVERY flat first-of-day is NOT an unexplained
    removal (benchmark flat bar suppressed + extension bar beyond the benchmark)."""
    bars = _bars()
    bench = _sig(bars, [54, 56], [55])          # benchmark exits on first flat day
    var = _sig(bars, [54], [])
    var[55] = _hold(bars[55], SUPPRESS_REASON, _base_meta(bars[55], 55, len(bench)))
    var[56] = _hold(bars[56], SUPPRESS_REASON, _base_meta(bars[56], 56, len(bench)))
    sup = [{"bar_index": 55, "entry_index": 54}, {"bar_index": 56, "entry_index": 54}]
    rts_a = [_rt(54, 55)]
    c = criterion_c_amended(bench, var, sup, rts_a)
    assert c["verified"] is True
    assert c["extension_hold_bars"] == 1            # bar 56 is an extension bar
    assert c["unexplained_flat_exits_removed"] == 0


def test_amended_rejects_unexplained_flat_removal():
    """A benchmark flat exit that is neither suppressed nor a displaced-leg exit
    MUST fail the amended criterion (real mechanism leak)."""
    bars = _bars()
    bench = _sig(bars, [54], [55, 58])
    var = _sig(bars, [54], [58])
    sup = [{"bar_index": 65, "entry_index": 54}]    # suppression nowhere near bar 55
    rts_a = [_rt(54, 55)]
    c = criterion_c_amended(bench, var, sup, rts_a)
    assert c["verified"] is False
    assert c["unexplained_flat_exits_removed"] == 1


def test_amended_rejects_unexplained_nonflat_removal():
    """A benchmark exit of ANY reason absent without suppression/displacement
    must fail (mechanism purity covers all exits, not just flat)."""
    bars = _bars()
    bench = _sig(bars, [54, 56], [57])
    bench[55] = _close_signal(bars[55], 1, "ensemble confluence broken - exits",
                              _base_meta(bars[55], 55, len(bench)))
    var = _sig(bars, [54, 56], [57])
    sup = []
    rts_a = [_rt(54, 55, "LONG"), _rt(56, 57, "LONG")]
    # displaced B opens empty at 56 -> displaced leg [56->57] covers exit 57; exit 55
    # remains unexplained non-flat removal.
    c = criterion_c_amended(bench, var, sup, rts_a)
    assert c["verified"] is False
    assert c["unexplained_nonflat_exits_removed"] >= 1


def test_amended_rejects_invented_flat():
    bars = _bars()
    bench = _sig(bars, [54, 56], [57])
    var = list(_sig(bars, [54, 56], [57]))
    var[58] = _close_signal(bars[58], 1, FLAT_REASON, _base_meta(bars[58], 58, len(bench)))
    sup = []
    rts_a = [_rt(54, 57), _rt(56, 57)]
    c = criterion_c_amended(bench, var, sup, rts_a)
    assert c["no_invented_flat"] is False
    assert c["verified"] is False


def test_amended_rejects_missing_hold():
    """Suppression record must correspond to an ACTUAL HOLD(SUPPRESS_REASON)."""
    bars = _bars()
    bench = _sig(bars, [54, 56], [55, 57])
    var = _sig(bars, [54, 56], [55, 57])            # no HOLD at bar 55
    sup = [{"bar_index": 55, "entry_index": 54}]
    rts_a = [_rt(54, 55), _rt(56, 57)]
    c = criterion_c_amended(bench, var, sup, rts_a)
    assert c["suppressed_holds"] is False
    assert c["verified"] is False


def test_amended_displaced_leg_exit_accounted():
    """A displaced A leg whose exit vanishes is NOT unexplained (it is a
    displaced-leg exit, covered by the displacement ledger D)."""
    bars = _bars()
    bench = _sig(bars, [54, 56], [55, 57])
    var = _sig(bars, [56], [57])                     # leg 54 never re-opens in variant
    var[55] = _hold(bars[55], SUPPRESS_REASON, _base_meta(bars[55], 55, len(bench)))
    sup = [{"bar_index": 55, "entry_index": 56}]
    rts_a = [_rt(54, 55), _rt(56, 57)]
    c = criterion_c_amended(bench, var, sup, rts_a)
    assert c["verified"] is True
    assert c["partition_spot_check"] is True


# ---------------------------------------------------------------------------
# 2. artifact integrity + recorded reproducibility
# ---------------------------------------------------------------------------

PROVENANCE = (
    ("iteration_009_vol_led.json", ITER9_RESULT_SHA256),
    ("our_algo_001_transition_quality.json", OUR_ALGO_001_SHA256),
    ("our_algo_002_transition_exit.json", OUR_ALGO_002_SHA256),
    ("our_algo_003_single_slot_reversal_hold.json", OUT_FILE_003_SHA),
)


def test_004_artifact_sha_fixed():
    assert OUT_FILE.exists()
    assert _sha256(OUT_FILE) == OUR_ALGO_004_OUT_SHA256


def test_004_provenance_artifacts_unchanged():
    for name, h in PROVENANCE:
        p = R / name
        assert p.exists() and _sha256(p) == h, name


def test_004_benchmark_replays_iter09_record():
    out = _load()
    assert out["benchmark_frozen"]["guard_equal_iteration009"] is True
    for k, exp in ITER9_VOL_LED_EXPECTED.items():
        got = out["economics"]["A_vol_led"][k]
        assert str(got) == str(exp) or (isinstance(got, float) and got == exp), k


def test_004_amended_acceptance_all_pass_and_promising():
    out = _load()
    acc = out["acceptance"]
    assert acc["all_acceptance_satisfied"] is True
    assert acc["failed"] == []
    assert acc["criteria"]["C_flat_suppression_only"] is True
    assert acc["flat_suppression_amended"]["verified"] is True
    assert acc["flat_suppression_amended"]["suppressed_holds"] is True
    assert acc["flat_suppression_amended"]["no_invented_flat"] is True
    assert acc["flat_suppression_amended"]["unexplained_flat_exits_removed"] == 0
    assert acc["flat_suppression_amended"]["unexplained_nonflat_exits_removed"] == 0
    assert out["classification"] == "PROMISING"


def test_004_original_flat_suppression_still_recorded_honestly():
    """The OUR-ALGO-003 form of C (echo/other-flat-unchanged) is IMPOSSIBLE under
    displacement; the artifact must still record it as failing (no whitewash)."""
    out = _load()
    fs = out["acceptance"]["flat_suppression"]
    assert fs["verified"] is False
    assert fs["suppressed_holds"] is True
    assert fs["no_invented_flat"] is True
    assert fs["echo_match"] is False
    assert fs["other_flat_unchanged"] is False
    assert out["criterion_c_amendment"]["amendment_id"] == "AMENDMENT-1"
    assert out["criterion_c_amendment"]["status"] == "APPLIED"


def test_004_coherent_single_slot_and_ledger():
    out = _load()
    assert out["single_slot_invariant"]["holds"] is True
    assert out["single_slot_invariant"]["max_open_qty"] == 1
    assert out["displacement_ledger"]["complete"] is True
    assert out["displacement_ledger"]["unexplained"] == []


def test_004_contamination_differential_recorded():
    out = _load()
    d = out["diagnostic_control_contaminated_B"]
    assert d["protective_stops"] == 1
    assert d["phantom_opposite_side_opens"] == 33
    assert d["single_slot_holds"] is False
    assert out["contamination_differential"]["net_default_vs_coherent"][0] != \
        out["contamination_differential"]["net_default_vs_coherent"][1]


def test_004_firewall_and_safety():
    out = _load()
    assert out["leakage_checks"]["causal_series_violations"] == 0
    assert out["leakage_checks"]["no_oos_in_research"] is True
    assert out["leakage_checks"]["benchmark_artifact_byte_unchanged"] is True
    assert out["leakage_checks"]["deterministic_double_run_identical"] is True
    assert out["protected_oos_firewall"]["used_for_selection"] is False
    assert out["protected_oos_firewall"]["touched"] is False
    assert out["safety_state"]["promotion"] == "NO"
    assert out["safety_state"]["algo_ready"] == "NO"
    assert out["safety_state"]["algorithm_health"] == "RED"
    assert out["safety_state"]["live_gate"] == "CLOSED"
    assert out["safety_state"]["paper_only"] is True


def test_004_one_structural_change_guarantees():
    out = _load()
    cd = out["candidate_definition"]
    assert cd["zero_new_thresholds"] is True
    assert cd["one_structural_change"] is True
    assert cd["single_slot_in_loop"] is True
    assert cd["non_overlay"] is True
    assert out["method"]["single_controlled_experiment"] is True
    assert out["method"]["no_parameter_optimization"] is True
    assert out["method"]["no_oos_for_selection"] is True
    assert out["promotion_notice"].startswith("Research acceptance criteria")


def test_004_halves_warmed_independently():
    out = _load()
    rep = out["repeatability"]
    assert rep["split"]["window1"][0] == "2022-01-03"
    assert rep["split"]["note"].startswith("pre-registered chronological halves")
    h1, h2 = rep["half1"], rep["half2"]
    assert h1["causality_violations"] == 0 and h2["causality_violations"] == 0
    # I criterion requires BETTER reversal per-RT in BOTH halves
    assert out["acceptance"]["criteria"]["I_temporal_stability"] is True


def test_004_003_artifact_not_rewritten():
    """Frozen OUR-ALGO-003 artifact must remain byte-identical (contaminated
    baseline kept for contrast; verdict stands)."""
    assert OUT_003.exists()
    assert _sha256(OUT_003) == OUT_FILE_003_SHA