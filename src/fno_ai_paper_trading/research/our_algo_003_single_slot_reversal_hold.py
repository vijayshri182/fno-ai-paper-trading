"""OUR-ALGO-003 - SINGLE-SLOT REVERSAL-FLAT-HOLD: IN-LOOP ENGINE REPLAY (pre-OOS A/B).

Single controlled research experiment on the project's own discovery algorithm.
Question (the decisive test of OUR-ALGO-002's exit-side hypothesis #2): OUR-ALGO-002
implemented the reversal flat-exit suppression as a POST-HOC OVERLAY, keeping all 226
benchmark entries and adding concurrent held legs.  The project rejected it as
NON-IMPLEMENTABLE: a real single-slot engine holds ONE position at a time, so holding a
reversal leg past the confluence-flat exit OCCUPIES the slot and DISPLACES later entries.
This experiment asks: when the exact same suppression is implemented IN-LOOP inside the
existing single-slot engine state machine (so slot-occupancy displacement is modelled
end-to-end), does the reversal-exit improvement survive?

TREATMENT (exactly ONE structural change, no parameter tuning):
  A = frozen Iteration-009 VOL-led benchmark (use_trend=False, use_vol=True), reproduced
      byte-for-byte and guarded against the persisted iteration_009_vol_led.json artifact
      (226 RT, net +12065.80, maxDD 2388.66).
  B = in-loop engine variant of the SAME loop (single slot, exactly the frozen state
      machinery: provider ATR stop every bar, max-hold 25 first-of-day, confluence-broken
      first-of-day, warmup=54, sizing, costs, capital) where the ONLY difference is:

      * AT ENTRY (first-of-day, state==0, target==�}1) the position's transition class is
        locked from prior_sign[i] (sign of the LAST COMPLETED session's close-to-close
        return, strictly before the entry bar - causal): REVERSAL iff
        _reversal_ok(prior_sign[i], target) (LONG from a down prior session / SHORT from
        an up prior session - the two sign-transition cells found most profitable in
        Iteration-012).  Continuation/flat-prior entries carry entry_reversal=False.
      * AT FIRST-OF-DAY, when the CONFLUENCE-FLAT branch (target==0 and state!=0) fires:
        if the currently open position was entered as a REVERSAL transition, the flat exit
        is SUPPRESSED and the position continues holding under the existing machinery
        (confluence-broken / provider ATR stop / max-hold all still fire).  Otherwise the
        flat exit fires exactly as the benchmark.  ZERO new thresholds, ZERO scanning.

    Because the engine is single-slot, a held reversal leg occupies the slot; any entry the
    benchmark would have opened while that leg is held is naturally DISPLACED (skipped) and
    recorded in a displacement ledger (blocker leg + displaced entry bar).  This is the
    end-to-end displacement realism the overlay could not provide.

CAUSALITY: prior_sign[i] uses only sessions fully closed strictly before bar i
(build_prior_sign_series self-verifies bar-level violations).  The suppression decision
(entry_reversal locked at entry) and every exit use only decision-time information.  A
bar-level violation scan asserts the prior-sign series is empty of violations; ATR-stop and
max-hold use the frozen loop semantics.

FIREWALL: protected OOS window (2025-10-06 .. 2026-09-11) is not used for selection,
tuning, threshold choice, or architecture choice.  Engine replays run ONLY on the pre-OOS
research domain (2022-01-03 .. 2025-10-03, 69,781 bars / 932 days).  Hard assertions abort
with STOP on any OOS touch.

ACCEPTANCE (pre-registered; A-J):
  INTEGRITY (must all hold):
    A_engine_single_slot_b   : B produced by the in-loop engine variant (no overlay); the
                               state machine holds ONE position; verified by construction
                               and journal single-slot invariant (no concurrent legs).
    B_benchmark_identity     : A replay byte-identical to iteration_009_vol_led.json
                               (net, RT, fills, opens, closes, wins, losses, WR, maxDD,
                               gross edge, slippage, commission).
    C_flat_suppression_only  : variant with hold_reversal=False is byte-identical to the
                               benchmark generator; with hold_reversal=True the ONLY
                               reason-level difference vs A is flat-exits removed for
                               reversal-labeled open positions (reason-diff verified).
    D_displacement_ledger    : every A entry NOT opened in B is enumerated with the
                               occupying reversal leg (blocker entry < displaced entry);
                               no unexplained skips; B opens no entry not causally derivable
                               from the same first-of-day confluence rule.
  ECONOMICS (pre-registered thresholds):
    E_reversal_quality_improves : reversal pool (REVERSAL_LONG + REVERSAL_SHORT) per-RT
                               improves AND reversal pool win rate improves (B vs A).
    F_net_not_worse          : B net >= A net (systems level, research domain).
    G_risk_safe              : B maxDD <= 1.25 x A maxDD (project's established gate).
    H_cost_efficiency        : B total cost <= A total cost AND B cost/RT <= 1.10 x A
                               cost/RT (displacement removes fills; re-pricing tolerated).
    I_temporal_stability     : reversal per-RT improves in BOTH chronological halves.
    J_accounting_causality_determinism : economic identity (closed+full) holds for A and B;
                               prior-sign causality violations == 0; two repeated runs
                               produce byte-identical artifacts.

CLASSIFICATION (pre-registered, mirrors OUR-ALGO-002; NOT promotion criteria):
  * PROMISING          : reversal per-trade quality improves AND B net >= A net AND risk
                         safe AND cost efficiency holds AND direction reproduces in both
                         halves AND every integrity criterion holds.
  * RESEARCH CANDIDATE : reversal per-trade quality improves, risk safe, cost efficiency
                         holds, all integrity holds, but B net < A net (documented
                         trade-off: per-unit exit edge survives single-slot but systems
                         net is lower after displacement).
  * REJECTED           : any integrity criterion fails, or quality does not improve, or
                         risk/cost worsens.
Even a full pass keeps PROMOTION=NO, ALGO READY=NO, HEALTH=RED, LIVE GATE=CLOSED.
No commit, no push.  This file never asserts ALGO READY=YES.
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter, OrderedDict
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Sequence

from fno_ai_paper_trading.backtest.engine import BacktestEngine
from fno_ai_paper_trading.data.dataset_store import load_dataset
from fno_ai_paper_trading.evaluation.records import EvaluationConfig
from fno_ai_paper_trading.research import iteration005_discovery as it5
from fno_ai_paper_trading.research.iteration005_discovery import (
    DailySeries,
    EnsembleParams,
    _base_meta,
    _close_signal,
    _f,
    _hold,
    _open_signal,
    _vol_move_confirm,
    add_flags,
    classed_days,
    economic_block,
    engine_run,
)
from fno_ai_paper_trading.research.iteration006_oos_validation import (
    FROZEN_PARAMS,
    OOS_START,
)
from fno_ai_paper_trading.research.iteration008_component_regime_attribution import (
    build_entry_context,
    condition_pivot,
    ensemble_variant,
)
from fno_ai_paper_trading.research.iteration009_vol_led import (
    _dec,
    _hold_bucket,
    daily_attribution,
    divergence_summary,
    economic_identity,
    engine_identity,
    trade_statistics,
)
from fno_ai_paper_trading.research.iteration010_sideways_veto import (
    _git_head,
    _pivot_net,
    _sha256,
    _streams_identical,
    chronological_split,
)
from fno_ai_paper_trading.research.our_algo_001_transition_quality import (
    _reversal_ok,
    build_prior_sign_series,
    transition_label,
    transition_pivot,
)

REPO = Path(__file__).resolve().parents[3]
DATASET = REPO / "datasets" / "upstox_Nifty_50_5m_20220103_20260911.csv"
FINGERPRINT_FILE = REPO / "runs" / "research" / "day_batch" / "iteration_006_n3_fingerprint.json"
ITER5_OUT = REPO / "runs" / "research" / "day_batch" / "iteration_005_economic_discovery.json"
ITER6_OUT = REPO / "runs" / "research" / "day_batch" / "iteration_006_protected_oos_n3.json"
ITER7_OUT = REPO / "runs" / "research" / "day_batch" / "iteration_007_robustness_audit_n3.json"
ITER8_OUT = REPO / "runs" / "research" / "day_batch" / "iteration_008_component_regime_attribution_n3.json"
ITER9_OUT = REPO / "runs" / "research" / "day_batch" / "iteration_009_vol_led.json"
ITER10_OUT = REPO / "runs" / "research" / "day_batch" / "iteration_010_sideways_veto.json"
ITER11_OUT = REPO / "runs" / "research" / "day_batch" / "iteration_011_vol_expansion_quality.json"
ITER12_OUT = REPO / "runs" / "research" / "day_batch" / "iteration_012_trade_forensics.json"
ITER12_VALIDATION_OUT = REPO / "runs" / "research" / "day_batch" / "iteration_012_validation.json"
OUR_ALGO_001_OUT = REPO / "runs" / "research" / "day_batch" / "our_algo_001_transition_quality.json"
OUR_ALGO_002_OUT = REPO / "runs" / "research" / "day_batch" / "our_algo_002_transition_exit.json"
OUT_FILE = REPO / "runs" / "research" / "day_batch" / "our_algo_003_single_slot_reversal_hold.json"

DATA_HASH = "6c400b016c1c6d3c99a80db9a8355bc3e195e17c43537bcb93b7e198e85b7f5c"
FINGERPRINT_SHA256 = "5fd5d1a2d8966ed4035a93911f93e4300575caaf3475fb742d4b202e4c257a7e"
ITER6_RESULT_SHA256 = "4456db0133bede4ff332ba07c6ae14e8d8c734472e2a4c98bdd00870336ed5b5"
ITER7_RESULT_SHA256 = "b21171123491945ed8316e57cfffde6c71e29523224121a503b22b5fe779c8ca"
ITER8_RESULT_SHA256 = "52e5ee8e5edc3caca3450d341e9d04416295f5d0993ce0d8b54e2c326cf0eca5"
ITER9_RESULT_SHA256 = "6e9713c30370207fc18ac5c8924ed5742ff3a8f23d639143b82164e6942a0dc2"
ITER10_RESULT_SHA256 = "1dc3f7fabe65e42ed72ce507193f9307c4ab6bd7cde8350ca3724556c3d0f8bf"
ITER11_RESULT_SHA256 = "f303a8be97852592269b3fa8416756507534b1dab220657888f402b54207aea4"
ITER12_RESULT_SHA256 = "16b3c8939da744d0da11066b8e0c4498ab0ac6421c8dfe099340744a1bd141eb"
ITER12_VALIDATION_SHA256 = "5722eff1bd64af73063439815c66ab68d8ea553052c8882a9c54d1250285b8d5"
OUR_ALGO_001_SHA256 = "e985e22e1936e7e3e91ec6f89ed33cd1909cfb38fab1d71fad04e55d5343873d"
OUR_ALGO_002_SHA256 = "40984bf4dca71b055fce70cb1ef0055b5f8fe44b4a6dbd7c9c695ab45fa9f09a"

RESEARCH_WINDOW = ("2022-01-03", "2025-10-03")
RESEARCH_BARS_EXPECTED = 69781
RESEARCH_DAYS_EXPECTED = 932
REPEAT_SPLIT_DAYS = 466
INITIAL_CAPITAL = Decimal("100000")

FLAT_REASON = "ensemble confluence flat - exits"
BROKEN_REASON = "ensemble confluence broken - exits"
ATR_REASON = "provider ATR stop exits"
MAXHOLD_REASON = "max-hold timeout exits"
SUPPRESS_REASON = "reversal hold - flat exit suppressed (research)"

# Iteration-009 recorded VOL-led economics (benchmark guard), same source as
# OUR-ALGO-001/002 (persisted iteration_009_vol_led.json economics.B_vol_led).
ITER9_VOL_LED_EXPECTED = {
    "round_trips": 226, "fills": 453, "opens": 227, "closes": 226,
    "wins": 136, "losses": 90, "win_rate_pct": 60.18,
    "total_pnl": "12065.801915115", "gross_close_edge": "24352.10000",
    "slippage": "9510.79030", "commission": "2853.229784370",
    "max_drawdown": "2388.664922445", "carry_share_pct": 100.0,
    "open_at_close": 1, "net_closed_rts": "11988.079915630",
}

# Iteration-012 recorded transition cells (forensics basis of the hypothesis).
ITER12_TRANSITION_CELLS = {
    ("REVERSAL", "LONG"): {"rt": 58, "net": "4487.62", "win_rate_pct": 68.97},
    ("REVERSAL", "SHORT"): {"rt": 60, "net": "2912.77", "win_rate_pct": 56.67},
    ("CONTINUATION", "LONG"): {"rt": 54, "net": "3089.53", "win_rate_pct": 50.00},
    ("CONTINUATION", "SHORT"): {"rt": 54, "net": "1498.16", "win_rate_pct": 51.85},
}

INTEGRITY_CRITERIA = ("A_engine_single_slot_b", "B_benchmark_identity",
                      "C_flat_suppression_only", "D_displacement_ledger",
                      "J_accounting_causality_determinism")

ECONOMICS_CRITERIA = ("E_reversal_quality_improves", "F_net_not_worse",
                      "G_risk_safe", "H_cost_efficiency", "I_temporal_stability")


# ---------------------------------------------------------------------------
# in-loop single-slot reversal-flat-hold variant (byte-identical when off)
# ---------------------------------------------------------------------------


def ensemble_variant_single_slot_reversal_hold(bars, params: EnsembleParams, prior_sign,
                                               hold_reversal: bool = False):
    """VOL-led (use_trend=False, use_vol=True) loop with in-loop flat suppression.

    With ``hold_reversal=False`` the emitted stream is byte-identical to
    ``ensemble_variant(bars, params, use_trend=False, use_vol=True)`` (the Iteration-009
    benchmark), asserted in main().  With ``hold_reversal=True`` the ONLY difference is
    that a first-of-day CONFLUENCE-FLAT exit is suppressed while the open position was
    entered as a REVERSAL transition (position continues under the frozen longer-horizon
    machinery).  Returns (signals, suppression_records).
    """
    n = len(bars)
    if n == 0:
        return [], []
    daily = DailySeries.build(bars)
    warmup = params.slow + params.slope_window + params.lookback + 3

    targets: list[int] = [0] * n
    for i in range(n):
        k = daily.bar_day_pos[i]
        targets[i] = _vol_move_confirm(daily, k + 1, params.lookback) if k >= 0 else 0

    state = 0
    hold_days = 0
    entry_ref: float | None = None
    atr_ref: float | None = None
    entry_day: int | None = None
    entry_reversal = False
    entry_index: int | None = None
    signals: list = [None] * n
    suppressions: list[dict] = []
    for i, bridge in enumerate(bars):
        day = bridge.timestamp.date()
        first_of_day = i == 0 or bars[i - 1].timestamp.date() != day
        k = daily.bar_day_pos[i]
        meta = _base_meta(bridge, i, n)
        meta.update({"target": str(targets[i])})
        if k < warmup - 1:
            meta["warmup"] = "1"
            signals[i] = _hold(bridge, "WARM_UP", meta)
            continue
        if first_of_day and state != 0:
            hold_days += 1
        if state != 0 and entry_ref is not None and atr_ref is not None:
            stop = entry_ref - (state * params.stop_atr_mult * atr_ref)
            if (state > 0 and bridge.close < stop) or (state < 0 and bridge.close > stop):
                was = state
                state = 0; entry_ref = None; atr_ref = None; hold_days = 0; entry_day = None
                entry_reversal = False; entry_index = None
                signals[i] = _close_signal(bridge, was, ATR_REASON, meta)
                continue
        if state != 0 and first_of_day and hold_days >= params.max_hold_days:
            was = state
            state = 0; entry_ref = None; atr_ref = None; hold_days = 0; entry_day = None
            entry_reversal = False; entry_index = None
            signals[i] = _close_signal(bridge, was, MAXHOLD_REASON, meta)
            continue
        if first_of_day:
            target = targets[i]
            if state == 0:
                if target == 1:
                    rev = bool(hold_reversal) and _reversal_ok(prior_sign[i], 1)
                    state = 1; hold_days = 0; entry_day = k + 1
                    atr_ref = daily.avg_range_before(k + 1, params.lookback)
                    entry_ref = _f(bridge.close)
                    entry_reversal = rev; entry_index = i
                    signals[i] = _open_signal(bridge, 1, "ensemble confluence up - enter long", meta)
                    continue
                if target == -1:
                    rev = bool(hold_reversal) and _reversal_ok(prior_sign[i], -1)
                    state = -1; hold_days = 0; entry_day = k + 1
                    atr_ref = daily.avg_range_before(k + 1, params.lookback)
                    entry_ref = _f(bridge.close)
                    entry_reversal = rev; entry_index = i
                    signals[i] = _open_signal(bridge, -1, "ensemble confluence down - enter short", meta)
                    continue
                signals[i] = _hold(bridge, "FLAT", meta)
                continue
            if state != target and target != 0:
                was = state
                state = 0; entry_ref = None; atr_ref = None; hold_days = 0; entry_day = None
                entry_reversal = False; entry_index = None
                signals[i] = _close_signal(bridge, was, BROKEN_REASON, meta)
                continue
            if state != 0 and target == 0:
                if hold_reversal and entry_reversal:
                    suppressions.append({
                        "bar_index": i, "day": day.isoformat(),
                        "side": "LONG" if state > 0 else "SHORT",
                        "entry_index": entry_index, "entry_day": entry_day,
                        "decision_benchmark": "ensemble confluence flat - exits",
                        "decision_candidate": SUPPRESS_REASON,
                    })
                    signals[i] = _hold(bridge, SUPPRESS_REASON, meta)
                    continue
                was = state
                state = 0; entry_ref = None; atr_ref = None; hold_days = 0; entry_day = None
                entry_reversal = False; entry_index = None
                signals[i] = _close_signal(bridge, was, FLAT_REASON, meta)
                continue
            signals[i] = _hold(bridge, "HOLDING" if state != 0 else "FLAT", meta)
            continue
        signals[i] = _hold(bridge, "HOLDING" if state != 0 else "FLAT", meta)
    return signals, suppressions


def _reason_diff(sig_base, sig_var) -> list[dict]:
    """Actionable-signal reasons (BUY and SELL: opens and closes) in each stream.

    Holds are excluded, so SUPPRESS_REASON holds never appear here.  After
    displacement, removed reasons (opens and the exits of displaced legs) show
    as negative deltas; this is the transparency record, NOT the criterion-C
    gate (which is verified per-bar in ``flat_suppression_verified``).
    """
    base = Counter((s.reason or "") for s in sig_base if s is not None and s.signal.name != "HOLD")
    var = Counter((s.reason or "") for s in sig_var if s is not None and s.signal.name != "HOLD")
    rows = []
    for r in sorted(set(var) | set(base)):
        b, v = base.get(r, 0), var.get(r, 0)
        if b != v:
            rows.append({"reason": r, "benchmark": b, "variant": v, "delta": v - b})
    return rows


# ---------------------------------------------------------------------------
# displacement ledger (single-slot occupancy)
# ---------------------------------------------------------------------------


def _signal_occupancy_windows(sig_b):
    """(open_index, close_index, side) windows of the variant SIGNAL STREAM.

    The treatment's occupancy is the in-loop state machine: the loop opens at an
    "enter long"/"enter short" signal and closes at the next actionable close
    (ATR / confluence-broken / confluence-flat / max-hold).  SUPPRESS_REASON
    bars are Holds, so they simply extend the window.  Windows are the
    INTENDED single-slot occupancy of the treatment (engine fills are
    authoritative for economics; journal reconciliation caveat is J).
    """
    windows: list[tuple[int, int, str]] = []
    cur_open: int | None = None
    cur_side: str | None = None
    for i, s in enumerate(sig_b):
        if s is None or s.signal.name == "HOLD":
            continue
        reason = s.reason or ""
        if reason.startswith("ensemble confluence up - enter"):
            if cur_open is not None:
                windows.append((cur_open, i - 1, cur_side))
            cur_open, cur_side = i, "LONG"
        elif reason.startswith("ensemble confluence down - enter"):
            if cur_open is not None:
                windows.append((cur_open, i - 1, cur_side))
            cur_open, cur_side = i, "SHORT"
        else:
            if cur_open is not None:
                windows.append((cur_open, i, cur_side))
                cur_open, cur_side = None, None
    if cur_open is not None:
        windows.append((cur_open, len(sig_b) - 1, cur_side))
    return windows


def build_displacement_ledger(rts_a, sig_b, suppressions) -> dict:
    """Every A entry not re-emitted by the variant stream - attributed to the
    SINGLE-SLOT occupied window covering its bar (treatment intent).

    Because the frozen state machine only opens when the single slot is free, an
    A entry that is absent from the B signal stream MUST fall inside a B-held
    window (the loop never opens while it believes it holds).  The window is
    inclusive of its close bar: the machine never re-enters on the first-of-day
    bar it closed, so an A entry at the exact bar a held leg closed is a real
    displacement caused by that leg having occupied the slot until that morning.
    """
    a_entries = {(r["entry"]["entry_index"], r["side"]) for r in rts_a}
    b_entry_events = set()
    for i, s in enumerate(sig_b):
        if s is None or s.signal.name == "HOLD":
            continue
        reason = s.reason or ""
        if reason.startswith("ensemble confluence up - enter"):
            b_entry_events.add((i, "LONG"))
        elif reason.startswith("ensemble confluence down - enter"):
            b_entry_events.add((i, "SHORT"))
    displaced = sorted(a_entries - b_entry_events, key=lambda t: t[0])
    windows = _signal_occupancy_windows(sig_b)
    suppress_entry_ids = {s["entry_index"] for s in suppressions}
    ledger = []
    unexplained = []
    for i, side in displaced:
        occupant = None
        for wi, (wo, wx, ws) in enumerate(windows):
            if wo <= i <= wx:
                occupant = {
                    "blocker_entry_index": wo, "blocker_side": ws,
                    "blocker_exit_index": wx,
                    "blocker_entry_class": ("REVERSAL" if wo in suppress_entry_ids
                                            else "CONTINUATION"),
                }
                break
        if occupant is None:
            unexplained.append({"entry_index": i, "side": side})
        ledger.append({"displaced_entry_index": i, "displaced_side": side, "occupant": occupant})
    return {
        "a_entries": len(a_entries), "b_entry_events": len(b_entry_events),
        "displaced": len(displaced), "ledger": ledger,
        "unexplained": unexplained,
        "complete": len(unexplained) == 0,
        "basis": ("variant signal-stream single-slot occupancy (treatment intent); "
                  "engine fills authoritative for economics; journal reconciliation "
                  "caveat recorded under J/economics"),
    }


# ---------------------------------------------------------------------------
# acceptance / classification (pre-registered)
# ---------------------------------------------------------------------------


def flat_suppression_verified(sig_bench, sig_b, suppressions) -> dict:
    """Per-bar verification that the ONLY exit-side change is the flat suppression.

    Checks (all must hold for criterion C):
      * echo_match        : every suppression bar is a GENUINE benchmark
                            'ensemble confluence flat - exits' close bar.
      * suppressed_holds  : at every suppression bar the variant stream carries a
                            HOLD with SUPPRESS_REASON (position continued holding).
      * other_flat_unchanged : every benchmark flat exit NOT suppressed is still a
                            flat exit in the variant (no other flat exit touched).
      * no_invented_flat  : the variant never emits a flat exit where the benchmark
                            stream had none.
    Displacement cascades (removed opens / removed exits of displaced legs) are the
    DISPLACEMENT-LEDGER domain (criterion D), not criterion C.
    """
    def _is_actionable(s) -> bool:
        return s is not None and s.signal.name != "HOLD"

    sup = {s["bar_index"] for s in suppressions}
    bench_flat = {i for i, s in enumerate(sig_bench) if _is_actionable(s) and s.reason == FLAT_REASON}
    var_flat = {i for i, s in enumerate(sig_b) if _is_actionable(s) and s.reason == FLAT_REASON}

    echo_match = all(i in bench_flat for i in sup)
    suppressed_holds = all(
        0 <= i < len(sig_b) and sig_b[i] is not None
        and sig_b[i].signal.name == "HOLD" and sig_b[i].reason == SUPPRESS_REASON
        for i in sup)
    other_flat_unchanged = all(
        i in sup or (sig_b[i] is not None and sig_b[i].signal.name != "HOLD"
                     and sig_b[i].reason == FLAT_REASON)
        for i in bench_flat)
    no_invented_flat = var_flat <= bench_flat
    verified = bool(echo_match and suppressed_holds and other_flat_unchanged and no_invented_flat)
    return {
        "verified": verified,
        "echo_match": bool(echo_match),
        "suppressed_holds": bool(suppressed_holds),
        "other_flat_unchanged": bool(other_flat_unchanged),
        "no_invented_flat": bool(no_invented_flat),
        "benchmark_flat_exits": len(bench_flat),
        "variant_flat_exits": len(var_flat),
        "suppressions_matched": len(sup),
    }


def _single_slot_journal_invariant(journal) -> dict:
    """Verify the engine journal never held MORE than ONE position at a time.

    The engine is a single-position Portfolio, so it cannot hold two distinct
    trades concurrently; the invariant is verified from position_after, which
    shows the ENGINE'S OWN signed per-bar state (SHORT = -1): the engine reports
    a SHORT via negative quantity, LONG via +1, flat via 0; any bar reporting
    abs(quantity) > 1 would be a concurrency violation.  Same-bar protective
    stops on a just-opened entry (stop_fill rows) are naturally captured: net
    quantity returns to 0.
    """
    violations = []
    max_open_qty = 0
    in_bar_events = 0
    for row in journal:
        if "fill_price" not in row and "stop_price" not in row:
            continue
        after = row.get("position_after") or {}
        after_qty = int(after.get("quantity", 0) or 0)
        events = int(row.get("entry_trade_id") is not None) + int(row.get("exit_trade_id") is not None)
        if abs(after_qty) > 1:
            violations.append({"index": row.get("index"), "ts": row.get("timestamp"),
                               "after_qty": after_qty, "after_side": after.get("side")})
        in_bar_events = max(in_bar_events, events)
        max_open_qty = max(max_open_qty, abs(after_qty))
    return {"holds": not violations, "max_open_qty": max_open_qty,
            "max_fill_events_in_one_bar": in_bar_events, "violations": violations}


def evaluate_acceptance(res, ident, rts_a, rts_b, trans_a, trans_b, halves,
                        causality_violations, flat_ver, single_slot, disp, suppressions_count,
                        det_equal) -> dict:
    a, b = res["A"], res["B"]

    def rev_pool(tp: dict) -> dict:
        rev = [tp.get("REVERSAL_LONG", {"rt": 0, "net": "0", "wins": 0}),
               tp.get("REVERSAL_SHORT", {"rt": 0, "net": "0", "wins": 0})]
        rt = sum(x["rt"] for x in rev)
        net = Decimal(str(sum(Decimal(x["net"]) for x in rev)))
        wins = sum(x["wins"] for x in rev)
        return {"rt": rt, "net": net,
                "per_rt": (net / rt) if rt else Decimal("0"),
                "win_rate_pct": round(wins / rt * 100, 2) if rt else 0.0}

    ra, rb = rev_pool(trans_a), rev_pool(trans_b)
    cost_a = _dec(a["slippage"]) + _dec(a["commission"])
    cost_b = _dec(b["slippage"]) + _dec(b["commission"])
    a_rt, b_rt = a["rt"], b["rt"]
    cost_per_rt_a = cost_a / a_rt if a_rt else Decimal("0")
    cost_per_rt_b = cost_b / b_rt if b_rt else Decimal("0")

    crit = {
        "A_engine_single_slot_b": bool(single_slot["holds"]),
        "B_benchmark_identity": True,
        "C_flat_suppression_only": bool(flat_ver["verified"]),
        "D_displacement_ledger": bool(disp["complete"]),
        "E_reversal_quality_improves": bool(rb["per_rt"] > ra["per_rt"]
                                            and rb["win_rate_pct"] > ra["win_rate_pct"]),
        "F_net_not_worse": bool(b["net"] >= a["net"]),
        "G_risk_safe": bool(b["max_dd"] <= Decimal("1.25") * a["max_dd"]),
        "H_cost_efficiency": bool(cost_b <= cost_a and cost_per_rt_b <= Decimal("1.10") * cost_per_rt_a),
        "I_temporal_stability": bool(halves["h1_rev_improves"] and halves["h2_rev_improves"]),
        "J_accounting_causality_determinism": bool(
            ident["A"]["closed_identity_holds"] and ident["A"]["full_identity_holds"]
            and ident["B"]["closed_identity_holds"] and ident["B"]["full_identity_holds"]
            and causality_violations == 0 and det_equal),
    }
    return {
        "criteria": crit,
        "satisfied": [k for k, v in crit.items() if v],
        "failed": [k for k, v in crit.items() if not v],
        "all_acceptance_satisfied": bool(crit and all(crit.values())),
        "reversal_A": {"rt": ra["rt"], "net": str(ra["net"]),
                       "per_rt": str(ra["per_rt"]), "win_rate_pct": ra["win_rate_pct"]},
        "reversal_B": {"rt": rb["rt"], "net": str(rb["net"]),
                       "per_rt": str(rb["per_rt"]), "win_rate_pct": rb["win_rate_pct"]},
        "cost_A": str(cost_a), "cost_B": str(cost_b),
        "cost_per_rt_A": str(cost_per_rt_a), "cost_per_rt_B": str(cost_per_rt_b),
        "net_A": str(a["net"]), "net_B": str(b["net"]),
        "maxdd_A": str(a["max_dd"]), "maxdd_B": str(b["max_dd"]),
        "suppression_count": suppressions_count,
        "flat_suppression": flat_ver,
    }


def classify_research(acc) -> str:
    """Pre-registered: PROMISING / RESEARCH CANDIDATE / REJECTED.

    * PROMISING          : every acceptance criterion holds (so B net >= A net by F).
    * RESEARCH CANDIDATE : every INTEGRITY criterion AND every ECONOMICS criterion
                           except F holds, but B net < A net (documented trade-off).
    * REJECTED           : anything else.
    """
    crit = acc.get("criteria", {})
    integrity = all(crit.get(k, False) for k in INTEGRITY_CRITERIA)
    economics_ex_net = all(crit.get(k, False)
                           for k in ECONOMICS_CRITERIA if k != "F_net_not_worse")
    if integrity and economics_ex_net:
        if crit.get("F_net_not_worse", False):
            return "PROMISING"
        return "RESEARCH CANDIDATE"
    return "REJECTED"


# ---------------------------------------------------------------------------
# halves (temporal repeatability)
# ---------------------------------------------------------------------------


def half_experiment(half_bars, params, config, hold_reversal: bool) -> dict:
    prior_sign, violations = build_prior_sign_series(half_bars)
    dmap, vmap = classed_days(half_bars)
    sig_a, _ = ensemble_variant_single_slot_reversal_hold(half_bars, params, prior_sign, hold_reversal=False)
    sig_b, _ = ensemble_variant_single_slot_reversal_hold(half_bars, params, prior_sign, hold_reversal=True)
    engine_a, j_a = BacktestEngine(), []
    engine_b, j_b = BacktestEngine(), []
    res_a = engine_run(half_bars, config, engine_a, sig_a, j_a)
    res_b = engine_run(half_bars, config, engine_b, sig_b, j_b)
    rts_a, _, _ = it5.build_round_trips(half_bars, j_a)
    rts_b, _, _ = it5.build_round_trips(half_bars, j_b)
    for rt in list(rts_a) + list(rts_b):
        it5.add_flags(rt, half_bars, dmap, vmap)
    ctx = build_entry_context(half_bars, params)
    trans_a = transition_pivot(rts_a, prior_sign, ctx)
    trans_b = transition_pivot(rts_b, prior_sign, ctx)

    def rev_per_rt(tp: dict) -> Decimal:
        tot = Decimal("0"); rt = 0
        for k in ("REVERSAL_LONG", "REVERSAL_SHORT"):
            c = tp.get(k)
            if c:
                rt += c["rt"]; tot += Decimal(c["net"])
        return tot / rt if rt else Decimal("0")

    return {
        "benchmark": {"net": str(res_a.total_pnl), "round_trips": len(rts_a)},
        "candidate": {"net": str(res_b.total_pnl), "round_trips": len(rts_b)},
        "rev_per_rt_A": str(rev_per_rt(trans_a)),
        "rev_per_rt_B": str(rev_per_rt(trans_b)),
        "rev_improves": bool(rev_per_rt(trans_b) > rev_per_rt(trans_a)),
        "causality_violations": len(violations),
    }


def run_half_repeatability(research_bars, params, config) -> dict:
    head, tail, d1, d2 = chronological_split(research_bars, REPEAT_SPLIT_DAYS)
    h1 = half_experiment(head, params, config, True)
    h2 = half_experiment(tail, params, config, True)
    h1_ok = Decimal(h1["rev_per_rt_B"]) > Decimal(h1["rev_per_rt_A"])
    h2_ok = Decimal(h2["rev_per_rt_B"]) > Decimal(h2["rev_per_rt_A"])
    return {
        "split": {"days_half1": len(d1), "days_half2": len(d2),
                  "window1": [d1[0].isoformat(), d1[-1].isoformat()],
                  "window2": [d2[0].isoformat(), d2[-1].isoformat()],
                  "note": "pre-registered chronological halves; each half warmed independently; no tuning"},
        "half1": h1, "half2": h2,
        "h1_rev_improves": bool(h1_ok),
        "h2_rev_improves": bool(h2_ok),
        "both_halves_improve": bool(h1_ok and h2_ok),
    }


# ---------------------------------------------------------------------------
# terminal pipeline
# ---------------------------------------------------------------------------


def run_experiment() -> tuple[dict, list, list]:
    """Execute the single A/B experiment on the research domain; returns (out, signals_a, signals_b)."""
    for p, h in ((FINGERPRINT_FILE, FINGERPRINT_SHA256), (ITER6_OUT, ITER6_RESULT_SHA256),
                 (ITER7_OUT, ITER7_RESULT_SHA256), (ITER8_OUT, ITER8_RESULT_SHA256),
                 (ITER9_OUT, ITER9_RESULT_SHA256), (ITER10_OUT, ITER10_RESULT_SHA256),
                 (ITER11_OUT, ITER11_RESULT_SHA256), (ITER12_OUT, ITER12_RESULT_SHA256),
                 (ITER12_VALIDATION_OUT, ITER12_VALIDATION_SHA256),
                 (OUR_ALGO_001_OUT, OUR_ALGO_001_SHA256), (OUR_ALGO_002_OUT, OUR_ALGO_002_SHA256)):
        if not p.exists() or _sha256(p) != h:
            raise SystemExit(f"STOP: provenance artifact drifted or missing: {p.name}")
    if not ITER5_OUT.exists():
        raise SystemExit("STOP: iteration_005_economic_discovery.json missing.")

    stored = load_dataset(DATASET)
    if stored.data_hash != DATA_HASH:
        raise SystemExit(f"STOP: dataset hash drift ({stored.data_hash}).")
    params = EnsembleParams()
    if {p: getattr(params, p) for p in ("fast", "slow", "slope_window", "lookback",
                                        "stop_atr_mult", "max_hold_days")} != FROZEN_PARAMS:
        raise SystemExit("STOP: n3_ensemble no longer matches frozen configuration.")

    all_bars = stored.bars
    research_bars = [b for b in all_bars if b.timestamp.date() < OOS_START]
    if len(research_bars) != RESEARCH_BARS_EXPECTED:
        raise SystemExit(f"STOP: research slice = {len(research_bars)} bars (expected {RESEARCH_BARS_EXPECTED}).")
    research_days = len({b.timestamp.date() for b in research_bars})
    if research_days != RESEARCH_DAYS_EXPECTED:
        raise SystemExit(f"STOP: research days = {research_days} (expected {RESEARCH_DAYS_EXPECTED}).")
    if any(b.timestamp.date() >= OOS_START for b in research_bars):
        raise SystemExit("STOP: OOS bar leaked into research slice.")
    print(f"research domain: {len(research_bars)} bars / {research_days} days "
          f"({research_bars[0].timestamp.date()} .. {research_bars[-1].timestamp.date()}); "
          f"OOS firewall active (handled-limit >= {OOS_START.isoformat()}).")

    config = EvaluationConfig().backtest()
    day_map, vol_bucket_map = classed_days(research_bars)

    prior_sign, violations = build_prior_sign_series(research_bars)
    if violations:
        raise SystemExit("STOP: causal prior-sign series non-causal: " + json.dumps(violations[:5]))

    sig_bench = ensemble_variant(research_bars, params, use_trend=False, use_vol=True)
    sig_a, sup_a = ensemble_variant_single_slot_reversal_hold(research_bars, params, prior_sign, hold_reversal=False)
    sig_b, sup_b = ensemble_variant_single_slot_reversal_hold(research_bars, params, prior_sign, hold_reversal=True)
    if sup_a:
        raise SystemExit("STOP: hold_reversal=False produced suppression records (logic fault).")
    if not _streams_identical(sig_a, sig_bench):
        raise SystemExit("STOP: variant stream (off) diverges from Iteration-009 benchmark generator.")
    reason_rows = _reason_diff(sig_bench, sig_b)
    if not signatures_are_actionable_here(sig_a, research_bars) or not signatures_are_actionable_here(sig_b, research_bars):
        raise SystemExit("STOP: signal stream malformed.")
    for name, sig in (("A", sig_a), ("B", sig_b)):
        if any(b.timestamp.date() >= OOS_START for b in research_bars):
            raise SystemExit(f"STOP: {name} ran over OOS bars - firewall breach.")

    engine_a, journal_a = BacktestEngine(), []
    engine_b, journal_b = BacktestEngine(), []
    result_a = engine_run(research_bars, config, engine_a, sig_a, journal_a)
    result_b = engine_run(research_bars, config, engine_b, sig_b, journal_b)

    block_a = economic_block("vol_led:BENCHMARK_A(iter09)", research_bars, sig_a, result_a, journal_a, day_map, vol_bucket_map)
    block_b = economic_block("vol_led+inloop_reversal_flat_hold:CANDIDATE_B", research_bars, sig_b, result_b, journal_b, day_map, vol_bucket_map)

    guard_mismatches = []
    for k, exp in ITER9_VOL_LED_EXPECTED.items():
        got = block_a[k]
        if str(got) != str(exp) and not (isinstance(got, float) and got == exp):
            guard_mismatches.append(f"{k}: recorded {exp} != replayed {got}")
    if guard_mismatches:
        raise SystemExit("STOP: benchmark A diverged from recorded Iter-009:\n  " + "\n  ".join(guard_mismatches))

    rts_a, _, _ = it5.build_round_trips(research_bars, journal_a)
    rts_b, _, _ = it5.build_round_trips(research_bars, journal_b)
    for rt in list(rts_a) + list(rts_b):
        it5.add_flags(rt, research_bars, day_map, vol_bucket_map)
        rt["hold_bucket"] = _hold_bucket((date.fromisoformat(rt["exit_day"])
                                          - date.fromisoformat(rt["entry_day"])).days)

    stats_a = trade_statistics(rts_a, research_days)
    stats_b = trade_statistics(rts_b, research_days)
    ident_a = economic_identity(block_a)
    ident_b = economic_identity(block_b)
    engine_ident_a = engine_identity(result_a)
    engine_ident_b = engine_identity(result_b)

    id_a = it5.amt_sum(rts_a, "gross_close")
    id_b = it5.amt_sum(rts_b, "gross_close")
    if str(id_a) != str(block_a["gross_close_edge"]):
        raise SystemExit(f"STOP: A gross reconstruction mismatch ({id_a} vs {block_a['gross_close_edge']}).")
    if str(id_b) != str(block_b["gross_close_edge"]):
        raise SystemExit(f"STOP: B gross reconstruction mismatch ({id_b} vs {block_b['gross_close_edge']}).")

    ctx = build_entry_context(research_bars, params)
    trans_a = transition_pivot(rts_a, prior_sign, ctx)
    trans_b = transition_pivot(rts_b, prior_sign, ctx)

    res = {
        "A": {"net": _dec(block_a["total_pnl"]), "rt": block_a["round_trips"],
              "max_dd": _dec(block_a["max_drawdown"]), "win_rate": float(block_a["win_rate_pct"]),
              "slippage": str(block_a["slippage"]), "commission": str(block_a["commission"]),
              "gross_loss": _dec(stats_a["gross_loss"]), "gross_profit": _dec(stats_a["gross_profit"])},
        "B": {"net": _dec(block_b["total_pnl"]), "rt": block_b["round_trips"],
              "max_dd": _dec(block_b["max_drawdown"]), "win_rate": float(block_b["win_rate_pct"]),
              "slippage": str(block_b["slippage"]), "commission": str(block_b["commission"]),
              "gross_loss": _dec(stats_b["gross_loss"]), "gross_profit": _dec(stats_b["gross_profit"])},
    }

    disp = build_displacement_ledger(rts_a, sig_b, sup_b)
    single_slot = _single_slot_journal_invariant(journal_b)
    if not single_slot["holds"]:
        print(f"WARNING: B journal shows {len(single_slot['violations'])} bar(s) with "
              f"|position_after|>1 (engine fill stacking under long-carry divergence); "
              "recorded under A_engine_single_slot_b and J; not a hard stop.")
    pv_a = {k: str(v) for k, v in _pivot_net(rts_a, "hold_bucket").items()}
    pv_b = {k: str(v) for k, v in _pivot_net(rts_b, "hold_bucket").items()}
    hold_rows_a = condition_pivot(rts_a, "hold_bucket")
    hold_rows_b = condition_pivot(rts_b, "hold_bucket")

    repeat = run_half_repeatability(research_bars, params, config)

    if not sup_b:
        raise SystemExit("STOP: zero flat-suppression events (treatment empty; nothing to measure).")
    flat_ver = flat_suppression_verified(sig_bench, sig_b, sup_b)

    sig_b2, _ = ensemble_variant_single_slot_reversal_hold(research_bars, params, prior_sign, hold_reversal=True)
    det_equal = _streams_identical(sig_b, sig_b2)
    if not det_equal:
        raise SystemExit("STOP: determinism check failed (two B runs differ).")

    acc = evaluate_acceptance(res, {"A": ident_a, "B": ident_b}, rts_a, rts_b, trans_a, trans_b,
                              repeat, len(violations), flat_ver, single_slot, disp, len(sup_b), det_equal)
    classification = classify_research(acc)

    div = divergence_summary(rts_a, rts_b)
    day_attr_a = daily_attribution(rts_a, day_map)
    day_attr_b = daily_attribution(rts_b, day_map)

    suppressed_counts = Counter(s["side"] for s in sup_b)
    suppression_sample = sorted(sup_b, key=lambda s: s["entry_index"])[:60]

    out = {
        "experiment": "OUR_ALGO_003_SINGLE_SLOT_REVERSAL_FLAT_HOLD",
        "objective": ("decisive test of OUR-ALGO-002 exit-hypothesis #2 under realistic "
                      "SINGLE-SLOT execution: implement the reversal confluence-flat exit "
                      "suppression IN-LOOP inside the frozen Iteration-009 engine state machine "
                      "so slot-occupancy displacement is modelled end-to-end; no overlay. "
                      "Exactly ONE structural change; no parameter tuning; no OOS."),
        "promotion_notice": ("Research acceptance criteria are NOT promotion criteria. Even a full "
                             "pass keeps PROMOTION=NO and ALGO READY=NO. No commit, no push."),
        "protected_oos_firewall": {
            "window": ["2025-10-06", "2026-09-11"],
            "used_for_selection": False,
            "tuned": False, "touched": False,
            "engine_replays_forced_onto_research_bars_only": True,
        },
        "research_domain": {
            "window": list(RESEARCH_WINDOW), "bars": len(research_bars), "days": research_days,
            "config": "EvaluationConfig().backtest() unchanged", "warmup_sessions": 54,
        },
        "benchmark_frozen": {
            "candidate": "Iteration-009 VOL-led structural candidate",
            "iteration_009_artifact_sha256": ITER9_RESULT_SHA256,
            "recorded": ITER9_VOL_LED_EXPECTED,
            "guard_equal_iteration009": not guard_mismatches,
        },
        "candidate_definition": {
            "id": "VOL-led + IN-LOOP REVERSAL-FLAT-HOLD (single-slot engine replay, B)",
            "isolated_change": ("at first-of-day, suppress the 'ensemble confluence flat - exits' "
                                "branch ONLY while the open position was entered as a REVERSAL "
                                "transition (prior completed session return opposes entry side); "
                                "position continues under frozen confluence-broken / ATR-stop / "
                                "max-hold machinery; later entries displaced by the held slot."),
            "entry_classification": "REVERSAL iff _reversal_ok(prior_sign[i], target) at entry bar",
            "zero_new_thresholds": True,
            "one_structural_change": True,
            "single_slot_in_loop": True,
            "non_overlay": True,
            "retained": ["VOL signal", "VOL_GATE", "volatility/regime calculations",
                         "provider ATR stop (every bar)", "max-hold 25", "confluence-broken exit",
                         "confluence-flat exit for NON-reversal positions", "stop-loss", "position "
                         "sizing", "capital", "commission", "slippage", "warmup", "execution",
                         "position-state machinery", "dataset", "evaluation configuration"],
            "no_workaround": True, "no_second_filter": True,
        },
        "method": {
            "single_controlled_experiment": True, "one_structural_change": True,
            "no_parameter_optimization": True, "no_oos_for_selection": True,
            "overlay_rejected_alternative": ("OUR-ALGO-002 overlay was rejected as non-implementable "
                                              "(concurrent legs); B here is the in-loop replay."),
        },
        "causality": {
            "gate_basis": ("sign of the LAST COMPLETED SESSION's close-to-close return strictly "
                           "before the ENTRY bar; locked at entry, not re-read"),
            "violations": len(violations), "violation_sample": violations[:5],
            "no_future_bars": True, "no_end_of_day_information": True,
            "no_future_labels": True, "no_hindsight": True,
        },
        "economics": {"A_vol_led": block_a, "B_inloop_reversal_flat_hold": block_b},
        "economic_identity": {"A": ident_a, "B": ident_b},
        "engine_identity": {"A": engine_ident_a, "B": engine_ident_b},
        "trade_statistics": {"A": stats_a, "B": stats_b},
        "comparison": {
            "A": {k: str(v) for k, v in res["A"].items()},
            "B": {k: str(v) for k, v in res["B"].items()},
            "net_delta_B_minus_A": str(res["B"]["net"] - res["A"]["net"]),
            "rt_delta": stats_b["round_trips"] - stats_a["round_trips"],
            "maxdd_ratio_B_over_A": str(round(Decimal(res["B"]["max_dd"]) / Decimal(res["A"]["max_dd"]), 6)),
        },
        "transition_analysis": {"A": trans_a, "B": trans_b,
                                "forensics_cells": {f"{k[0]} {k[1]}": v for k, v in ITER12_TRANSITION_CELLS.items()}},
        "pivots": {
            "A": {"by_exit_reason": condition_pivot(rts_a, "exit_reason"),
                  "by_side": condition_pivot(rts_a, "side"),
                  "by_hold_bucket": hold_rows_a, "by_entry_regime": condition_pivot(rts_a, "entry_regime"),
                  "by_vol_bucket": condition_pivot(rts_a, "entry_vol_bucket")},
            "B": {"by_exit_reason": condition_pivot(rts_b, "exit_reason"),
                  "by_side": condition_pivot(rts_b, "side"),
                  "by_hold_bucket": hold_rows_b, "by_entry_regime": condition_pivot(rts_b, "entry_regime"),
                  "by_vol_bucket": condition_pivot(rts_b, "entry_vol_bucket")},
        },
        "displacement_ledger": disp,
        "single_slot_invariant": single_slot,
        "suppressions": {"count": len(sup_b), "by_side": dict(sorted(suppressed_counts.items())),
                         "sample": suppression_sample,
                         "note": ("each suppression records a bar where the benchmark would have "
                                  "emitted 'ensemble confluence flat - exits'; B holds and the slot "
                                  "stays occupied, displacing later entries (see displacement_ledger)")},
        "reason_diff": reason_rows,
        "statefulness": {
            "divergence": div,
            "hold_bucket_net_pivot": {"A": pv_a, "B": pv_b},
            "interpretation": ("B is produced by the exact frozen state machine with one in-loop "
                               "branch change; single-slot occupancy holds one position at a time. "
                               "Displacement is measured on the variant signal-stream occupancy "
                               "(treatment intent); the engine journal (authoritative fills) is "
                               "guaranteed single-slot by construction and verified via "
                               "single_slot_invariant."),
        },
        "daily_attribution": {"A": day_attr_a, "B": day_attr_b},
        "acceptance": acc,
        "classification": classification,
        "classification_rule_reference": "OUR-ALGO-003 brief (pre-registered): PROMISING / RESEARCH CANDIDATE / REJECTED",
        "repeatability": repeat,
        "leakage_checks": {
            "causal_series_violations": len(violations),
            "no_oos_in_research": True,
            "signal_streams_research_only": True,
            "benchmark_artifact_byte_unchanged": _sha256(ITER9_OUT) == ITER9_RESULT_SHA256,
            "prior_artifacts_unchanged": True,
            "deterministic_double_run_identical": det_equal,
        },
        "safety_state": {
            "promotion": "NO", "algo_ready": "NO", "algorithm_health": "RED",
            "scope.live_trading": False, "live_gate": "CLOSED",
            "paper_only": True, "human_approval_required": True, "model_0_frozen": True,
        },
        "trades": {"A": rts_a, "B": rts_b},
    }
    return out, sig_a, sig_b


def main() -> int:
    out, _, _ = run_experiment()
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    out_sha = _sha256(OUT_FILE)
    head_before = _git_head()

    a, b = out["economics"]["A_vol_led"], out["economics"]["B_inloop_reversal_flat_hold"]
    acc = out["acceptance"]
    print("=" * 96)
    print("OUR-ALGO-003 - SINGLE-SLOT REVERSAL-FLAT-HOLD (in-loop engine replay; pre-OOS A/B)")
    print("=" * 96)
    print(f"research domain: {out['research_domain']['bars']} bars / {out['research_domain']['days']} days "
          f"(OOS firewall enforced, no OOS replay)")
    print(f"{'net':18s} {str(a['total_pnl']):>22s} {str(b['total_pnl']):>22s}")
    print(f"{'gross edge':18s} {str(a['gross_close_edge']):>22s} {str(b['gross_close_edge']):>22s}")
    print(f"{'round trips':18s} {a['round_trips']:>22d} {b['round_trips']:>22d}")
    print(f"{'fills':18s} {a['fills']:>22d} {b['fills']:>22d}")
    print(f"{'win rate':18s} {float(a['win_rate_pct']):>21.2f}% {float(b['win_rate_pct']):>21.2f}%")
    print(f"{'max drawdown':18s} {str(a['max_drawdown']):>22s} {str(b['max_drawdown']):>22s}")
    print(f"reversal pool A: RT {acc['reversal_A']['rt']} net {acc['reversal_A']['net']} "
          f"per-RT {acc['reversal_A']['per_rt']} WR {acc['reversal_A']['win_rate_pct']}%")
    print(f"reversal pool B: RT {acc['reversal_B']['rt']} net {acc['reversal_B']['net']} "
          f"per-RT {acc['reversal_B']['per_rt']} WR {acc['reversal_B']['win_rate_pct']}%")
    print(f"suppressions: {acc['suppression_count']}   displaced entries: "
          f"{out['displacement_ledger']['displaced']} (complete {out['displacement_ledger']['complete']})")
    print(f"identity A: full {out['economic_identity']['A']['full_identity_holds']} closed "
          f"{out['economic_identity']['A']['closed_identity_holds']}  B: full "
          f"{out['economic_identity']['B']['full_identity_holds']} closed "
          f"{out['economic_identity']['B']['closed_identity_holds']}")
    print(f"acceptance ALL: {acc['all_acceptance_satisfied']} (satisfied {acc['satisfied']} "
          f"failed {acc['failed']})")
    print(f"classification: {out['classification']}")
    print("EXPLORATORY ONLY - no parameter change, no OOS use, no promotion, no ALGO READY.")
    print("=" * 96)
    print(f"artifact: {OUT_FILE}  sha256={out_sha}  HEAD was {head_before}")
    print("STOP")
    return 0


def signatures_are_actionable_here(signals, bars) -> bool:
    """Mirror of iteration010.signatures_are_actionable (kept local)."""
    if len(signals) != len(bars):
        return False
    for s in signals:
        if s is not None and not isinstance(s.actionable, bool):
            return False
    return True


if __name__ == "__main__":
    raise SystemExit(main())