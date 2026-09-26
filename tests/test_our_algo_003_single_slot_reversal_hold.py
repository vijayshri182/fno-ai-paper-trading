"""Tests for OUR-ALGO-003 - SINGLE-SLOT REVERSAL-FLAT-HOLD (in-loop engine replay).

Artifact-integrity + pure-logic + synthetic-signal tests only.  No engine
re-run for tuning.  The in-loop variant is asserted on engineered synthetic
bars to verify: byte-identity of the hold_reversal=False arm vs the Iteration-009
benchmark generator, reversal-only flat suppression, short/LONG side symmetry
(the SELL-flat regression), continuation flat exits untouched, continued ATR-stop
behaviour, side-agnostic single-slot displacement attribution (inclusive of the
blocker exit bar), per-bar flat-suppression verification, pre-registered
acceptance+classification logic, and the recorded artifact reproducibility.
"""
from __future__ import annotations

import json
from datetime import date, datetime, time as dtime, timedelta
from decimal import Decimal

from fno_ai_paper_trading.models.enums import InstrumentType, Signal
from fno_ai_paper_trading.models.instruments import Instrument
from fno_ai_paper_trading.models.market import MarketPrice
from fno_ai_paper_trading.research.iteration005_discovery import (
    _base_meta,
    _close_signal,
    _open_signal,
    _hold,
    EnsembleParams,
)
from fno_ai_paper_trading.research.iteration008_component_regime_attribution import (
    ensemble_variant,
)
from fno_ai_paper_trading.research.iteration010_sideways_veto import (
    _streams_identical,
)
from fno_ai_paper_trading.research.our_algo_003_single_slot_reversal_hold import (
    ATR_REASON,
    BROKEN_REASON,
    ECONOMICS_CRITERIA,
    FLAT_REASON,
    INTEGRITY_CRITERIA,
    ITER10_RESULT_SHA256,
    ITER11_RESULT_SHA256,
    ITER12_RESULT_SHA256,
    ITER12_VALIDATION_SHA256,
    ITER9_RESULT_SHA256,
    ITER9_VOL_LED_EXPECTED,
    MAXHOLD_REASON,
    OUR_ALGO_001_SHA256,
    OUR_ALGO_002_SHA256,
    OUT_FILE,
    REPO,
    SUPPRESS_REASON,
    _reason_diff,
    _sha256,
    build_displacement_ledger,
    classify_research,
    evaluate_acceptance,
    flat_suppression_verified,
    ensemble_variant_single_slot_reversal_hold,
)

R = REPO / "runs" / "research" / "day_batch"

# Deterministic artifact sha recorded at execution time (run twice, identical).
OUR_ALGO_003_OUT_SHA256 = "5775c3ec16e1f5f056a157c8cd90477739910e5ee33a413ecde1d1693acbdd87"

# Frozen Iteration-012 transition forensics (hypothesis basis).
ITER12_CELLS = {
    "REVERSAL_LONG": {"rt": 58, "net": "4487.621492640", "win_rate_pct": 68.97},
    "REVERSAL_SHORT": {"rt": 60, "net": "2912.772272930", "win_rate_pct": 56.67},
    "CONTINUATION_LONG": {"rt": 54, "net": "3089.525554375", "win_rate_pct": 50.00},
    "CONTINUATION_SHORT": {"rt": 54, "net": "1498.160595685", "win_rate_pct": 51.85},
}

PER = 75


def _load():
    return json.loads(OUT_FILE.read_text(encoding="utf-8"))


def _first_bar(s: int) -> int:
    return s * PER


# ---------------------------------------------------------------------------
# synthetic bars engineered so _vol_move_confirm(daily, k+1, lookback) fires
# ---------------------------------------------------------------------------
# EnsembleParams: fast=9 slow=26 slope_window=5 lookback=20 -> warmup = 54
# sessions.  session 53 = range expansion (range 10 >= avg ~0.5); session 54
# closes with a signed close (target +-1 on all its bars -> first-of-day entry);
# session 55 returns to a FLAT close (return 0 -> target 0 -> confluence-flat
# branch fires while holding); session 56 crosses the frozen ATR stop.


def _mk_bars(sessions: int, profile) -> list[MarketPrice]:
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


def _long_profile(s, b, n):
    if s == 53:
        return Decimal("100"), Decimal("100"), Decimal("5")
    if s == 54:
        return Decimal("100"), Decimal("110"), Decimal("0.25")
    if s == 55:
        return Decimal("110"), Decimal("110"), Decimal("0.25")
    if s == 56:
        return Decimal("100"), Decimal("90"), Decimal("5")
    return Decimal("100"), Decimal("100"), Decimal("0.25")


def _short_profile(s, b, n):
    if s == 53:
        return Decimal("100"), Decimal("100"), Decimal("5")
    if s == 54:
        return Decimal("100"), Decimal("90"), Decimal("0.25")
    if s == 55:
        return Decimal("90"), Decimal("90"), Decimal("0.25")
    if s == 56:
        return Decimal("100"), Decimal("110"), Decimal("5")
    return Decimal("100"), Decimal("100"), Decimal("0.25")


def _bars_long(sessions: int = 60) -> list[MarketPrice]:
    return _mk_bars(sessions, _long_profile)


def _bars_short(sessions: int = 60) -> list[MarketPrice]:
    return _mk_bars(sessions, _short_profile)


def _prior_none(n: int, hits: dict[int, int] | None = None) -> list[int | None]:
    ps: list[int | None] = [None] * n
    for i, val in (hits or {}).items():
        ps[i] = val
    return ps


# ---------------------------------------------------------------------------
# 1. byte-identity of the hold_reversal=False arm vs Iter-009 benchmark
# ---------------------------------------------------------------------------


def test_off_stream_byte_identical_to_benchmark():
    bars = _bars_long()
    params = EnsembleParams()
    prior_sign = _prior_none(len(bars))
    st_off, sup_off = ensemble_variant_single_slot_reversal_hold(
        bars, params, prior_sign, hold_reversal=False)
    st_bench = ensemble_variant(bars, params, use_trend=False, use_vol=True)
    assert sup_off == []
    assert _streams_identical(st_off, st_bench)


def test_off_stream_identical_short_bars():
    bars = _bars_short()
    params = EnsembleParams()
    st_off, sup_off = ensemble_variant_single_slot_reversal_hold(
        bars, params, _prior_none(len(bars)), hold_reversal=False)
    assert sup_off == []
    assert _streams_identical(st_off, ensemble_variant(
        bars, params, use_trend=False, use_vol=True))


# ---------------------------------------------------------------------------
# 2. in-loop flat suppression: reversal only, both sides, ATR still fires
# ---------------------------------------------------------------------------


def test_reversal_long_flat_exit_suppressed_and_atr_still_fires():
    bars = _bars_long()
    params = EnsembleParams()
    entry = _first_bar(54)
    flat_bar = _first_bar(55)
    atr_bar = _first_bar(56)
    prior_sign = _prior_none(len(bars), {entry: -1})  # LONG from down prior -> REVERSAL
    st_b, sup_b = ensemble_variant_single_slot_reversal_hold(
        bars, params, prior_sign, hold_reversal=True)
    assert len(sup_b) == 1
    assert sup_b[0]["entry_index"] == entry and sup_b[0]["bar_index"] == flat_bar
    assert sup_b[0]["side"] == "LONG"
    # variant HOLDs with the pre-registered suppression reason (no flat close)
    assert st_b[flat_bar].signal == Signal.HOLD
    assert st_b[flat_bar].reason == SUPPRESS_REASON
    # the held leg is later stopped out by the FROZEN provider ATR stop, not flat
    assert st_b[atr_bar].signal == Signal.SELL
    assert st_b[atr_bar].reason == ATR_REASON
    # benchmark/off-stream emits the flat SELL close at the suppression bar
    st_off, _ = ensemble_variant_single_slot_reversal_hold(
        bars, params, prior_sign, hold_reversal=False)
    assert st_off[flat_bar].signal == Signal.SELL
    assert st_off[flat_bar].reason == FLAT_REASON


def test_reversal_short_flat_exit_suppressed_and_atr_still_fires():
    bars = _bars_short()
    params = EnsembleParams()
    entry = _first_bar(54)
    flat_bar = _first_bar(55)
    atr_bar = _first_bar(56)
    prior_sign = _prior_none(len(bars), {entry: 1})  # SHORT from up prior -> REVERSAL
    st_b, sup_b = ensemble_variant_single_slot_reversal_hold(
        bars, params, prior_sign, hold_reversal=True)
    assert len(sup_b) == 1
    assert sup_b[0]["entry_index"] == entry and sup_b[0]["bar_index"] == flat_bar
    assert sup_b[0]["side"] == "SHORT"
    assert st_b[flat_bar].signal == Signal.HOLD
    assert st_b[flat_bar].reason == SUPPRESS_REASON
    # short exits with BUY; still the frozen ATR stop on the down-side break
    assert st_b[atr_bar].signal == Signal.BUY
    assert st_b[atr_bar].reason == ATR_REASON
    st_off, _ = ensemble_variant_single_slot_reversal_hold(
        bars, params, prior_sign, hold_reversal=False)
    assert st_off[flat_bar].signal == Signal.BUY
    assert st_off[flat_bar].reason == FLAT_REASON


def test_continuation_flat_exit_untouched():
    bars = _bars_long()
    params = EnsembleParams()
    entry = _first_bar(54)
    flat_bar = _first_bar(55)
    prior_sign = _prior_none(len(bars), {entry: 1})  # LONG from up prior -> CONTINUATION
    st_b, sup_b = ensemble_variant_single_slot_reversal_hold(
        bars, params, prior_sign, hold_reversal=True)
    assert sup_b == []
    assert st_b[flat_bar].signal == Signal.SELL
    assert st_b[flat_bar].reason == FLAT_REASON


def test_actionable_deltas_confined_to_suppression_and_frozen_exits():
    """The ONLY actionable (BUY/SELL) deltas are the suppressed flat exits and
    the frozen-machinery closes that fire on the still-held legs afterwards."""
    bars = _bars_long()
    params = EnsembleParams()
    entry = _first_bar(54)
    prior_sign = _prior_none(len(bars), {entry: -1})
    st_off, _ = ensemble_variant_single_slot_reversal_hold(
        bars, params, prior_sign, hold_reversal=False)
    st_on, sup_b = ensemble_variant_single_slot_reversal_hold(
        bars, params, prior_sign, hold_reversal=True)

    def actionable(st):
        return {i for i, s in enumerate(st) if s is not None and s.signal != Signal.HOLD}

    removed = actionable(st_off) - actionable(st_on)
    # every removed actionable bar is exactly a suppression bar (flat exit -> HOLD)
    assert removed == {s["bar_index"] for s in sup_b}
    added = actionable(st_on) - actionable(st_off)
    # added closes are exclusively the frozen exits on the extended hold, strictly
    # AFTER the suppression that extended it
    for i in added:
        assert st_on[i].reason in (ATR_REASON, BROKEN_REASON, MAXHOLD_REASON), (i, st_on[i].reason)
        assert any(s["bar_index"] < i for s in sup_b), i
    # net flat exits removed == number of suppressed benchmark flat exits
    flats_off = sum(1 for s in st_off if s is not None
                    and s.signal != Signal.HOLD and s.reason == FLAT_REASON)
    flats_on = sum(1 for s in st_on if s is not None
                   and s.signal != Signal.HOLD and s.reason == FLAT_REASON)
    assert flats_off - flats_on == len(sup_b)


# ---------------------------------------------------------------------------
# 3. _reason_diff regression: LONG flat exits are SELL signals (SELL counted)
# ---------------------------------------------------------------------------


def test_reason_diff_counts_sell_side_flat_exits():
    bars = _bars_long(12)
    bridge = bars[_first_bar(1)]
    sig_bench = [None] * 1
    sig_var = [None] * 1
    sig_bench[0] = _close_signal(bridge, 1, FLAT_REASON, _base_meta(bridge, 0, 1))
    sig_var[0] = _hold(bridge, SUPPRESS_REASON, _base_meta(bridge, 0, 1))
    rows = _reason_diff(sig_bench, sig_var)
    assert any(r["reason"] == FLAT_REASON and r["delta"] == -1 for r in rows)


def test_reason_diff_counts_buy_side_flat_exits():
    bars = _bars_long(12)
    bridge = bars[_first_bar(1)]
    sig_bench = [None] * 1
    sig_var = [None] * 1
    sig_bench[0] = _close_signal(bridge, -1, FLAT_REASON, _base_meta(bridge, 0, 1))
    sig_var[0] = _hold(bridge, SUPPRESS_REASON, _base_meta(bridge, 0, 1))
    rows = _reason_diff(sig_bench, sig_var)
    assert any(r["reason"] == FLAT_REASON and r["delta"] == -1 for r in rows)


# ---------------------------------------------------------------------------
# 4. per-bar flat-suppression verification (criterion C)
# ---------------------------------------------------------------------------


def _long_run():
    bars = _bars_long()
    params = EnsembleParams()
    entry = _first_bar(54)
    prior_sign = _prior_none(len(bars), {entry: -1})
    st_bench = ensemble_variant(bars, params, use_trend=False, use_vol=True)
    st_b, sup_b = ensemble_variant_single_slot_reversal_hold(
        bars, params, prior_sign, hold_reversal=True)
    return bars, st_bench, st_b, sup_b


def test_flat_suppression_verified_all_checks():
    bars, st_bench, st_b, sup_b = _long_run()
    ver = flat_suppression_verified(st_bench, st_b, sup_b)
    assert ver["verified"] is True
    assert ver["echo_match"] is True
    assert ver["suppressed_holds"] is True
    assert ver["other_flat_unchanged"] is True
    assert ver["no_invented_flat"] is True
    assert ver["suppressions_matched"] == 1
    # the scenario re-enters (session 57) and flat-exits again (session 58):
    # benchmark 2 flats, variant keeps only the non-suppressed one
    assert ver["benchmark_flat_exits"] == 2
    assert ver["variant_flat_exits"] == 1


def test_flat_suppression_verified_rejects_missing_hold():
    bars, st_bench, st_b, sup_b = _long_run()
    flat_bar = _first_bar(55)
    tampered = list(st_b)
    tampered[flat_bar] = _close_signal(
        bars[flat_bar], 1, FLAT_REASON, _base_meta(bars[flat_bar], flat_bar, len(bars)))
    ver = flat_suppression_verified(st_bench, tampered, sup_b)
    assert ver["verified"] is False
    assert ver["suppressed_holds"] is False


def test_flat_suppression_verified_rejects_bad_echo():
    bars, st_bench, st_b, sup_b = _long_run()
    bogus = list(sup_b) + [{"bar_index": _first_bar(54), "day": "2022-01-01"}]
    ver = flat_suppression_verified(st_bench, st_b, bogus)
    assert ver["verified"] is False
    assert ver["echo_match"] is False


def test_flat_suppression_verified_rejects_invented_flat():
    bars, st_bench, st_b, sup_b = _long_run()
    idx = _first_bar(57)
    tampered = list(st_b)
    tampered[idx] = _close_signal(
        bars[idx], 1, FLAT_REASON, _base_meta(bars[idx], idx, len(bars)))
    ver = flat_suppression_verified(st_bench, tampered, sup_b)
    assert ver["verified"] is False
    assert ver["no_invented_flat"] is False


# ---------------------------------------------------------------------------
# 5. single-slot displacement ledger (side-agnostic, exit-inclusive)
# ---------------------------------------------------------------------------


def _rt(entry_index, exit_index, side):
    return {"entry": {"entry_index": entry_index}, "exit_index": exit_index, "side": side}


def _entry_signals(windows) -> list:
    """Build a signal stream containing exactly the given [open, close, side]
    windows.  Entry reasons use the variant's real prefixes so
    build_displacement_ledger classifies them as B entry events."""
    n = max((w[1] for w in windows), default=0) + 10
    bars = _bars_long(n)
    sig = [None] * n
    for wo, wx, side in windows:
        sig[wo] = _open_signal(
            bars[wo], 1 if side == "LONG" else -1,
            ("ensemble confluence up - enter long" if side == "LONG"
             else "ensemble confluence down - enter short"),
            _base_meta(bars[wo], wo, n))
        sig[wx] = _close_signal(
            bars[wx], 1 if side == "LONG" else -1, FLAT_REASON, _base_meta(bars[wx], wx, n))
    return sig


def test_displacement_side_agnostic_occupant():
    rts_a = [_rt(100, 150, "LONG"), _rt(120, 170, "SHORT")]
    sig_b = _entry_signals([(95, 130, "LONG")])
    sup = [{"entry_index": 95}]
    disp = build_displacement_ledger(rts_a, sig_b, sup)
    assert disp["displaced"] == 2
    assert disp["complete"] is True and disp["unexplained"] == []
    rows = {d["displaced_entry_index"]: d for d in disp["ledger"]}
    # both displaced entries attributed to the SINGLE long occupant window
    assert rows[100]["occupant"]["blocker_side"] == "LONG"
    assert rows[120]["occupant"]["blocker_side"] == "LONG"
    assert rows[100]["occupant"]["blocker_entry_class"] == "REVERSAL"


def test_displacement_exit_bar_inclusive():
    # A entry at the exact bar a B leg exited is a real displacement
    # (frozen machine never same-bar re-enters after a close).
    rts_a = [_rt(130, 180, "LONG")]
    sig_b = _entry_signals([(95, 130, "LONG")])
    disp = build_displacement_ledger(rts_a, sig_b, [{"entry_index": 95}])
    assert disp["displaced"] == 1
    assert disp["complete"] is True
    assert disp["ledger"][0]["occupant"]["blocker_exit_index"] == 130


def test_displacement_unexplained_when_outside_windows():
    rts_a = [_rt(200, 250, "LONG")]
    sig_b = _entry_signals([(95, 130, "LONG")])
    disp = build_displacement_ledger(rts_a, sig_b, [{"entry_index": 95}])
    assert disp["displaced"] == 1
    assert disp["complete"] is False
    assert disp["unexplained"] == [{"entry_index": 200, "side": "LONG"}]


def test_displacement_no_delta_when_identitical():
    rts_a = [_rt(95, 130, "LONG")]
    sig_b = _entry_signals([(95, 130, "LONG")])
    disp = build_displacement_ledger(rts_a, sig_b, [])
    assert disp["displaced"] == 0 and disp["complete"] is True


# ---------------------------------------------------------------------------
# 6. acceptance + classification (pre-registered)
# ---------------------------------------------------------------------------


def _trans(a="10", b="15"):
    def cells(per_rt, wins, rt):
        return {"REVERSAL_LONG": {"rt": rt, "net": str(Decimal(per_rt) * rt), "wins": wins},
                "REVERSAL_SHORT": {"rt": 0, "net": "0", "wins": 0}}
    return cells(a, 6, 10), cells(b, 7, 10)


def _res(anet="1000", bnet="1300", art=100, brt=100, amaxdd="100", bmaxdd="110",
         aslip="500", acomm="500", bslip="500", bcomm="500"):
    return {
        "A": {"net": Decimal(anet), "rt": art, "max_dd": Decimal(amaxdd),
              "slippage": aslip, "commission": acomm, "win_rate": 60.0},
        "B": {"net": Decimal(bnet), "rt": brt, "max_dd": Decimal(bmaxdd),
              "slippage": bslip, "commission": bcomm, "win_rate": 62.0},
    }


def _args(tra=None, trb=None, halves=None, flat=True, causality=0, det=True,
          unexplained=None, slots=True):
    tra, trb = tra or _trans()[0], trb or _trans()[1]
    return {
        "ident": {"A": {"closed_identity_holds": True, "full_identity_holds": True},
                  "B": {"closed_identity_holds": True, "full_identity_holds": True}},
        "rts_a": [], "rts_b": [],
        "trans_a": tra, "trans_b": trb,
        "halves": halves if halves is not None else {"h1_rev_improves": True,
                                                     "h2_rev_improves": True},
        "causality_violations": causality,
        "flat_ver": {"verified": flat},
        "single_slot": {"holds": slots, "max_open_qty": 1 if slots else 2, "violations": []},
        "disp": {"unexplained": unexplained if unexplained is not None else [],
                 "complete": not (unexplained if unexplained is not None else [])},
        "suppressions_count": 1,
        "det_equal": det,
    }


def test_acceptance_all_pass_promising():
    acc = evaluate_acceptance(_res(), **_args())
    assert acc["all_acceptance_satisfied"] is True
    assert acc["failed"] == []
    assert classify_research(acc) == "PROMISING"


def test_acceptance_net_lower_still_research_candidate():
    acc = evaluate_acceptance(_res(anet="1300", bnet="1200"), **_args())
    assert acc["criteria"]["F_net_not_worse"] is False
    assert acc["all_acceptance_satisfied"] is False
    # every INTEGRITY criterion and every other ECONOMIC criterion still hold
    for k in INTEGRITY_CRITERIA:
        assert acc["criteria"][k] is True, k
    for k in ECONOMICS_CRITERIA:
        if k != "F_net_not_worse":
            assert acc["criteria"][k] is True, k
    assert classify_research(acc) == "RESEARCH CANDIDATE"


def test_acceptance_drawdown_unsafe_rejects():
    acc = evaluate_acceptance(_res(bmaxdd="130"), **_args())
    assert acc["criteria"]["G_risk_safe"] is False
    assert classify_research(acc) == "REJECTED"


def test_acceptance_cost_efficiency_break_per_rt_rejects():
    res = _res(art=100, brt=88, bslip="495", bcomm="495")  # 990 cost, 88 RT -> 11.25/RT > 1.1x
    acc = evaluate_acceptance(res, **_args())
    assert acc["criteria"]["H_cost_efficiency"] is False
    assert classify_research(acc) == "REJECTED"


def test_acceptance_flat_verification_failure_rejects():
    acc = evaluate_acceptance(_res(), **_args(flat=False))
    assert acc["criteria"]["C_flat_suppression_only"] is False
    assert classify_research(acc) == "REJECTED"


def test_acceptance_causality_violation_fails_j():
    acc = evaluate_acceptance(_res(), **_args(causality=2))
    assert acc["criteria"]["J_accounting_causality_determinism"] is False
    assert classify_research(acc) == "REJECTED"


def test_acceptance_half_failure_fails_temporal_stability():
    halves = {"h1_rev_improves": True, "h2_rev_improves": False}
    acc = evaluate_acceptance(_res(), **_args(halves=halves))
    assert acc["criteria"]["I_temporal_stability"] is False
    assert classify_research(acc) == "REJECTED"


def test_acceptance_single_slot_violation_fails_A():
    acc = evaluate_acceptance(_res(), **_args(slots=False))
    assert acc["criteria"]["A_engine_single_slot_b"] is False
    assert classify_research(acc) == "REJECTED"


def test_acceptance_unexplained_displacement_fails_D():
    acc = evaluate_acceptance(_res(), **_args(unexplained=[{"entry_index": 5}]))
    assert acc["criteria"]["A_engine_single_slot_b"] is True  # slot invariant holds
    assert acc["criteria"]["D_displacement_ledger"] is False
    assert classify_research(acc) == "REJECTED"


def test_classification_rule():
    all_true = {k: True for k in (INTEGRITY_CRITERIA + ECONOMICS_CRITERIA)}
    acc_ok = {"criteria": dict(all_true),
              "all_acceptance_satisfied": True, "net_A": "12065", "net_B": "13000"}
    assert classify_research(acc_ok) == "PROMISING"
    # net lower (F fails) with everything else holding => RESEARCH CANDIDATE
    acc_cand = {"criteria": {**all_true, "F_net_not_worse": False},
                "all_acceptance_satisfied": False, "net_A": "12065", "net_B": "11000"}
    assert classify_research(acc_cand) == "RESEARCH CANDIDATE"
    # an integrity failure rejects regardless of economics
    acc_bad = {"criteria": {**all_true, "A_engine_single_slot_b": False},
               "all_acceptance_satisfied": False, "net_A": "12065", "net_B": "13000"}
    assert classify_research(acc_bad) == "REJECTED"
    # a flipped economic gate rejects
    acc_bad2 = {"criteria": {**all_true, "G_risk_safe": False},
                "all_acceptance_satisfied": False, "net_A": "12065", "net_B": "13000"}
    assert classify_research(acc_bad2) == "REJECTED"


# ---------------------------------------------------------------------------
# 7. artifact integrity + recorded reproducibility
# ---------------------------------------------------------------------------

PROVENANCE = (
    ("iteration_009_vol_led.json", ITER9_RESULT_SHA256),
    ("iteration_010_sideways_veto.json", ITER10_RESULT_SHA256),
    ("iteration_011_vol_expansion_quality.json", ITER11_RESULT_SHA256),
    ("iteration_012_trade_forensics.json", ITER12_RESULT_SHA256),
    ("iteration_012_validation.json", ITER12_VALIDATION_SHA256),
    ("our_algo_001_transition_quality.json", OUR_ALGO_001_SHA256),
    ("our_algo_002_transition_exit.json", OUR_ALGO_002_SHA256),
)


def test_artifact_exists_and_sha_fixed():
    assert OUT_FILE.exists()
    assert _sha256(OUT_FILE) == OUR_ALGO_003_OUT_SHA256


def test_artifact_provenance_artifacts_unchanged():
    for name, h in PROVENANCE:
        p = R / name
        assert p.exists() and _sha256(p) == h, name


def test_artifact_benchmark_matches_iter09_record():
    out = _load()
    assert out["benchmark_frozen"]["guard_equal_iteration009"] is True
    for k, exp in ITER9_VOL_LED_EXPECTED.items():
        got = out["economics"]["A_vol_led"][k]
        assert str(got) == str(exp) or (isinstance(got, float) and got == exp), k


def test_artifact_integrity_guarantees():
    out = _load()
    # D: signal-stream single-slot occupancy ledger is complete (no unexplained skips).
    assert out["displacement_ledger"]["displaced"] >= 0
    assert out["displacement_ledger"]["complete"] is True
    assert out["displacement_ledger"]["unexplained"] == []
    assert out["displacement_ledger"]["b_entry_events"] > 0
    # A: the recorded journal did NOT cleanly hold a single unit in every bar
    # (engine fill stacking under long-carry divergence) -> honestly recorded.
    assert out["single_slot_invariant"]["holds"] is False
    assert out["single_slot_invariant"]["max_open_qty"] > 1
    assert out["acceptance"]["criteria"]["A_engine_single_slot_b"] is False
    # C: reason-level isolation is coupled to displacement at scale ->
    # suppressed_holds and no_invented_flat verified; echo/other-flat cannot
    # hold because displaced legs remove flats without a suppression bar.
    assert out["acceptance"]["flat_suppression"]["verified"] is False
    assert out["acceptance"]["flat_suppression"]["suppressed_holds"] is True
    assert out["acceptance"]["flat_suppression"]["no_invented_flat"] is True
    assert out["acceptance"]["flat_suppression"]["echo_match"] is False
    assert out["acceptance"]["flat_suppression"]["other_flat_unchanged"] is False
    assert out["acceptance"]["criteria"]["C_flat_suppression_only"] is False
    assert out["acceptance"]["suppression_count"] >= 1


def test_artifact_criteria_partition_exact():
    out = _load()
    keys = set(out["acceptance"]["criteria"])
    assert keys == set(INTEGRITY_CRITERIA + ECONOMICS_CRITERIA)
    assert set(out["acceptance"]["satisfied"] + out["acceptance"]["failed"]) == keys
    assert len(out["acceptance"]["satisfied"]) + len(out["acceptance"]["failed"]) == len(keys)


def test_artifact_classification_consistent_with_criteria():
    out = _load()
    acc = out["acceptance"]
    ok = acc["all_acceptance_satisfied"]
    if ok:
        expected = ("PROMISING" if Decimal(out["comparison"]["net_delta_B_minus_A"]) >= 0
                    else "RESEARCH CANDIDATE")
    else:
        expected = "REJECTED"
    assert out["classification"] == expected


def test_artifact_causality_firewall_and_safety():
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


def test_artifact_experiment_isolates_one_structural_change():
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


def test_artifact_repeatability_halves_warmed_independently():
    out = _load()
    rep = out["repeatability"]
    assert rep["split"]["window1"][0] == "2022-01-03"
    assert rep["split"]["note"].startswith("pre-registered chronological halves")
    h1, h2 = rep["half1"], rep["half2"]
    assert h1["causality_violations"] == 0 and h2["causality_violations"] == 0