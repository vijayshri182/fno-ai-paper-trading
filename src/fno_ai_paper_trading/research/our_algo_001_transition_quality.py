"""OUR-ALGO-001 - TRANSITION-QUALITY RESTRICTION OF THE VOL-LED SIGNAL (pre-OOS A/B, research only).

Single controlled research experiment on the project's own discovery algorithm.
Question (tests the NOT-TESTED future hypothesis #1 of Iteration-012 forensic
analysis): is the Iteration-009 VOL-led architecture fundamentally a REGIME-
CHANGE detector rather than a mere volatility-expansion detector?  If yes, the
per-trade edge should CONCENTRATE in entries where the confirming session's
direction REVERSES the last completed session's signed return (a down->up
transition for LONG entries, an up->down transition for SHORT entries) - the
two sign-transition cells found most profitable in the Iteration-012 trade
forensics (down->up 58 RT, net +4487.62, WR 68.97%; up->down 60 RT, net
+2912.77, WR 56.67%; reversal-transition cells together 118 RT, net +7400.39 =
61.7% of A's net).

EXPERIMENT (exactly ONE structural change, no parameter tuning):
  A = frozen Iteration-009 VOL-led benchmark (vol-confirmation-only entries,
      reproduced byte-for-byte, guarded against the persisted
      iteration_009_vol_led.json artifact).
  B = A restricted to REVERSAL TRANSITIONS.  When the existing Iteration-009
      VOL-led entry condition fires at a first-of-entry-day bar ``i`` whose last
      COMPLETED session index is ``k = daily.bar_day_pos[i]``, B additionally
      requires that the last completed session moved OPPOSITE to the candidate
      entry side (a regime-change, not a continuation):

        LONG :  prior_session_return = returns[k]  <  0   (down->up)
        SHORT:  prior_session_return = returns[k]  >  0   (up->down)

      This is a SIGN-ONLY condition: ZERO new thresholds, ZERO extra indicators,
      ZERO new parameters.  It is structurally OPPOSITE to the rejected
      Iteration-011 continuation gate and is NOT a regime veto (Iteration-010).
      Entries with a flat prior session (returns[k] == 0) carry no transition
      evidence and are suppressed.  Everything else (VOL signal, VOL_GATE,
      volatility/regime calculations, exits, stop-loss, sizing, capital, costs,
      slippage, warmup, execution, multiplier, position-state machinery,
      dataset, config) is unchanged.

CAUSALITY: ``returns[k]`` uses only sessions fully closed strictly before bar
``i`` (``bar_day_pos`` references only those sessions; the last session index
``last_idx[k] < i`` always).  A bar-level violation scan asserts every gate
input is strictly-before -> ``violations`` must be empty.  The gate NEVER
consumes the running session's return.  The inherited frozen-benchmark
decision-time handling is left untouched and documented: the underlying A-arm
confirmation ``_vol_move_confirm(daily, k+1, lookback)`` tests the completed
range ``ranges[k]`` against the prior 20 completed ranges (causal) but its
direction sign(returns[k+1]) uses the entry session's own close (same-entry-day
information at a first-of-day bar).  Arm B orients LATERAL direction with the
strictly causal ``returns[k]``; it does not alter A's signal machinery.

FIREWALL: protected OOS window (2025-10-06 .. 2026-09-11) is not used for
selection, tuning, threshold choice, or architecture choice.  Engine replays
run ONLY on the pre-OOS research domain (2022-01-03 .. 2025-10-03, 69,781 bars
/ 932 days).  Hard assertions abort with STOP on any OOS touch.

Classification (pre-registered):
  * PROMISING RESEARCH RESULT  : B improves ATR systems-level (B net > A net)
                                 AND all ten acceptance conditions hold.
  * INCONCLUSIVE               : B improves per-trade QUALITY (per-RT net and
                                 win rate) and all ten conditions hold, but B
                                 nets LESS than A (documented explicit trade-
                                 off: the regime-change cells carry a stronger
                                 per-unit edge but a smaller systems-level P&L
                                 pool).  Also used when B net > A net but not
                                 every condition holds.
  * REJECTED                   : otherwise (quality did not improve AND net
                                 did not improve, or a condition failed while
                                 net did not improve).
Even a full pass keeps PROMOTION=NO, ALGO READY=NO.  No commit, no push.
"""
from __future__ import annotations

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
    _entry_suppressions_only,
    _git_head,
    _pivot_net,
    _sha256,
    _streams_identical,
    affected_day_rows,
    chronological_split,
    path_analysis,
    signatures_are_actionable,
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
OUT_FILE = REPO / "runs" / "research" / "day_batch" / "our_algo_001_transition_quality.json"
MD_FILE = REPO / "runs" / "research" / "day_batch" / "OUR_ALGO_001_RESEARCH.md"

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

RESEARCH_WINDOW = ("2022-01-03", "2025-10-03")
RESEARCH_BARS_EXPECTED = 69781
RESEARCH_DAYS_EXPECTED = 932
REPEAT_SPLIT_DAYS = 466  # pre-registered chronological half count (466 + 466)

# Iteration-009 recorded VOL-led economics (benchmark guard).  Values come from
# the persisted iteration_009_vol_led.json ``economics.B_vol_led`` block.
ITER9_VOL_LED_EXPECTED = {
    "round_trips": 226, "fills": 453, "opens": 227, "closes": 226,
    "wins": 136, "losses": 90, "win_rate_pct": 60.18,
    "total_pnl": "12065.801915115", "gross_close_edge": "24352.10000",
    "slippage": "9510.79030", "commission": "2853.229784370",
    "max_drawdown": "2388.664922445", "carry_share_pct": 100.0,
    "open_at_close": 1, "net_closed_rts": "11988.079915630",
}

TRIVIAL_GUARD_NOTE = ("a strategy that simply stops trading is not successful; if most of the P&L "
                      "improvement comes from eliminating most trading activity the evidence is weak")

# Iteration-012 recorded transition cells (forensics basis of the hypothesis).
ITER12_TRANSITION_CELLS = {
    ("REVERSAL", "LONG"): {"rt": 58, "net": "4487.62", "win_rate_pct": 68.97},
    ("REVERSAL", "SHORT"): {"rt": 60, "net": "2912.77", "win_rate_pct": 56.67},
    ("CONTINUATION", "LONG"): {"rt": 54, "net": "3089.53", "win_rate_pct": 50.00},
    ("CONTINUATION", "SHORT"): {"rt": 54, "net": "1498.16", "win_rate_pct": 51.85},
}


def _sign_of(x) -> int | None:
    """Sign of a return: 1 up / -1 down / 0 flat (None when input missing)."""
    if x is None:
        return None
    return 1 if x > 0 else (-1 if x < 0 else 0)


def _reversal_ok(prior_sign: int | None, target: int) -> bool:
    """Transition-quality gate: LONG only from a DOWN prior session; SHORT
    only from an UP prior session.  Flat/undefined prior = no reversal evidence.
    """
    if prior_sign is None or prior_sign == 0:
        return False
    return (target == 1 and prior_sign == -1) or (target == -1 and prior_sign == 1)


# ---------------------------------------------------------------------------
# causal prior-session return-sign series (strictly decision-time)
# ---------------------------------------------------------------------------


def build_prior_sign_series(bars) -> tuple[list[int | None], list[dict]]:
    """Per-bar sign of the LAST COMPLETED SESSION's close-to-close return.

    Returns (prior_sign_per_bar, violations).  For bar ``i`` with
    ``k = daily.bar_day_pos[i]`` (the last session strictly before bar ``i``),
    ``prior_sign[i] = _sign_of(returns[k])``.  A session is only referenced if
    its last bar index is strictly before ``i``.
    """
    n = len(bars)
    daily = DailySeries.build(bars)
    prior_sign: list[int | None] = [None] * n
    for i in range(n):
        k = daily.bar_day_pos[i]
        if k >= 0:
            prior_sign[i] = _sign_of(daily.returns[k])
    violations: list[dict] = []
    for i in range(n):
        k = daily.bar_day_pos[i]
        if k >= 0:
            hi = daily.last_idx[k]
            if not (hi < i):
                violations.append({"bar_index": i, "session": k,
                                   "session_last_index": hi, "uses_future": hi >= i})
    return prior_sign, violations


# ---------------------------------------------------------------------------
# frozen-loop transition-gated variant (byte-identical when gate off)
# ---------------------------------------------------------------------------


def ensemble_variant_transition_gate(bars, params: EnsembleParams, prior_sign,
                                     gate: bool = True):
    """VOL-led candidate (use_trend=False, use_vol=True) with the transition gate.

    With ``gate=False`` the emitted signal stream is byte-identical to
    ``ensemble_variant(bars, params, use_trend=False, use_vol=True)`` (the
    Iteration-009 benchmark), asserted in main().  With ``gate=True`` the ONLY
    difference is that a first-of-day VOL-led ENTRY is suppressed unless the
    last completed session's signed return OPPOSES the entry side (reversal
    transition).  Exits, stops, holds, sizing and meta are untouched.
    Returns (signals, vetoed) where ``vetoed`` records suppressed entries.
    """
    n = len(bars)
    daily = DailySeries.build(bars)
    ema_fast = daily.day_ema(params.fast)
    ema_slow = daily.day_ema(params.slow)
    warmup = params.slow + params.slope_window + params.lookback + 3

    targets: list[int] = [0] * n
    for i in range(n):
        k = daily.bar_day_pos[i]
        if k < 0:
            targets[i] = 0
            continue
        targets[i] = _vol_move_confirm(daily, k + 1, params.lookback)

    state = 0
    hold_days = 0
    entry_ref: float | None = None
    atr_ref: float | None = None
    entry_day: int | None = None
    signals: list = [None] * n
    vetoed: list[dict] = []
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
                signals[i] = _close_signal(bridge, was, "provider ATR stop exits", meta)
                continue
        if state != 0 and first_of_day and hold_days >= params.max_hold_days:
            was = state
            state = 0; entry_ref = None; atr_ref = None; hold_days = 0; entry_day = None
            signals[i] = _close_signal(bridge, was, "max-hold timeout exits", meta)
            continue
        if first_of_day:
            target = targets[i]
            if state == 0:
                suppress = bool(gate) and target != 0 and not _reversal_ok(prior_sign[i], target)
                if target == 1 and not suppress:
                    state = 1; hold_days = 0; entry_day = k + 1
                    atr_ref = daily.avg_range_before(k + 1, params.lookback)
                    entry_ref = _f(bridge.close)
                    signals[i] = _open_signal(bridge, 1, "ensemble confluence up - enter long", meta)
                    continue
                if target == -1 and not suppress:
                    state = -1; hold_days = 0; entry_day = k + 1
                    atr_ref = daily.avg_range_before(k + 1, params.lookback)
                    entry_ref = _f(bridge.close)
                    signals[i] = _open_signal(bridge, -1, "ensemble confluence down - enter short", meta)
                    continue
                if suppress:
                    vetoed.append({
                        "bar_index": i, "day": day.isoformat(), "target": target,
                        "side": "LONG" if target == 1 else "SHORT",
                        "prior_session_return_sign": prior_sign[i],
                        "prior_session_return": str(daily.returns[k]) if k >= 0 else None,
                        "transition": ("REVERSAL" if _reversal_ok(prior_sign[i], target)
                                       else "CONTINUATION" if prior_sign[i] != 0
                                       else "UNDEFINED") if target != 0 else "NONE",
                        "decision_benchmark": "enter " + ("long" if target == 1 else "short"),
                        "decision_candidate": "flat - reversal transition required",
                    })
                    signals[i] = _hold(bridge, "FLAT - reversal transition required (research)", meta)
                    continue
                signals[i] = _hold(bridge, "FLAT", meta)
                continue
            if state != target and target != 0:
                was = state
                state = 0; entry_ref = None; atr_ref = None; hold_days = 0; entry_day = None
                signals[i] = _close_signal(bridge, was, "ensemble confluence broken - exits", meta)
                continue
            if state != 0 and target == 0:
                was = state
                state = 0; entry_ref = None; atr_ref = None; hold_days = 0; entry_day = None
                signals[i] = _close_signal(bridge, was, "ensemble confluence flat - exits", meta)
                continue
            signals[i] = _hold(bridge, "HOLDING" if state != 0 else "FLAT", meta)
            continue
        signals[i] = _hold(bridge, "HOLDING" if state != 0 else "FLAT", meta)
    return signals, vetoed


# ---------------------------------------------------------------------------
# transition attribution helpers
# ---------------------------------------------------------------------------


def transition_label(prior_sign: int | None, raw_volmove) -> str:
    """Per-trade transition class (Iteration-012 J-matrix semantics)."""
    cur_sign = _sign_of(raw_volmove) if isinstance(raw_volmove, (int, float, Decimal)) else None
    if cur_sign not in (1, -1):
        return "UNDEFINED"
    kind = "REVERSAL" if (prior_sign is not None and prior_sign != 0
                          and prior_sign != cur_sign) else "CONTINUATION"
    return f"{kind}_{'LONG' if cur_sign == 1 else 'SHORT'}"


def transition_pivot(rts: Sequence[dict], prior_sign, ctx) -> dict:
    cells: dict[str, list] = OrderedDict()
    for r in rts:
        i = r["entry"]["entry_index"]
        ps = prior_sign[i] if i < len(prior_sign) else None
        raw = (ctx[i]["volmove"] if ctx and ctx[i] else None) if i < len(ctx) else None
        cells.setdefault(transition_label(ps, raw), []).append(r)
    return {
        k: {"rt": len(g), "net": str(it5.amt_sum(g, "net")),
            "wins": sum(1 for r in g if r["net"] > 0),
            "win_rate_pct": round(sum(1 for r in g if r["net"] > 0) / len(g) * 100, 2) if g else 0.0}
        for k, g in cells.items()
    }


def pre_entry_return_quality(rts: Sequence[dict], prior_sign, daily) -> dict:
    """Winner/loser magnitude structure (Iteration-012 M-section context)."""
    def coll(r):
        i = r["entry"]["entry_index"]
        k = daily.bar_day_pos[i]
        return {
            "pre_entry_return": daily.returns[k] if k >= 0 else None,
            "entry_day_range": daily.ranges[k + 1] if 0 <= k + 1 < len(daily.ranges) else None,
        }

    wins, losses = [], []
    for r in rts:
        d = coll(r)
        (wins if r["net"] > 0 else losses).append(d["pre_entry_return"])
    return {
        "winner_pre_entry_return_mean": round(float(sum(wins)) / len(wins), 8) if wins else None,
        "loser_pre_entry_return_mean": round(float(sum(losses)) / len(losses), 8) if losses else None,
        "winner_count": len(wins), "loser_count": len(losses),
        "note": ("post-hoc attribution only, NOT part of the signal: winners tend to enter on "
                 "smaller prior-session moves with larger entry-day ranges (Iteration-012 M)"),
    }


def _per_rt(res_slot: dict, key: str = "net") -> Decimal:
    rt = _dec(res_slot["rt"])
    return _dec(res_slot[key]) / rt if rt else Decimal("0")


def b_only_nets(trades_a: Sequence[dict], trades_b: Sequence[dict]) -> list:
    a_keys = {(r["entry"]["entry_index"], r["side"]) for r in trades_a}
    return [r["net"] for r in trades_b
            if (r["entry"]["entry_index"], r["side"]) not in a_keys]


# ---------------------------------------------------------------------------
# acceptance (pre-registered) and classification
# ---------------------------------------------------------------------------


def evaluate_acceptance(res, ident, trades_a, trades_b, halves, causality_violations,
                        gross_ok_a, gross_ok_b) -> dict:
    a, b = res["A"], res["B"]
    per_rt_a = _per_rt(a)
    per_rt_b = _per_rt(b)
    removed = a["rt"] - b["rt"]
    removed_pct = removed / a["rt"] * 100.0 if a["rt"] else 0.0
    b_only_net = sum((Decimal(x) for x in b_only_nets(trades_a, trades_b)), Decimal("0"))
    crit = {
        "A_trade_quality_improves": bool(per_rt_b > per_rt_a),
        "B_win_rate_improves": bool(b["win_rate"] > a["win_rate"]),
        "C_drag_reduced": bool(b["coverage"] > a["coverage"] and b["coverage"] > 100.0),
        "D_drawdown_safe": bool(b["max_dd"] <= Decimal("1.25") * a["max_dd"]),
        "E_retained_meaningful": bool(b["rt"] >= Decimal("0.40") * a["rt"]),
        "F_accounting": bool(ident["A"]["closed_identity_holds"] and ident["A"]["full_identity_holds"]
                             and ident["B"]["closed_identity_holds"] and ident["B"]["full_identity_holds"]),
        "G_reconstruction": bool(gross_ok_a and gross_ok_b),
        "H_causality": bool(causality_violations == 0),
        "I_repeatability": bool(halves["h1_improves"] and halves["h2_improves"]),
        "J_no_opportunity_collapse": bool(not (removed_pct > 60.0 and b_only_net <= 0)),
    }
    quality_improves = bool(crit["A_trade_quality_improves"] and crit["B_win_rate_improves"])
    return {
        "criteria": crit,
        "satisfied": [k for k, v in crit.items() if v],
        "failed": [k for k, v in crit.items() if not v],
        "quality_improves": quality_improves,
        "net_improves": bool(b["net"] > a["net"]),
        "all_acceptance_satisfied": bool(crit and all(crit.values())),
        "per_rt_A": str(per_rt_a), "per_rt_B": str(per_rt_b),
        "trivial_guard": {
            "trades_removed": int(removed), "trades_removed_pct": round(removed_pct, 2),
            "candidate_net_improvement": str(b["net"] - a["net"]),
            "net_of_path_additions_b_only": str(b_only_net),
            "opportunity_collapse": bool(removed_pct > 60.0 and b_only_net <= 0),
            "weak_evidence": bool(removed_pct > 50.0 and b_only_net <= 0),
            "weak_note": TRIVIAL_GUARD_NOTE,
        },
    }


def classify_research(res, acceptance) -> str:
    if acceptance["net_improves"]:
        if acceptance["all_acceptance_satisfied"]:
            return "PROMISING RESEARCH RESULT"
        return "INCONCLUSIVE"
    if acceptance["quality_improves"] and acceptance["all_acceptance_satisfied"]:
        return "INCONCLUSIVE"  # explicit quality/system-level trade-off (documented)
    return "REJECTED"


# ---------------------------------------------------------------------------
# repeatability (section 21) - chronological halves of the research domain
# ---------------------------------------------------------------------------


def half_experiment(half_bars, params, config) -> dict:
    """A/B on one chronological half (independently warmed, no tuning)."""
    prior_sign, violations = build_prior_sign_series(half_bars)
    dmap, vmap = classed_days(half_bars)
    sig_a, sup_a = ensemble_variant_transition_gate(half_bars, params, prior_sign, gate=False)
    sig_b, sup_b = ensemble_variant_transition_gate(half_bars, params, prior_sign, gate=True)
    if sup_a:
        raise RuntimeError("STOP: gate=False produced suppression records (logic fault).")
    engine_a, j_a = BacktestEngine(), []
    engine_b, j_b = BacktestEngine(), []
    res_a = engine_run(half_bars, config, engine_a, sig_a, j_a)
    res_b = engine_run(half_bars, config, engine_b, sig_b, j_b)
    rts_a, _, _ = it5.build_round_trips(half_bars, j_a)
    rts_b, _, _ = it5.build_round_trips(half_bars, j_b)
    for rt in list(rts_a) + list(rts_b):
        it5.add_flags(rt, half_bars, dmap, vmap)
    def wr(rts):
        return (round(sum(1 for r in rts if r["net"] > 0) / len(rts) * 100, 2) if rts else 0.0)
    per_rt_a = (res_a.total_pnl / len(rts_a)) if rts_a else Decimal("0")
    per_rt_b = (res_b.total_pnl / len(rts_b)) if rts_b else Decimal("0")
    return {
        "benchmark": {"net": str(res_a.total_pnl), "round_trips": len(rts_a),
                      "wins": sum(1 for r in rts_a if r["net"] > 0),
                      "losses": sum(1 for r in rts_a if r["net"] <= 0),
                      "win_rate_pct": wr(rts_a), "per_rt_net": str(per_rt_a)},
        "candidate": {"net": str(res_b.total_pnl), "round_trips": len(rts_b),
                      "wins": sum(1 for r in rts_b if r["net"] > 0),
                      "losses": sum(1 for r in rts_b if r["net"] <= 0),
                      "win_rate_pct": wr(rts_b), "per_rt_net": str(per_rt_b)},
        "suppressed": len(sup_b),
        "causality_violations": len(violations),
        "quality_improves": bool(per_rt_b > per_rt_a and wr(rts_b) > wr(rts_a)),
        "net_improves": bool(res_b.total_pnl > res_a.total_pnl),
        "improves": bool(per_rt_b > per_rt_a and wr(rts_b) > wr(rts_a)),
    }


def run_half_repeatability(research_bars, params, config) -> dict:
    head, tail, d1, d2 = chronological_split(research_bars, REPEAT_SPLIT_DAYS)
    h1 = half_experiment(head, params, config)
    h2 = half_experiment(tail, params, config)
    return {
        "split": {"days_half1": len(d1), "days_half2": len(d2),
                  "window1": [d1[0].isoformat(), d1[-1].isoformat()],
                  "window2": [d2[0].isoformat(), d2[-1].isoformat()],
                  "note": "pre-registered chronological halves; each half warmed independently; no tuning"},
        "half1": h1, "half2": h2,
        "h1_improves": h1["improves"], "h2_improves": h2["improves"],
        "both_halves_improve": bool(h1["improves"] and h2["improves"]),
        "evidence": ("repeatability assessed by whether the per-trade quality edge of reversal-"
                     "transition entries reproduces in both chronological halves (recorded numbers "
                     "only; no promotion inference)"),
    }


# ---------------------------------------------------------------------------
# entry / terminal (canonical pipeline)
# ---------------------------------------------------------------------------


def main() -> int:
    for p, h in ((FINGERPRINT_FILE, FINGERPRINT_SHA256), (ITER6_OUT, ITER6_RESULT_SHA256),
                 (ITER7_OUT, ITER7_RESULT_SHA256), (ITER8_OUT, ITER8_RESULT_SHA256),
                 (ITER9_OUT, ITER9_RESULT_SHA256), (ITER10_OUT, ITER10_RESULT_SHA256),
                 (ITER11_OUT, ITER11_RESULT_SHA256), (ITER12_OUT, ITER12_RESULT_SHA256),
                 (ITER12_VALIDATION_OUT, ITER12_VALIDATION_SHA256)):
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
          f"OOS firewall active (handed-limit >= {OOS_START.isoformat()}).")

    config = EvaluationConfig().backtest()
    day_map, vol_bucket_map = classed_days(research_bars)

    daily = DailySeries.build(research_bars)
    prior_sign, violations = build_prior_sign_series(research_bars)
    if violations:
        raise SystemExit("STOP: causal prior-sign series non-causal: " + json.dumps(violations[:5]))

    sig_a, vetoed_a = ensemble_variant_transition_gate(research_bars, params, prior_sign, gate=False)
    sig_b, vetoed_b = ensemble_variant_transition_gate(research_bars, params, prior_sign, gate=True)
    if vetoed_a:
        raise SystemExit("STOP: gate=False produced suppression records (logic fault).")

    sig_bench = ensemble_variant(research_bars, params, use_trend=False, use_vol=True)
    if not _streams_identical(sig_a, sig_bench):
        raise SystemExit("STOP: gate=False signal stream diverges from Iteration-009 benchmark generator.")
    entry_change_ok, entry_change_check = _entry_suppressions_only(
        sig_b, sig_bench,
        [(v["bar_index"], v["side"]) for v in vetoed_b])
    if not entry_change_ok:
        raise SystemExit("STOP: gate=True changed more than entry decisions (structural leak): "
                         + json.dumps(entry_change_check))
    for v in vetoed_b:
        v["suppression_class"] = entry_change_check["per_slot_class"].get(v["bar_index"], "secondary")
    veto_direct = entry_change_check["direct_benchmark_entries_suppressed"]
    veto_secondary = entry_change_check["secondary_path_suppressions"]
    for name, sig in (("A", sig_a), ("B", sig_b)):
        if not signatures_are_actionable(sig, research_bars):
            raise SystemExit(f"STOP: {name} signal stream malformed.")
        if any(b.timestamp.date() >= OOS_START for b in research_bars):
            raise SystemExit(f"STOP: {name} ran over OOS bars - firewall breach.")

    engine_a, journal_a = BacktestEngine(), []
    engine_b, journal_b = BacktestEngine(), []
    result_a = engine_run(research_bars, config, engine_a, sig_a, journal_a)
    result_b = engine_run(research_bars, config, engine_b, sig_b, journal_b)

    block_a = economic_block("vol_led:BENCHMARK_A(iter09)", research_bars, sig_a, result_a, journal_a, day_map, vol_bucket_map)
    block_b = economic_block("vol_led+reversal_transition:CANDIDATE_B", research_bars, sig_b, result_b, journal_b, day_map, vol_bucket_map)

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
    stats_a["gross_close_sum"] = str(id_a)
    stats_b["gross_close_sum"] = str(id_b)
    if str(id_a) != str(block_a["gross_close_edge"]):
        raise SystemExit(f"STOP: A gross reconstruction mismatch ({id_a} vs {block_a['gross_close_edge']}).")
    if str(id_b) != str(block_b["gross_close_edge"]):
        raise SystemExit(f"STOP: B gross reconstruction mismatch ({id_b} vs {block_b['gross_close_edge']}).")

    a_reg = _pivot_net(rts_a, "entry_regime")
    b_reg = _pivot_net(rts_b, "entry_regime")
    a_vol = _pivot_net(rts_a, "entry_vol_bucket")
    b_vol = _pivot_net(rts_b, "entry_vol_bucket")

    ctx = build_entry_context(research_bars, params)
    trans_a = transition_pivot(rts_a, prior_sign, ctx)
    trans_b = transition_pivot(rts_b, prior_sign, ctx)

    res = {
        "A": {
            "net": _dec(block_a["total_pnl"]),
            "rt": block_a["round_trips"], "max_dd": _dec(block_a["max_drawdown"]),
            "coverage": float(_dec(block_a["gross_close_edge"]) / (_dec(block_a["slippage"]) + _dec(block_a["commission"])) * 100),
            "win_rate": float(block_a["win_rate_pct"]),
            "hv_net": _dec(a_vol.get("high_vol", "0")),
            "mix_net": _dec(a_reg.get("mixed", "0")),
            "gross_loss": _dec(stats_a["gross_loss"]), "gross_profit": _dec(stats_a["gross_profit"]),
        },
        "B": {
            "net": _dec(block_b["total_pnl"]),
            "rt": block_b["round_trips"], "max_dd": _dec(block_b["max_drawdown"]),
            "coverage": float(_dec(block_b["gross_close_edge"]) / (_dec(block_b["slippage"]) + _dec(block_b["commission"])) * 100),
            "win_rate": float(block_b["win_rate_pct"]),
            "hv_net": _dec(b_vol.get("high_vol", "0")),
            "mix_net": _dec(b_reg.get("mixed", "0")),
            "gross_loss": _dec(stats_b["gross_loss"]), "gross_profit": _dec(stats_b["gross_profit"]),
        },
    }
    repeat = run_half_repeatability(research_bars, params, config)
    acceptance = evaluate_acceptance(res, {"A": ident_a, "B": ident_b}, rts_a, rts_b,
                                     repeat, len(violations), True, True)
    classification = classify_research(res, acceptance)

    a_by: dict = {}
    for r in rts_a:
        a_by[(r["entry"]["entry_index"], r["side"])] = r
    for v in vetoed_b:
        key = (v["bar_index"], v["side"])
        a_rt = a_by.get(key)
        v["benchmark_position_realized_net"] = str(a_rt["net"]) if a_rt else "no_round_trip"
    veto_sign_count = Counter(v["prior_session_return_sign"] for v in vetoed_b)

    def trace_rows(rts):
        rows = []
        for r in rts:
            i = r["entry"]["entry_index"]
            c = ctx[i] if i < len(ctx) else None
            rows.append({
                "entry_index": i, "entry_day": r["entry_day"],
                "side": r["side"],
                "raw_underlying_volmove": c["volmove"] if c else None,
                "expansion_ratio": c["expansion"] if c else None,
                "prior_session_return_sign": prior_sign[i] if i < len(prior_sign) else None,
                "prior_session_return": str(daily.returns[daily.bar_day_pos[i]]) if i < len(prior_sign) and daily.bar_day_pos[i] >= 0 else None,
                "transition": transition_label(prior_sign[i] if i < len(prior_sign) else None,
                                               c["volmove"] if c else None),
                "position_before": "FLAT",
                "exit_index": r["exit_index"], "exit_day": r["exit_day"],
                "exit_reason": r["exit_reason"], "realized_pnl_net": str(r["net"]),
            })
        return rows

    trace_a, trace_b = trace_rows(rts_a), trace_rows(rts_b)
    div = divergence_summary(rts_a, rts_b)
    pa = path_analysis(rts_a, rts_b, vetoed_b, day_map)
    day_rows = affected_day_rows(rts_a, rts_b, day_map, vetoed_b)
    day_attr_a = daily_attribution(rts_a, day_map)
    day_attr_b = daily_attribution(rts_b, day_map)

    out = {
        "experiment": "OUR_ALGO_001_TRANSITION_QUALITY",
        "objective": ("test the Iteration-012 future-hypothesis #1: whether the Iteration-009 VOL-led "
                      "architecture is fundamentally a regime-change detector.  Exactly ONE structural "
                      "change: restrict entries to REVERSAL transitions (down->up LONG / up->down SHORT); "
                      "no parameter tuning; no OOS."),
        "promotion_notice": ("Research acceptance criteria are NOT promotion criteria. Even a full "
                             "pass keeps PROMOTION=NO and ALGO READY=NO. No commit, no push."),
        "protected_oos_firewall": {
            "window": ["2025-10-06", "2026-09-11"],
            "used_for_selection": False,
            "oos_result_immutable": {"net": "+1710.004256155", "round_trips": 31, "wins": 20, "losses": 11},
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
            "id": "VOL-led + REVERSAL-TRANSITION ENTRY QUALITY GATE (B)",
            "isolated_change": ("suppress a new VOL-led ENTRY unless the last completed session's "
                                "signed close-to-close return OPPOSES the entry side (down->up LONG / "
                                "up->down SHORT); flat prior session carries no transition evidence"),
            "sign_condition": "sign(returns[k]) must equal -target (k = last completed session, causal)",
            "zero_new_thresholds": True,
            "one_structural_change": True,
            "not_a_regime_veto": True,
            "opposes_rejected_iter11_continuation": True,
            "retained": ["VOL signal", "VOL_GATE", "volatility calculation", "regime calculations",
                         "exits (provider ATR stop / max-hold / confluence-flat / confluence-broken)",
                         "stop-loss", "position sizing", "capital", "commission", "slippage",
                         "warmup", "execution", "multiplier", "position-state machinery",
                         "dataset", "evaluation configuration"],
            "no_workaround": True, "no_second_filter": True,
        },
        "method": {
            "single_controlled_experiment": True, "one_structural_change": True,
            "no_parameter_optimization": True, "no_oos_for_selection": True,
            "forbidden_optimizations": "none performed (no threshold/regime/ADX/RSI/VOL_GATE/MA/"
                                       "exit/sizing/vote/grid/ML/Bayesian tuning)",
        },
        "causality": {
            "gate_basis": ("sign of the LAST COMPLETED SESSION's close-to-close return (daily.returns, "
                           "existing Iteration-005 DailySeries) strictly before the entry bar"),
            "current_day_not_used": True,
            "no_future_bars": True, "no_end_of_day_information": True,
            "no_future_labels": True, "no_hindsight": True,
            "violations": len(violations), "violation_sample": violations[:5],
            "inherited_frozen_benchmark_timing": ("the A-arm confirmation _vol_move_confirm(daily, k+1, "
                "lookback) keeps its frozen semantics: range comparison uses completed sessions (causal) "
                "while its ±1 direction sign uses the ENTRY session's own close (same-day at a first-of-day "
                "bar). B does NOT alter this; B's gate uses only strictly-causal returns[k].")
        },
        "economics": {"A_vol_led": block_a, "B_reversal_transition": block_b},
        "economic_identity": {"A": ident_a, "B": ident_b},
        "engine_identity": {"A": engine_ident_a, "B": engine_ident_b},
        "trade_statistics": {"A": stats_a, "B": stats_b},
        "comparison": {
            "A": {k: str(v) for k, v in res["A"].items()},
            "B": {k: str(v) for k, v in res["B"].items()},
            "net_delta_B_minus_A": str(res["B"]["net"] - res["A"]["net"]),
            "rt_delta": stats_b["round_trips"] - stats_a["round_trips"],
            "retention_high_vol": (str(res["B"]["hv_net"] / res["A"]["hv_net"]) if res["A"]["hv_net"] > 0 else None),
            "retention_mixed": (str(res["B"]["mix_net"] / res["A"]["mix_net"]) if res["A"]["mix_net"] > 0 else None),
            "per_rt_delta": str(_per_rt(res["B"]) - _per_rt(res["A"])),
        },
        "transition_analysis": {
            "A": trans_a, "B": trans_b,
            "forensics_cells": {f"{k[0]} {k[1]}": v for k, v in ITER12_TRANSITION_CELLS.items()},
            "reversal_retained": trans_b.get("REVERSAL_LONG", {}).get("rt", 0) + trans_b.get("REVERSAL_SHORT", {}).get("rt", 0),
            "note": ("Iteration-012 forensics recorded net values rounded to 2dp; A here reproduces the "
                     "frozen Iteration-009 pipeline exactly"),
        },
        "entry_quality": {
            "A": pre_entry_return_quality(rts_a, prior_sign, daily),
            "B": pre_entry_return_quality(rts_b, prior_sign, daily),
        },
        "pivots": {
            "A": {"by_exit_reason": condition_pivot(rts_a, "exit_reason"),
                  "by_entry_regime": condition_pivot(rts_a, "entry_regime"),
                  "by_side": condition_pivot(rts_a, "side"),
                  "by_vol_bucket": condition_pivot(rts_a, "entry_vol_bucket"),
                  "by_hold_bucket": condition_pivot(rts_a, "hold_bucket")},
            "B": {"by_exit_reason": condition_pivot(rts_b, "exit_reason"),
                  "by_entry_regime": condition_pivot(rts_b, "entry_regime"),
                  "by_side": condition_pivot(rts_b, "side"),
                  "by_vol_bucket": condition_pivot(rts_b, "entry_vol_bucket"),
                  "by_hold_bucket": condition_pivot(rts_b, "hold_bucket")},
        },
        "transition_gate": {
            "suppressed_count": len(vetoed_b),
            "direct_benchmark_entries_suppressed": veto_direct,
            "secondary_path_suppressions": veto_secondary,
            "suppressed_records": vetoed_b,
            "suppressed_by_prior_sign": dict(sorted(veto_sign_count.items())),
        },
        "path_analysis": pa,
        "statefulness": {
            "trace_A": trace_a, "trace_B": trace_b,
            "divergence": div,
            "interpretation": ("shared same-bar-same-side positions are byte-identical including exit "
                               "bar/reason/net; A-only positions are either GATED (suppressed) entries or "
                               "positions displaced because B already held; B-only positions are path "
                               "additions arising from B being flat while A held."),
        },
        "daily_attribution": {"A": day_attr_a, "B": day_attr_b, "affected_days": day_rows},
        "acceptance": acceptance,
        "classification": classification,
        "classification_rule_reference": "OUR-ALGO-001 brief (pre-registered): PROMISING / INCONCLUSIVE / REJECTED",
        "repeatability": repeat,
        "leakage_checks": {
            "causal_series_violations": len(violations),
            "no_oos_in_research": True,
            "signal_streams_research_only": True,
            "benchmark_artifact_byte_unchanged": _sha256(ITER9_OUT) == ITER9_RESULT_SHA256,
            "gate_uses_only_prior_session": len(violations) == 0,
            "entry_change_isolation": entry_change_check,
        },
        "safety_state": {
            "promotion": "NO", "algo_ready": "NO", "algorithm_health": "RED",
            "scope.live_trading": False, "live_gate": "CLOSED",
            "paper_only": True, "human_approval_required": True, "model_0_frozen": True,
        },
        "trades": {"A": rts_a, "B": rts_b},
    }

    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    out_sha = _sha256(OUT_FILE)
    head_before = _git_head()
    print(f"our_algo_001 written: {OUT_FILE}  sha256={out_sha}")
    print(f"HEAD before: {head_before}")

    print("=" * 96)
    print("OUR-ALGO-001 - REVERSAL-TRANSITION QUALITY GATE (pre-OOS A/B, research domain only)")
    print("=" * 96)
    print(f"research domain: {len(research_bars)} bars / {research_days} days (OOS firewall enforced, no OOS replay)")
    print(f"{'net':18s} {str(block_a['total_pnl']):>22s} {str(block_b['total_pnl']):>22s}")
    print(f"{'gross edge':18s} {str(block_a['gross_close_edge']):>22s} {str(block_b['gross_close_edge']):>22s}")
    print(f"{'round trips':18s} {block_a['round_trips']:>22d} {block_b['round_trips']:>22d}")
    print(f"{'fills':18s} {block_a['fills']:>22d} {block_b['fills']:>22d}")
    print(f"{'win rate':18s} {float(block_a['win_rate_pct']):>21.2f}% {float(block_b['win_rate_pct']):>21.2f}%")
    print(f"{'max drawdown':18s} {str(block_a['max_drawdown']):>22s} {str(block_b['max_drawdown']):>22s}")
    print(f"{'cost coverage':18s} {res['A']['coverage']:>21.2f}% {res['B']['coverage']:>21.2f}%")
    print(f"{'per-RT net':18s} {str(_per_rt(res['A'])):>22s} {str(_per_rt(res['B'])):>22s}")
    print(f"{'high_vol net':18s} {str(a_vol.get('high_vol','0')):>22s} {str(b_vol.get('high_vol','0')):>22s}")
    print(f"{'mixed net':18s} {str(a_reg.get('mixed','0')):>22s} {str(b_reg.get('mixed','0')):>22s}")
    print(f"gated entries: {len(vetoed_b)} (direct benchmark-entry suppressions {veto_direct}, "
          f"secondary path suppressions {veto_secondary})   suppressed by prior sign: "
          + "; ".join(f"{k}={v}" for k, v in sorted(veto_sign_count.items())))
    print(f"identity A: full {ident_a['full_identity_holds']} closed {ident_a['closed_identity_holds']}"
          f"  B: full {ident_b['full_identity_holds']} closed {ident_b['closed_identity_holds']}")
    print(f"path: common {pa['common_same_bar_same_side']} / A-only {pa['a_only']} "
          f"/ B-only {pa['b_only']}  -> {pa['stateful_path_change']}")
    print(f"acceptance ALL: {acceptance['all_acceptance_satisfied']} "
          f"(satisfied {acceptance['satisfied']} failed {acceptance['failed']}) "
          f"quality {acceptance['quality_improves']} net {acceptance['net_improves']}")
    print(f"classification: {classification}")
    print("EXPLORATORY ONLY - no parameter change, no OOS use, no promotion, no ALGO READY.")
    print("=" * 96)
    print("STOP")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())