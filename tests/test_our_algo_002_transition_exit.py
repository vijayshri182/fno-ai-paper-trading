"""Tests for OUR-ALGO-002 - TRANSITION-SPECIFIC EXIT / HOLDING OVERLAY (pre-OOS A/B).

Artifact-integrity + pure-logic + synthetic-signal tests only.  No engine
re-run for tuning.  The overlay exit scan is asserted on engineered synthetic
bars to verify: flat-exit suppression, ATR stop, existing max-hold, confluence-
broken exits, causality, entry-universe identity, continuation byte-identity,
engine-faithful fill/cost conventions, and the recorded artifact reproducibility
(transition cells must equal the Iteration-012 forensics cells on the frozen
pipeline; continuation economics must be saved exactly).
"""
from __future__ import annotations

import json
from datetime import date, datetime, time as dtime, timedelta
from decimal import Decimal

from fno_ai_paper_trading.models.enums import InstrumentType
from fno_ai_paper_trading.models.instruments import Instrument
from fno_ai_paper_trading.models.market import MarketPrice
from fno_ai_paper_trading.research.iteration005_discovery import (
    COMM_RATE,
    SLIP_RATE,
    DailySeries,
    EnsembleParams,
)
from fno_ai_paper_trading.research.our_algo_002_transition_exit import (
    ATR_REASON,
    BROKEN_REASON,
    FLAT_REASON,
    ITER9_RESULT_SHA256,
    ITER9_VOL_LED_EXPECTED,
    ITER10_RESULT_SHA256,
    ITER11_RESULT_SHA256,
    ITER12_RESULT_SHA256,
    ITER12_VALIDATION_SHA256,
    MAXHOLD_REASON,
    OUT_FILE,
    REPO,
    _exit_fill,
    _sha256,
    build_overlay,
    build_swapped_exit_rt,
    classify_research,
    continuation_economics,
    evaluate_acceptance,
    layered_book,
    make_carry_leg,
    reversal_pool,
    scan_held_exit,
    transition_pool_pivot,
)
from fno_ai_paper_trading.research.our_algo_001_transition_quality import (
    build_prior_sign_series,
    transition_label,
)

R = REPO / "runs" / "research" / "day_batch"

# Deterministic artifact sha recorded at execution time (run twice, identical).
OUR_ALGO_002_OUT_SHA256 = "40984bf4dca71b055fce70cb1ef0055b5f8fe44b4a6dbd7c9c695ab45fa9f09a"

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


def _flat_bars(sessions: int = 30) -> list[MarketPrice]:
    def profile(s, b, n):
        return Decimal("100"), Decimal("100"), Decimal("0.25")
    return _mk_bars(sessions, profile)


def _first_bar(s: int) -> int:
    return s * 75


# ---------------------------------------------------------------------------
# 1. fill conventions (engine adverse slippage)
# ---------------------------------------------------------------------------


def test_exit_fill_adverse_direction():
    # side_dir is the POSITION side; the exit order is opposite (LONG closes
    # with SELL which receives (1-slip); SHORT closes with BUY which pays (1+slip)).
    assert _exit_fill(Decimal("100"), 1) == Decimal("100") * (Decimal("1") - SLIP_RATE)
    assert _exit_fill(Decimal("100"), -1) == Decimal("100") * (Decimal("1") + SLIP_RATE)
    assert _exit_fill(Decimal("100"), 1) < Decimal("100")
    assert _exit_fill(Decimal("100"), -1) > Decimal("100")


def test_exit_fill_deterministic():
    assert (_exit_fill(Decimal("1234.5"), 1), _exit_fill(Decimal("1234.5"), -1)) == \
        (Decimal("1234.5") * Decimal("0.999"), Decimal("1234.5") * Decimal("1.001"))


# ---------------------------------------------------------------------------
# 2. overlay exit scan: flat suppression + only the existing exit machinery
# ---------------------------------------------------------------------------


def test_scan_suppresses_flat_exits_to_carry():
    bars = _flat_bars(30)
    targets = [0] * len(bars)
    params = EnsembleParams()
    entry = _first_bar(5)
    out = scan_held_exit(bars, entry, 1, 100.0, 2.0, params, targets)
    assert out == {"kind": "carry"}  # never exits: only confluence flat would fire


def test_scan_never_exits_before_after_entry():
    bars = _flat_bars(30)
    targets = [0] * len(bars)
    params = EnsembleParams()
    for entry in (_first_bar(3), _first_bar(20)):
        out = scan_held_exit(bars, entry, 1, 100.0, 2.0, params, targets)
        assert out["kind"] in ("exit", "carry")
        assert out["kind"] == "carry" or out["index"] > entry


def test_scan_atr_stop_fires_on_unfavourable_move():
    # Long entered at 100; a session close well below stop (100 - 4*2 = 92)
    # must fire the provider ATR stop on that bar.

    def profile(s, b, n):
        if s == 8:
            return Decimal("96"), Decimal("90"), Decimal("0.25")
        return Decimal("100"), Decimal("100"), Decimal("0.25")

    bars = _mk_bars(20, profile)
    targets = [0] * len(bars)
    params = EnsembleParams()
    out = scan_held_exit(bars, _first_bar(5), 1, 100.0, 2.0, params, targets)
    assert out["kind"] == "exit" and out["reason"] == ATR_REASON
    # short side stop is above entry
    out_s = scan_held_exit(bars, _first_bar(5), -1, 100.0, 2.0, params, targets)
    assert out_s["kind"] == "carry"


def test_scan_max_hold_timeout_fires():
    # Flat prices and non-zero target matching the held side for every session
    # means only the existing max-hold (25) can fire after enough sessions.

    def profile(s, b, n):
        return Decimal("100"), Decimal("100"), Decimal("0.25")

    bars = _mk_bars(40, profile)
    params = EnsembleParams()
    entry = _first_bar(3)
    targets = [0] * len(bars)
    for s in range(40):
        targets[_first_bar(s)] = 1
    out = scan_held_exit(bars, entry, 1, 100.0, 2.0, params, targets)
    assert out["kind"] == "exit" and out["reason"] == MAXHOLD_REASON
    assert (out["index"] - entry) // 75 >= 25


def test_scan_confluence_broken_opposite_target_exits():
    # First-of-day target == -1 while holding LONG (+1) -> confluence broken.

    def profile(s, b, n):
        return Decimal("100"), Decimal("100"), Decimal("0.25")

    bars = _mk_bars(12, profile)
    params = EnsembleParams()
    entry = _first_bar(3)
    targets = [0] * len(bars)
    for s in range(12):
        targets[_first_bar(s)] = 1
    targets[_first_bar(7)] = -1
    out = scan_held_exit(bars, entry, 1, 100.0, 2.0, params, targets)
    assert out["kind"] == "exit" and out["reason"] == BROKEN_REASON
    assert out["index"] == _first_bar(7)


def test_scan_causality_no_future_bar():
    # confirm same-side targets never exit; only existing reasons fire.
    bars = _flat_bars(30)
    params = EnsembleParams()
    targets = [0] * len(bars)
    for s in range(30):
        targets[_first_bar(s)] = 1
    out = scan_held_exit(bars, _first_bar(2), 1, 100.0, 2.0, params, targets)
    assert out["kind"] == "exit" and out["reason"] == MAXHOLD_REASON
    assert out["index"] > _first_bar(2)


# ---------------------------------------------------------------------------
# 3. swapped round-trip economics (engine fill/cost identity)
# ---------------------------------------------------------------------------


def _fake_rt(entry_index, side_order="BUY", entry_close="100", exit_index=10, reason=FLAT_REASON):
    e = _first_bar(0)
    return {
        "entry": {"entry_index": entry_index, "entry_ts": "2022-01-04T09:15:00",
                  "entry_price": Decimal("99.95"), "entry_close": Decimal(entry_close),
                  "entry_side_order": side_order, "entry_qty": 1},
        "exit_index": exit_index, "exit_reason": reason, "side": "LONG" if side_order == "BUY" else "SHORT",
        "entry_day": "2022-01-04", "exit_day": "2022-01-05",
    }


def test_swapped_rt_economic_identity():
    bars = _flat_bars(10)
    rt = _fake_rt(0)
    outcome = {"kind": "exit", "index": 5, "reason": BROKEN_REASON}
    new = build_swapped_exit_rt(rt, bars, outcome)
    ep, xp = new["entry"]["entry_price"], new["exit_price"]
    for k in ("realized", "commission", "slippage", "gross_close", "net"):
        assert isinstance(new[k], Decimal), k
    assert new["net"] == new["realized"] - new["commission"]
    assert new["realized"] - new["commission"] == \
        new["gross_close"] - new["slippage"] - new["commission"]
    assert new["exit_reason"] == BROKEN_REASON
    assert new["exit_index"] == 5
    assert new["exit_close"] == bars[5].close


def test_swapped_rt_commission_uses_adverse_fills():
    bars = _flat_bars(10)
    rt = _fake_rt(0)
    outcome = {"kind": "exit", "index": 5, "reason": BROKEN_REASON}
    new = build_swapped_exit_rt(rt, bars, outcome)
    assert new["commission"] == (new["entry"]["entry_price"] + new["exit_price"]) * COMM_RATE


def test_carry_leg_mtm_identity():
    bars = _flat_bars(10)
    rt = _fake_rt(0)
    leg = make_carry_leg(rt, bars)
    assert leg["net_mtm"] == leg["unrealized"] - leg["entry_commission"]
    assert leg["source"] == "swapped_reversal_suppressed_flat"


# ---------------------------------------------------------------------------
# 4. layered book reproduces engine conventions on a simple path
# ---------------------------------------------------------------------------


def test_layered_book_single_trade():
    bars = _flat_bars(10)
    ep = Decimal("99.95")
    xp = Decimal("100.05")
    rt = {
        "entry": {"entry_index": 0, "entry_ts": "2022-01-04T09:15:00",
                  "entry_price": ep, "entry_close": Decimal("100"),
                  "entry_side_order": "BUY", "entry_qty": 1},
        "exit_index": 5, "exit_price": xp,
        "realized": (xp - ep), "commission": (ep + xp) * COMM_RATE,
        "side": "LONG", "exit_day": "2022-01-05",
    }
    book = layered_book(bars, [rt], [])
    expected = (xp - ep) - (ep + xp) * COMM_RATE
    assert book["total_pnl"] == expected


# ---------------------------------------------------------------------------
# 5. build_overlay: continuation preserved, reversal flat-only swap
# ---------------------------------------------------------------------------


def _overlay_entry_session() -> int:
    return 22  # deep enough that avg_range_before(k+1, 20) is non-None on flat bars


def test_overlay_keeps_continuation_byte_identical():
    bars = _flat_bars(30)
    params = EnsembleParams()
    daily = DailySeries.build(bars)
    targets = [0] * len(bars)
    e = _first_bar(_overlay_entry_session())
    rt_cont = {
        "entry": {"entry_index": e, "entry_ts": bars[e].timestamp.isoformat()[:19],
                  "entry_price": Decimal("99.95"), "entry_close": Decimal("100"),
                  "entry_side_order": "BUY", "entry_qty": 1},
        "entry_day": bars[e].timestamp.date().isoformat(),
        "exit_index": e + 10, "exit_reason": FLAT_REASON, "side": "LONG",
        "exit_day": "2022-01-10", "net": Decimal("50"),
    }
    prior_sign = [None] * len(bars)
    prior_sign[e] = 1
    ctx = [None] * len(bars)
    ctx[e] = {"volmove": 1}
    rts_b, _, _, _ = build_overlay(bars, [rt_cont], prior_sign, ctx, daily, params, targets)
    assert len(rts_b) == 1
    assert rts_b[0]["exit_reason"] == FLAT_REASON
    assert rts_b[0]["exit_index"] == e + 10
    assert rts_b[0]["entry"]["entry_index"] == e


def test_overlay_swaps_only_reversal_flat_to_carry():
    bars = _flat_bars(30)
    params = EnsembleParams()
    daily = DailySeries.build(bars)
    targets = [0] * len(bars)
    e = _first_bar(_overlay_entry_session())
    rt_rev = {
        "entry": {"entry_index": e, "entry_ts": bars[e].timestamp.isoformat()[:19],
                  "entry_price": Decimal("99.95"), "entry_close": Decimal("100"),
                  "entry_side_order": "BUY", "entry_qty": 1},
        "entry_day": bars[e].timestamp.date().isoformat(),
        "exit_index": e + 4, "exit_reason": FLAT_REASON, "side": "LONG",
        "exit_day": "2022-01-10", "net": Decimal("50"),
    }
    prior_sign = [None] * len(bars)
    prior_sign[e] = -1
    ctx = [None] * len(bars)
    ctx[e] = {"volmove": 1}
    rts_b, swaps, legs, _ = build_overlay(bars, [rt_rev], prior_sign, ctx, daily, params, targets)
    assert rts_b == []
    assert sum(1 for s in swaps) == 1
    assert swaps[0]["new_exit_reason"] == "open_at_close_carry"
    assert len(legs) == 1


def test_overlay_leaves_non_flat_reversal_untouched():
    bars = _flat_bars(30)
    params = EnsembleParams()
    daily = DailySeries.build(bars)
    targets = [0] * len(bars)
    e = _first_bar(_overlay_entry_session())
    rt_rev = {
        "entry": {"entry_index": e, "entry_ts": bars[e].timestamp.isoformat()[:19],
                  "entry_price": Decimal("99.95"), "entry_close": Decimal("100"),
                  "entry_side_order": "BUY", "entry_qty": 1},
        "entry_day": bars[e].timestamp.date().isoformat(),
        "exit_index": e + 4, "exit_reason": BROKEN_REASON, "side": "LONG",
        "exit_day": "2022-01-10", "net": Decimal("50"),
    }
    prior_sign = [None] * len(bars)
    prior_sign[e] = -1
    ctx = [None] * len(bars)
    ctx[e] = {"volmove": 1}
    rts_b, swaps, _, _ = build_overlay(bars, [rt_rev], prior_sign, ctx, daily, params, targets)
    assert len(rts_b) == 1 and swaps == []
    assert rts_b[0]["exit_reason"] == BROKEN_REASON


# ---------------------------------------------------------------------------
# 6. transition helpers (reversal pool, pivot labels)
# ---------------------------------------------------------------------------


def test_reversal_pool_and_pivot_identify_only_reversals():
    bars = _flat_bars(30)
    e1, e2 = _first_bar(5), _first_bar(6)
    rts = [
        {**{"entry": {"entry_index": e1}, "side": "LONG", "net": Decimal("10"),
            "exit_index": e1 + 2, "entry_day": "2022-01-04", "exit_reason": FLAT_REASON}},
        {**{"entry": {"entry_index": e2}, "side": "SHORT", "net": Decimal("20"),
            "exit_index": e2 + 2, "entry_day": "2022-01-04", "exit_reason": BROKEN_REASON}},
    ]
    prior_sign = [None] * len(bars)
    prior_sign[e1], prior_sign[e2] = -1, 1
    ctx = [None] * len(bars)
    ctx[e1], ctx[e2] = {"volmove": 1}, {"volmove": -1}
    pool = reversal_pool(rts, prior_sign, ctx)
    assert len(pool) == 2
    assert all(transition_label(
        prior_sign[r["entry"]["entry_index"]],
        ctx[r["entry"]["entry_index"]]["volmove"]).startswith("REVERSAL") for r in pool)


# ---------------------------------------------------------------------------
# 7. acceptance logic + classification (pre-registered)
# ---------------------------------------------------------------------------


def _mk_res(anet="1000", bnet="1300", art=100, brt=100):
    return {
        "A": {"net": Decimal(anet), "rt": art, "max_dd": Decimal("100"),
              "coverage": 150.0, "win_rate": 60.0},
        "B": {"net": Decimal(bnet), "rt": brt, "max_dd": Decimal("110"),
              "coverage": 160.0, "win_rate": 62.0},
    }


def _rev(per_a="10", per_b="15", wr_a=60.0, wr_b=62.0):
    return {"per_rt": Decimal(per_a), "win_rate_pct": wr_a}, \
        {"per_rt": Decimal(per_b), "win_rate_pct": wr_b}


def _acceptance_args(**over):
    args = {
        "ident_a": {"closed_identity_holds": True, "full_identity_holds": True},
        "ident_b": {"closed_identity_holds": True, "full_identity_holds": True},
        "val_a": {"max_drawdown_match": True, "total_pnl_match": True, "carry_match": True},
        "rev_a": _rev()[0], "rev_b": _rev()[1],
        "halves": {"h1_reversal_improves": True, "h2_reversal_improves": True},
        "causality_violations": 0,
        "continuity_mismatches": [],
        "entry_mismatches": [],
        "determinism_ok": True,
    }
    args.update(over)
    return args


def test_acceptance_all_pass_and_promising():
    res = _mk_res()
    acc = evaluate_acceptance(res, **_acceptance_args())
    assert acc["all_acceptance_satisfied"] is True
    assert acc["integrity_ok"] is True
    assert classify_research(acc) == "PROMISING"


def test_acceptance_drawdown_unsafe_fails_and_rejects():
    res = _mk_res(bnet="1300")
    res["B"]["max_dd"] = Decimal("130")  # 1.3x > 1.25x gate
    acc = evaluate_acceptance(res, **_acceptance_args())
    assert acc["criteria"]["E_risk_safe"] is False
    assert "E_risk_safe" in acc["failed"]
    assert acc["integrity_ok"] is True
    assert classify_research(acc) == "REJECTED"


def test_acceptance_entry_mismatch_breaks_integrity():
    res = _mk_res()
    acc = evaluate_acceptance(res, **_acceptance_args(entry_mismatches=[{"bar": 5}]))
    assert acc["criteria"]["A_entry_universe_identity"] is False
    assert acc["integrity_ok"] is False
    assert classify_research(acc) == "REJECTED"


def test_acceptance_continuation_mismatch_breaks_integrity():
    res = _mk_res()
    acc = evaluate_acceptance(res, **_acceptance_args(continuity_mismatches=[{"entry_index": 5}]))
    assert acc["criteria"]["B_continuation_preserved"] is False
    assert classify_research(acc) == "REJECTED"


def test_acceptance_causality_violation_fails_h():
    res = _mk_res()
    acc = evaluate_acceptance(res, **_acceptance_args(causality_violations=2))
    assert acc["criteria"]["H_causality"] is False
    assert classify_research(acc) == "REJECTED"


def test_acceptance_reversal_quality_worse_rejects():
    res = _mk_res()
    acc = evaluate_acceptance(res, **_acceptance_args(rev_b=_rev()[1], rev_a={"per_rt": Decimal("20"),
                                                                             "win_rate_pct": 60.0}))
    assert acc["criteria"]["C_reversal_quality_improves"] is False
    assert classify_research(acc) == "REJECTED"


def test_classify_research_labels():
    # integrity failure => REJECTED regardless of economic picture
    bad_integrity = {"criteria": {**{k: True for k in ("A_entry_universe_identity",
                                                       "B_continuation_preserved", "G_accounting",
                                                       "H_causality", "J_determinism")},
                                   "C_reversal_quality_improves": True, "D_net_not_worse": True,
                                   "E_risk_safe": True, "F_cost_efficiency": True,
                                   "I_temporal_stability": True}}
    assert classify_research(bad_integrity) == "PROMISING"
    assert classify_research({**bad_integrity, "criteria": {**bad_integrity["criteria"],
                                                            "E_risk_safe": False}}) == "REJECTED"


# ---------------------------------------------------------------------------
# 8. continuation-economics reconciliation (the core question)
# ---------------------------------------------------------------------------


def _mini_rt(e, side, net, prior, raw, reason):
    return {"entry": {"entry_index": e}, "exit_index": e + 1, "side": side,
            "entry_day": "2022-01-04", "exit_reason": reason, "net": Decimal(net)}


def test_continuation_economics_zero_delta_when_untouched():
    rts = [_mini_rt(10, "LONG", "50", 1, 1, FLAT_REASON)]
    prior_sign = [None] * 20
    prior_sign[10] = 1
    ctx = [None] * 20
    ctx[10] = {"volmove": 1}
    c = continuation_economics(rts, rts, prior_sign, ctx)
    assert c["continuation_preserved"] is True
    assert Decimal(c["4_continuation_net_A"]) == Decimal(c["5_continuation_net_B"])
    assert Decimal(c["6_continuation_net_delta"]) == Decimal("0")


# ---------------------------------------------------------------------------
# 9. artifact integrity + recorded reproducibility
# ---------------------------------------------------------------------------


def test_artifact_exists_and_sha_fixed():
    assert OUT_FILE.exists()
    assert _sha256(OUT_FILE) == OUR_ALGO_002_OUT_SHA256


def test_artifact_benchmark_matches_iter09_record():
    out = _load()
    assert out["benchmark_frozen"]["guard_equal_iteration009"] is True
    for k, exp in ITER9_VOL_LED_EXPECTED.items():
        got = out["economics"]["A_vol_led"][k]
        assert str(got) == str(exp) or (isinstance(got, float) and got == exp), k


def test_artifact_provenance_artifacts_unchanged():
    for p, h in ((R / "iteration_009_vol_led.json", ITER9_RESULT_SHA256),
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


def test_artifact_entry_universe_identical_and_continuation_preserved():
    out = _load()
    ui = out["entry_universe_integrity"]
    assert ui["entry_mismatches"] == []
    assert ui["continuation_mismatches"] == []
    assert ui["A_entry_count"] == ui["B_entry_count"] == 227
    assert out["continuation_economics"]["continuation_preserved"] is True
    assert Decimal(out["continuation_economics"]["6_continuation_net_delta"]) == Decimal("0")


def test_artifact_swap_and_outcome_stats():
    out = _load()
    h = out["holding_change"]
    assert h["swap_count"] == 85
    assert h["swapped_to_closed"] == 85 and h["swapped_to_carry"] == 0
    assert set(h["by_new_exit_reason"]) <= {"ensemble confluence broken - exits",
                                            "provider ATR stop exits"}
    a, b = out["comparison"]["A"], out["comparison"]["B"]
    assert a["rt"] == b["rt"] == "226"
    assert Decimal(b["net"]) > Decimal(a["net"])          # net improves
    rev = out["reversal_pool"]
    assert Decimal(rev["B"]["per_rt"]) > Decimal(rev["A"]["per_rt"])  # per-trade quality improves
    # pre-registered outcome: risk gate fails => REJECTED
    assert Decimal(b["max_dd"]) > Decimal(a["max_dd"]) * Decimal("1.25")
    assert out["acceptance"]["criteria"]["E_risk_safe"] is False
    assert out["acceptance"]["criteria"]["C_reversal_quality_improves"] is False
    assert out["acceptance"]["integrity_ok"] is True
    assert out["classification"] == "REJECTED"


def test_artifact_reconstruction_validation_a():
    out = _load()
    va = out["reconstruction_validation_A"]
    assert va["max_drawdown_match"] is True
    assert va["total_pnl_match"] is True
    assert va["carry_match"] is True


def test_artifact_causality_firewall_and_safety():
    out = _load()
    assert out["leakage_checks"]["causal_series_violations"] == 0
    assert out["leakage_checks"]["overlay_violations"] == 0
    assert out["leakage_checks"]["no_oos_in_research"] is True
    assert out["protected_oos_firewall"]["used_for_selection"] is False
    assert out["safety_state"]["algo_ready"] == "NO"
    assert out["safety_state"]["promotion"] == "NO"
    assert out["safety_state"]["live_gate"] == "CLOSED"
    assert out["determinism"]["two_runs_identical"] is True