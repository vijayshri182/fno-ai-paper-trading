"""ITERATION 011 - VOLATILITY-EXPANSION QUALITY CONFIRMATION (pre-OOS A/B, research only).

Single controlled research experiment.  Question: can a causal confirmation of
volatility-move PERSISTENCE (same direction, non-shrinking magnitude across the
two most recent COMPLETED sessions) distinguish productive volatility expansion
from one-off expansion, i.e. remove low-quality entries while retaining the high
-volatility / mixed-regime economic capture of the Iteration-009 VOL-led
architecture?

EXPERIMENT (exactly one structural change, no parameter tuning):
  A = frozen Iteration-009 VOL-led benchmark (vol-confirmation-only entries,
      reproduced byte-for-byte and guarded against the persisted
      iteration_009_vol_led.json artifact).
  B = VOL-led + CAUSAL VOLATILITY-EXPANSION PERSISTENCE CONFIRMATION.  When the
      existing Iteration-009 VOL-led entry condition fires, B additionally
      requires that the "volatility-move signal" be directionally persistent
      relative to the immediately preceding completed session and non-shrinking:

        LONG :  current_volmove > 0 AND previous_volmove > 0
                AND abs(current_volmove) >= abs(previous_volmove)
        SHORT:  current_volmove < 0 AND previous_volmove < 0
                AND abs(current_volmove) >= abs(previous_volmove)

      Everything else (VOL signal, VOL_GATE, volatility/regime calculations,
      exits, stop-loss, sizing, capital, costs, slippage, warmup, execution,
      multiplier, position-state machinery, dataset, config) is unchanged.

CAUSALITY: the "volatility-move signal" for the confirmation is the EXISTING
daily close-to-close signed move of a completed session (``DailySeries.returns``
-- no new indicator, no threshold, no parameter).  At a first-of-day entry bar
``i`` whose last COMPLETED session index is ``k = daily.bar_day_pos[i]`` (the
session strictly before the bar containing ``i``):

    current_volmove(i) = returns[k]      (last completed session - causal)
    previous_volmove(i)= returns[k - 1]  (session before that - causal)

Both values use only sessions that are fully closed strictly before bar ``i``
(closes from bars before ``i``).  A bar-level violation scan asserts every
confirmation input is strictly-before -> ``violations`` must be empty.  The
confirmation NEVER consumes the running session's return (the A-arm signal
machinery itself is untouched and byte-for-byte frozen).

FIREWALL: protected OOS window (2025-10-06 .. 2026-09-11) is not used for
selection, tuning, threshold choice, or architecture choice.  Engine replays
run ONLY on the pre-OOS research domain (2022-01-03 .. 2025-10-03,
69,781 bars / 932 days).  Hard assertions abort with STOP on any OOS touch.

Classification (pre-registered): PROMISING RESEARCH RESULT / INCONCLUSIVE /
REJECTED (see ACCEPTANCE, 10 conditions).  Even a full pass keeps PROMOTION=NO,
ALGO READY=NO.  No commit, no push.
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
    _action_maps,
    _entry_suppressions_only,
    _git_head,
    _pivot_net,
    _position_before,
    _sha256,
    _streams_identical,
    chronological_split,
    signatures_are_actionable,
)

REPO = Path(__file__).resolve().parents[3]
DATASET = REPO / "datasets" / "upstox_Nifty_50_5m_20220103_20260911.csv"
FINGERPRINT_FILE = REPO / "runs" / "research" / "day_batch" / "iteration_006_n3_fingerprint.json"
ITER6_OUT = REPO / "runs" / "research" / "day_batch" / "iteration_006_protected_oos_n3.json"
ITER7_OUT = REPO / "runs" / "research" / "day_batch" / "iteration_007_robustness_audit_n3.json"
ITER8_OUT = REPO / "runs" / "research" / "day_batch" / "iteration_008_component_regime_attribution_n3.json"
ITER9_OUT = REPO / "runs" / "research" / "day_batch" / "iteration_009_vol_led.json"
ITER10_OUT = REPO / "runs" / "research" / "day_batch" / "iteration_010_sideways_veto.json"
OUT_FILE = REPO / "runs" / "research" / "day_batch" / "iteration_011_vol_expansion_quality.json"

DATA_HASH = "6c400b016c1c6d3c99a80db9a8355bc3e195e17c43537bcb93b7e198e85b7f5c"
FINGERPRINT_SHA256 = "5fd5d1a2d8966ed4035a93911f93e4300575caaf3475fb742d4b202e4c257a7e"
ITER6_RESULT_SHA256 = "4456db0133bede4ff332ba07c6ae14e8d8c734472e2a4c98bdd00870336ed5b5"
ITER7_RESULT_SHA256 = "b21171123491945ed8316e57cfffde6c71e29523224121a503b22b5fe779c8ca"
ITER8_RESULT_SHA256 = "52e5ee8e5edc3caca3450d341e9d04416295f5d0993ce0d8b54e2c326cf0eca5"
ITER9_RESULT_SHA256 = "6e9713c30370207fc18ac5c8924ed5742ff3a8f23d639143b82164e6942a0dc2"
ITER10_RESULT_SHA256 = "1dc3f7fabe65e42ed72ce507193f9307c4ab6bd7cde8350ca3724556c3d0f8bf"

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

ACCEPTANCE = {
    "A_net_improves": "B net > A net (meaningful improvement)",
    "B_no_opportunity_collapse": "B round trips >= 0.60 x A round trips (retained opportunity)",
    "C_high_vol_retention": "B high-vol P&L >= 0.90 x A high-vol P&L",
    "D_mixed_retention": "B mixed-regime P&L >= 0.90 x A mixed-regime P&L",
    "E_cost_coverage": "B cost coverage >= A cost coverage - 25pp and B coverage > 100 (%)",
    "F_drawdown": "B maxDD <= 1.25 x A maxDD (not +25%)",
    "G_repeatability": "both chronological halves show B net > A net for the same structural reason",
    "H_causality": "zero causal-series violations and entry-change isolation holds (no unexplained opens)",
    "I_accounting": "closed AND full economic identity hold for A and B",
    "J_reconstruction": "round-trip gross reconstruction equals economic-block gross edge for A and B",
}
TRIVIAL_GUARD_NOTE = ("a strategy that simply stops trading is not successful; if most of the P&L "
                      "improvement comes from eliminating most trading activity the evidence is weak")

VOL_QUALITY_CLASSES = {
    "SAME_SIGN_AND_EXPANDING": "same sign and |current| > |previous|",
    "SAME_SIGN_AND_FLAT": "same sign and |current| == |previous| (within float epsilon)",
    "SAME_SIGN_AND_CONTRACTING": "same sign and |current| < |previous|",
    "SIGN_FLIP": "opposite signs",
    "ZERO_UNDEFINED": "either value is zero or undefined (warmup/None)",
}


# ---------------------------------------------------------------------------
# causal volmove-quality series (existing primitive, strictly decision-time)
# ---------------------------------------------------------------------------


def build_vol_quality_series(bars) -> tuple[list, list, list[dict]]:
    """Per-bar causal signed volmove pair ``(cur, prev)`` plus violation scan.

    For bar ``i`` with ``k = daily.bar_day_pos[i]`` (the last COMPLETED session
    strictly before bar ``i``):

        cur(i)  = daily.returns[k]        (move of the last completed session)
        prev(i) = daily.returns[k - 1]    (move of the session before that)

    Because ``bar_day_pos`` references only sessions strictly before bar ``i``,
    every input uses closes from bars strictly before ``i``.  ``violations``
    lists any bar where the referenced session's last bar is NOT strictly
    before ``i`` (must be empty when consumed by the variant).
    """
    n = len(bars)
    daily = DailySeries.build(bars)
    returns = daily.returns
    cur: list = [None] * n
    prev: list = [None] * n
    violations: list[dict] = []
    for i in range(n):
        k = daily.bar_day_pos[i]
        if k < 0:
            cur[i] = None
            prev[i] = None
            continue
        cur[i] = returns[k]
        prev[i] = returns[k - 1] if k - 1 >= 0 else None
        hi = daily.last_idx[k]
        if not (hi < i):
            violations.append({"bar_index": i, "session": k,
                               "session_last_index": hi, "uses_future": hi >= i})
    return cur, prev, violations


def volmove_quality_class(cur, prev) -> str:
    """Classify the causal (current, previous) volmove relationship (5 classes)."""
    if cur is None or prev is None or cur == 0 or prev == 0:
        return "ZERO_UNDEFINED"
    if (cur > 0) != (prev > 0):
        return "SIGN_FLIP"
    ac, ap = abs(cur), abs(prev)
    eps = 1e-12 * max(ac, ap)
    if ac > ap + eps:
        return "SAME_SIGN_AND_EXPANDING"
    if ac < ap - eps:
        return "SAME_SIGN_AND_CONTRACTING"
    return "SAME_SIGN_AND_FLAT"


def confirmation_passes(cur, prev, target: int) -> bool:
    """Persistence + non-shrinking confirmation on the causal pair.

    LONG (target=+1): both moves positive and |current| >= |previous|.
    SHORT (target=-1): both moves negative and |current| >= |previous|.
    Zero / undefined values fail the confirmation.
    """
    if cur is None or prev is None or cur == 0 or prev == 0:
        return False
    if target > 0:
        return cur > 0 and prev > 0 and abs(cur) >= abs(prev)
    if target < 0:
        return cur < 0 and prev < 0 and abs(cur) >= abs(prev)
    return False


# ---------------------------------------------------------------------------
# frozen-loop variant with the vol-quality confirmation (byte-identical +
# confirmation when off)
# ---------------------------------------------------------------------------


def ensemble_variant_vol_quality(bars, params: EnsembleParams, cur, prev, confirm: bool = True):
    """VOL-led candidate (use_trend=False, use_vol=True) + vol-quality gate.

    With ``confirm=False`` the emitted signal stream is byte-identical to
    ``ensemble_variant(bars, params, use_trend=False, use_vol=True)`` (the
    Iteration-009 benchmark), asserted in main().  With ``confirm=True`` the ONLY
    difference is that a first-of-day VOL-led ENTRY is suppressed when the causal
    persistence confirmation fails (``confirmation_passes(cur[i], prev[i],
    target)`` is False).  Exits, stops, holds, sizing and meta are untouched.
    Returns (signals, suppressed) where ``suppressed`` records the flat replaces.
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
    suppressed: list[dict] = []
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
                gate = confirmation_passes(cur[i], prev[i], target) if confirm else True
                if target == 1 and gate:
                    state = 1; hold_days = 0; entry_day = k + 1
                    atr_ref = daily.avg_range_before(k + 1, params.lookback)
                    entry_ref = _f(bridge.close)
                    signals[i] = _open_signal(bridge, 1, "ensemble confluence up - enter long", meta)
                    continue
                if target == -1 and gate:
                    state = -1; hold_days = 0; entry_day = k + 1
                    atr_ref = daily.avg_range_before(k + 1, params.lookback)
                    entry_ref = _f(bridge.close)
                    signals[i] = _open_signal(bridge, -1, "ensemble confluence down - enter short", meta)
                    continue
                if confirm and not gate and target != 0:
                    suppressed.append({
                        "bar_index": i, "day": day.isoformat(), "target": target,
                        "side": "LONG" if target == 1 else "SHORT",
                        "cur_volmove": cur[i], "prev_volmove": prev[i],
                        "quality_class": volmove_quality_class(cur[i], prev[i]),
                        "decision_benchmark": "enter " + ("long" if target == 1 else "short"),
                        "decision_candidate": "flat - vol-quality confirmation denied",
                    })
                    signals[i] = _hold(bridge, "FLAT - vol-quality confirmation denied (research)", meta)
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
    return signals, suppressed


# ---------------------------------------------------------------------------
# opportunity / retention / path analysis
# ---------------------------------------------------------------------------


def _rt_key(rts: Sequence[dict]) -> dict[tuple[int, str], dict]:
    return {(r["entry"]["entry_index"], r["side"]): r for r in rts}


def path_analysis(rts_a, rts_b, suppressed, day_map) -> dict:
    a_by = _rt_key(rts_a)
    b_by = _rt_key(rts_b)
    a_keys, b_keys = set(a_by), set(b_by)
    common, a_only, b_only = a_keys & b_keys, a_keys - b_keys, b_keys - a_keys
    sup_set = {s["bar_index"] for s in suppressed}

    a_only_tags = []
    for key in sorted(a_only):
        rt = a_by[key]
        idx = rt["entry"]["entry_index"]
        a_only_tags.append({
            "entry_index": idx, "side": rt["side"], "entry_day": rt["entry_day"],
            "exit_reason": rt["exit_reason"], "net": str(rt["net"]),
            "entry_day_regime": rt.get("entry_regime"),
            "reason_for_divergence": ("confirmation_denied" if idx in sup_set
                                      else "displaced_by_b_position"),
        })
    b_only_tags = [{
        "entry_index": rt["entry"]["entry_index"], "side": rt["side"],
        "entry_day": rt["entry_day"], "exit_reason": rt["exit_reason"],
        "net": str(rt["net"]), "entry_day_regime": rt.get("entry_regime"),
        "reason_for_divergence": "b_flat_when_a_holding_or_reentry",
    } for key in sorted(b_only) for rt in [b_by[key]]]

    exit_identical = all(
        a_by[k]["exit_index"] == b_by[k]["exit_index"]
        and a_by[k]["exit_reason"] == b_by[k]["exit_reason"]
        and str(a_by[k]["net"]) == str(b_by[k]["net"])
        for k in common
    )

    def tag_net(tags):
        return str(sum((Decimal(t["net"]) for t in tags), Decimal("0")))

    def reg_winloss(tags):
        c = Counter((t.get("entry_day_regime") or "?") + ":" + ("W" if Decimal(t["net"]) > 0 else "L")
                    for t in tags)
        return dict(sorted(c.items()))

    return {
        "common_same_bar_same_side": len(common),
        "a_only": len(a_only_tags),
        "b_only": len(b_only_tags),
        "exit_machinery_unchanged_shared_positions_identical": exit_identical,
        "a_only_tags": a_only_tags,
        "b_only_tags": b_only_tags,
        "a_only_summary": {
            "count": len(a_only_tags),
            "confirmation_denied": sum(1 for t in a_only_tags if t["reason_for_divergence"] == "confirmation_denied"),
            "displaced_by_b_position": sum(1 for t in a_only_tags if t["reason_for_divergence"] == "displaced_by_b_position"),
            "net": tag_net(a_only_tags),
            "regime_winloss": reg_winloss(a_only_tags),
            "winners": sum(1 for t in a_only_tags if Decimal(t["net"]) > 0),
            "losers": sum(1 for t in a_only_tags if Decimal(t["net"]) <= 0),
            "winners_net": tag_net([t for t in a_only_tags if Decimal(t["net"]) > 0]),
            "losers_net": tag_net([t for t in a_only_tags if Decimal(t["net"]) <= 0]),
        },
        "b_only_summary": {
            "count": len(b_only_tags),
            "net": tag_net(b_only_tags),
            "regime_winloss": reg_winloss(b_only_tags),
            "winners": sum(1 for t in b_only_tags if Decimal(t["net"]) > 0),
            "losers": sum(1 for t in b_only_tags if Decimal(t["net"]) <= 0),
            "winners_net": tag_net([t for t in b_only_tags if Decimal(t["net"]) > 0]),
            "losers_net": tag_net([t for t in b_only_tags if Decimal(t["net"]) <= 0]),
        },
        "stateful_path_change": ("SUPPRESSION_PLUS_DOWNSTREAM" if b_only else
                                 ("SUPPRESSION_ONLY" if a_only else "NONE")),
    }


def affected_day_rows(rts_a, rts_b, day_map, suppressed) -> dict:
    def by_day(rts):
        out = {}
        for r in rts:
            out.setdefault(r["entry_day"], []).append(r)
        return out

    a_d, b_d = by_day(rts_a), by_day(rts_b)
    all_days = sorted(set(a_d) | set(b_d))
    sup_by_day: Counter = Counter(s["day"] for s in suppressed)
    rows = []
    for d in all_days:
        ag, bg = a_d.get(d, []), b_d.get(d, [])
        a_net = sum(Decimal(r["net"]) for r in ag)
        b_net = sum(Decimal(r["net"]) for r in bg)
        suppressed_here = int(sup_by_day.get(d, 0))
        if len(ag) == len(bg) and a_net == b_net and suppressed_here == 0:
            continue
        rows.append({
            "day": d, "regime": day_map.get(d, "?"),
            "benchmark_trades": len(ag), "candidate_trades": len(bg),
            "benchmark_net": str(a_net), "candidate_net": str(b_net),
            "delta": str(b_net - a_net),
            "entries_suppressed": suppressed_here,
            "exits_changed": 0,
            "state_divergence": "confirmation-suppressed" if suppressed_here else "path-added/reordered",
        })
    return {
        "affected_day_count": len(rows), "rows": rows,
        "layer_note": ("LAYERS: RAW SIGNAL = per-bar meta 'target' (byte-identical between arms); "
                       "FINAL DECISION = open/close/hold signal emitted; POSITION STATE = engine state "
                       "after the bar; REALIZED TRADE RESULT = round-trip net (post-cost). The "
                       "confirmation changes only the FINAL DECISION layer for new entries."),
    }


def opportunity_retention(rts_a, rts_b, suppressed, direct_count, secondary_count) -> dict:
    a_keys = {(r["entry"]["entry_index"], r["side"]) for r in rts_a}
    b_keys = {(r["entry"]["entry_index"], r["side"]) for r in rts_b}
    common = a_keys & b_keys
    gross_a = it5.amt_sum(rts_a, "gross_close")
    gross_b = it5.amt_sum(rts_b, "gross_close")
    net_a = it5.amt_sum(rts_a, "net")
    net_b = it5.amt_sum(rts_b, "net")
    return {
        "a_entries": len(a_keys), "b_entries": len(b_keys),
        "common_same_bar_same_side": len(common),
        "a_only": len(a_keys - b_keys), "b_only": len(b_keys - a_keys),
        "entries_suppressed": len(suppressed),
        "direct_suppressions": direct_count,
        "secondary_suppressions": secondary_count,
        "retention_gross_pct": round(float(gross_b / gross_a * 100), 2) if gross_a else None,
        "retention_net_pct": round(float(net_b / net_a * 100), 2) if net_a else None,
        "net_delta": str(net_b - net_a),
    }


def vol_quality_pivot(rts, cur, prev) -> list[dict]:
    by_class = OrderedDict()
    for r in rts:
        i = r["entry"]["entry_index"]
        cls = volmove_quality_class(cur[i], prev[i])
        by_class.setdefault(cls, []).append(r)
    out = []
    for cls, gr in by_class.items():
        winners = [r for r in gr if r["net"] > 0]
        out.append({
            "vol_quality_class": cls,
            "count": len(gr),
            "wins": len(winners),
            "losses": len(gr) - len(winners),
            "net": str(it5.amt_sum(gr, "net")),
            "gross": str(it5.amt_sum(gr, "gross_close")),
        })
    return out


def winner_loser_analysis(rts, day_map, vol_bucket_map) -> dict:
    pools = {
        "sideways": [r for r in rts if r.get("entry_regime") == "sideways"],
        "high_vol": [r for r in rts if r.get("entry_vol_bucket") == "high_vol"],
        "mixed": [r for r in rts if r.get("entry_regime") == "mixed"],
    }
    out = {}
    for name, pool in pools.items():
        winners = [r for r in pool if r["net"] > 0]
        losers = [r for r in pool if r["net"] <= 0]
        out[name] = {
            "trades": len(pool),
            "profitable_trades": len(winners), "profitable_net": str(it5.amt_sum(winners, "net")),
            "losing_trades": len(losers), "losing_net": str(it5.amt_sum(losers, "net")),
        }
    return out


# ---------------------------------------------------------------------------
# acceptance + classification (10 pre-registered conditions)
# ---------------------------------------------------------------------------


def evaluate_acceptance(res, ident, trades_a, trades_b, halves, causality_violations,
                        gross_ok_a, gross_ok_b) -> dict:
    a, b = res["A"], res["B"]
    crit = {
        "A_net_improves": bool(b["net"] > a["net"]),
        "B_no_opportunity_collapse": bool(b["rt"] >= Decimal("0.60") * a["rt"]),
        "C_high_vol_retention": bool(a["hv_net"] > 0 and b["hv_net"] >= Decimal("0.90") * a["hv_net"]),
        "D_mixed_retention": bool(a["mix_net"] > 0 and b["mix_net"] >= Decimal("0.90") * a["mix_net"]),
        "E_cost_coverage": bool(b["coverage"] >= a["coverage"] - 25.0 and b["coverage"] > 100.0),
        "F_drawdown": bool(b["max_dd"] <= Decimal("1.25") * a["max_dd"]),
        "G_repeatability": bool(halves["h1_improves"] and halves["h2_improves"]),
        "H_causality": bool(causality_violations == 0),
        "I_accounting": bool(ident["A"]["closed_identity_holds"] and ident["A"]["full_identity_holds"]
                             and ident["B"]["closed_identity_holds"] and ident["B"]["full_identity_holds"]),
        "J_reconstruction": bool(gross_ok_a and gross_ok_b),
    }
    removed = a["rt"] - b["rt"]
    removed_pct = removed / a["rt"] * 100.0 if a["rt"] else 0.0
    b_only_net = sum((Decimal(x["net"]) for x in trades_b
                      if (x["entry"]["entry_index"], x["side"])
                      not in {(r["entry"]["entry_index"], r["side"]) for r in trades_a}), Decimal("0"))
    opportunity_collapse = bool(removed_pct > 60.0 and b_only_net <= 0)
    return {
        "criteria": crit,
        "satisfied": [k for k, v in crit.items() if v],
        "failed": [k for k, v in crit.items() if not v],
        "all_acceptance_satisfied": bool(crit and all(crit.values())),
        "trivial_guard": {
            "trades_removed": int(removed), "trades_removed_pct": round(removed_pct, 2),
            "candidate_net_improvement": str(b["net"] - a["net"]),
            "net_of_path_additions_b_only": str(b_only_net),
            "opportunity_collapse": opportunity_collapse,
            "weak_evidence": bool(removed_pct > 50.0 and b_only_net <= 0),
            "weak_note": TRIVIAL_GUARD_NOTE,
        },
    }


def classify_research(res, acceptance) -> str:
    if res["B"]["net"] <= res["A"]["net"]:
        return "REJECTED"
    if acceptance["all_acceptance_satisfied"]:
        return "PROMISING RESEARCH RESULT"
    return "INCONCLUSIVE"


# ---------------------------------------------------------------------------
# repeatability (section 17) - chronological halves of the research domain
# ---------------------------------------------------------------------------


def half_experiment(half_bars, params, config) -> dict:
    """A/B on one chronological half (independently warmed, no tuning)."""
    cur, prev, violations = build_vol_quality_series(half_bars)
    dmap, vmap = classed_days(half_bars)
    sig_a, sup_a = ensemble_variant_vol_quality(half_bars, params, cur, prev, confirm=False)
    sig_b, sup_b = ensemble_variant_vol_quality(half_bars, params, cur, prev, confirm=True)
    if sup_a:
        raise RuntimeError("STOP: confirm=False produced suppression records (logic fault).")
    engine_a, j_a = BacktestEngine(), []
    engine_b, j_b = BacktestEngine(), []
    res_a = engine_run(half_bars, config, engine_a, sig_a, j_a)
    res_b = engine_run(half_bars, config, engine_b, sig_b, j_b)
    rts_a, _, _ = it5.build_round_trips(half_bars, j_a)
    rts_b, _, _ = it5.build_round_trips(half_bars, j_b)
    for rt in list(rts_a) + list(rts_b):
        it5.add_flags(rt, half_bars, dmap, vmap)
    a_reg = _pivot_net(rts_a, "entry_regime")
    b_reg = _pivot_net(rts_b, "entry_regime")
    a_vol = _pivot_net(rts_a, "entry_vol_bucket")
    b_vol = _pivot_net(rts_b, "entry_vol_bucket")
    return {
        "benchmark": {"net": str(res_a.total_pnl), "round_trips": len(rts_a),
                      "wins": sum(1 for r in rts_a if r["net"] > 0),
                      "losses": sum(1 for r in rts_a if r["net"] <= 0),
                      "by_regime": a_reg, "by_vol_bucket": a_vol,
                      "hv_net": a_vol.get("high_vol", "0"),
                      "mix_net": a_reg.get("mixed", "0")},
        "candidate": {"net": str(res_b.total_pnl), "round_trips": len(rts_b),
                      "wins": sum(1 for r in rts_b if r["net"] > 0),
                      "losses": sum(1 for r in rts_b if r["net"] <= 0),
                      "by_regime": b_reg, "by_vol_bucket": b_vol,
                      "hv_net": b_vol.get("high_vol", "0"),
                      "mix_net": b_reg.get("mixed", "0")},
        "suppressed": len(sup_b),
        "causality_violations": len(violations),
        "improves": bool(res_b.total_pnl > res_a.total_pnl),
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
        "evidence": ("repeatability assessed by whether the vol-quality retention/removal effect "
                     "reproduces in both chronological halves (recorded numbers only; no promotion inference)"),
    }


# ---------------------------------------------------------------------------
# entry / terminal (canonical pipeline)
# ---------------------------------------------------------------------------


def main() -> int:
    for p, h in ((FINGERPRINT_FILE, FINGERPRINT_SHA256), (ITER6_OUT, ITER6_RESULT_SHA256),
                 (ITER7_OUT, ITER7_RESULT_SHA256), (ITER8_OUT, ITER8_RESULT_SHA256),
                 (ITER9_OUT, ITER9_RESULT_SHA256), (ITER10_OUT, ITER10_RESULT_SHA256)):
        if not p.exists() or _sha256(p) != h:
            raise SystemExit(f"STOP: provenance artifact drifted or missing: {p.name}")

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

    cur, prev, violations = build_vol_quality_series(research_bars)
    if violations:
        raise SystemExit("STOP: causal volmove series non-causal: " + json.dumps(violations[:5]))
    class_counts = Counter(volmove_quality_class(c, p) for c, p in zip(cur, prev))

    sig_a, sup_a = ensemble_variant_vol_quality(research_bars, params, cur, prev, confirm=False)
    sig_b, sup_b = ensemble_variant_vol_quality(research_bars, params, cur, prev, confirm=True)
    if sup_a:
        raise SystemExit("STOP: confirm=False produced suppression records (logic fault).")

    sig_bench = ensemble_variant(research_bars, params, use_trend=False, use_vol=True)
    if not _streams_identical(sig_a, sig_bench):
        raise SystemExit("STOP: confirm=False signal stream diverges from Iteration-009 benchmark generator.")
    entry_change_ok, entry_change_check = _entry_suppressions_only(
        sig_b, sig_bench, [(s["bar_index"], s["side"]) for s in sup_b])
    if not entry_change_ok:
        raise SystemExit("STOP: confirm=True changed more than entry decisions (structural leak): "
                         + json.dumps(entry_change_check))
    for s in sup_b:
        s["suppression_class"] = entry_change_check["per_slot_class"].get(s["bar_index"], "secondary")
    sup_direct = entry_change_check["direct_benchmark_entries_suppressed"]
    sup_secondary = entry_change_check["secondary_path_suppressions"]
    for name, sig in (("A", sig_a), ("B", sig_b)):
        if not signatures_are_actionable(sig, research_bars):
            raise SystemExit(f"STOP: {name} signal stream malformed.")
        if any(b.timestamp.date() >= OOS_START for b in research_bars):
            raise SystemExit(f"STOP: {name} ran over OOS bars - firewall breach.")

    # determinism: run B twice, full engine replay, and require identical results
    engine_b, journal_b = BacktestEngine(), []
    result_b = engine_run(research_bars, config, engine_b, sig_b, journal_b)
    engine_b2, journal_b2 = BacktestEngine(), []
    result_b2 = engine_run(research_bars, config, engine_b2, sig_b, journal_b2)
    deterministic = (str(result_b.total_pnl) == str(result_b2.total_pnl)
                     and result_b.orders_filled == result_b2.orders_filled
                     and len(journal_b) == len(journal_b2))

    engine_a, journal_a = BacktestEngine(), []
    result_a = engine_run(research_bars, config, engine_a, sig_a, journal_a)

    block_a = economic_block("vol_led:BENCHMARK_A(iter09)", research_bars, sig_a, result_a, journal_a, day_map, vol_bucket_map)
    block_b = economic_block("vol_led+vol_quality_confirmation:CANDIDATE_B", research_bars, sig_b, result_b, journal_b, day_map, vol_bucket_map)

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
    gross_ok_a = str(id_a) == str(block_a["gross_close_edge"])
    gross_ok_b = str(id_b) == str(block_b["gross_close_edge"])
    if not gross_ok_a:
        raise SystemExit(f"STOP: A gross reconstruction mismatch ({id_a} vs {block_a['gross_close_edge']}).")
    if not gross_ok_b:
        raise SystemExit(f"STOP: B gross reconstruction mismatch ({id_b} vs {block_b['gross_close_edge']}).")

    a_reg = _pivot_net(rts_a, "entry_regime")
    b_reg = _pivot_net(rts_b, "entry_regime")
    a_vol = _pivot_net(rts_a, "entry_vol_bucket")
    b_vol = _pivot_net(rts_b, "entry_vol_bucket")

    a_by = _rt_key(rts_a)
    for s in sup_b:
        key = (s["bar_index"], s["side"])
        a_rt = a_by.get(key)
        s["benchmark_position_realized_net"] = str(a_rt["net"]) if a_rt else "no_round_trip"
    sup_regime_count = Counter(day_map.get(s["day"], "?") for s in sup_b)
    sup_side_count = Counter(s["side"] for s in sup_b)
    sup_class_count = Counter(s["quality_class"] for s in sup_b)

    ctx = build_entry_context(research_bars, params)

    def trace_rows(rts):
        rows = []
        for r in rts:
            i = r["entry"]["entry_index"]
            rows.append({
                "entry_index": i, "entry_day": r["entry_day"], "side": r["side"],
                "raw_underlying_volmove": ctx[i]["volmove"] if ctx[i] else None,
                "vol_gate_state": "ACTIVE_RETAINED",
                "conf_cur_volmove": cur[i], "conf_prev_volmove": prev[i],
                "vol_quality_class": volmove_quality_class(cur[i], prev[i]),
                "position_before": "FLAT",
                "exit_index": r["exit_index"], "exit_day": r["exit_day"],
                "exit_reason": r["exit_reason"], "realized_pnl_net": str(r["net"]),
            })
        return rows

    trace_a, trace_b = trace_rows(rts_a), trace_rows(rts_b)
    div = divergence_summary(rts_a, rts_b)
    pa = path_analysis(rts_a, rts_b, sup_b, day_map)
    day_rows = affected_day_rows(rts_a, rts_b, day_map, sup_b)
    day_attr_a = daily_attribution(rts_a, day_map)
    day_attr_b = daily_attribution(rts_b, day_map)
    opp = opportunity_retention(rts_a, rts_b, sup_b, sup_direct, sup_secondary)

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
                                     repeat, len(violations), gross_ok_a, gross_ok_b)
    classification = classify_research(res, acceptance)

    out = {
        "experiment": "ITERATION_011_VOL_EXPANSION_QUALITY",
        "objective": ("test whether a causal confirmation of volatility-move persistence "
                      "(same direction, non-shrinking magnitude across the two most recent COMPLETED "
                      "sessions) removes low-quality entries while retaining the high-vol / mixed-regime "
                      "capture of Iteration-009 VOL-led; exactly ONE structural change; no parameter tuning; no OOS"),
        "hypothesis": ("causally-confirmable volatility-expansion persistence separates productive "
                       "volatility expansion from one-off expansion"),
        "benchmark": "Iteration-009 VOL-led structural candidate (frozen, byte-for-byte)",
        "candidate": "VOL-led + volatility-expansion persistence confirmation (causal, entry-only gate)",
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
        "data_fingerprint": DATA_HASH,
        "benchmark_frozen": {
            "candidate": "Iteration-009 VOL-led structural candidate",
            "iteration_009_artifact_sha256": ITER9_RESULT_SHA256,
            "recorded": ITER9_VOL_LED_EXPECTED,
            "guard_equal_iteration009": not guard_mismatches,
        },
        "candidate_definition": {
            "id": "VOL-led + VOLATILITY-EXPANSION PERSISTENCE CONFIRMATION (B)",
            "isolated_change": ("suppress a new VOL-led ENTRY when the causal volmove-persistence "
                                "confirmation fails (same direction AND |current| >= |previous| on the "
                                "two most recent COMPLETED sessions)"),
            "retained": ["VOL signal", "VOL_GATE", "volatility calculation", "regime calculations",
                         "exits (provider ATR stop / max-hold / confluence-flat / confluence-broken)",
                         "stop-loss", "position sizing", "capital", "commission", "slippage",
                         "warmup", "execution", "multiplier", "position-state machinery",
                         "dataset", "evaluation configuration"],
            "no_workaround": True, "no_second_filter": True, "no_new_indicator": True,
            "no_volatility_threshold_added": True, "no_parameter_sweep": True,
        },
        "method": {
            "single_controlled_experiment": True, "one_structural_change": True,
            "no_parameter_optimization": True, "no_oos_for_selection": True,
            "forbidden_optimizations": "none performed (no threshold/regime/ADX/RSI/VOL_GATE/MA/"
                                       "exit/sizing/vote/grid/ML/Bayesian tuning)",
            "b_independently_replayed": True,
        },
        "causality": {
            "confirmation_basis": ("existing DailySeries.returns of the last two COMPLETED sessions "
                                   "strictly before the entry bar; sign = direction, magnitude = |return|"),
            "current_volmove_at_bar_i": "returns[bar_day_pos[i]] (last completed session - causal)",
            "previous_volmove_at_bar_i": "returns[bar_day_pos[i] - 1] (session before - causal)",
            "running_session_return_not_used": True,
            "no_future_bars": True, "no_end_of_day_information": True,
            "no_future_labels": True, "no_hindsight": True,
            "violations": len(violations), "violation_sample": violations[:5],
            "per_bar_class_histogram": dict(sorted(class_counts.items())),
            "vol_quality_class_definitions": VOL_QUALITY_CLASSES,
        },
        "economics": {"A_vol_led": block_a, "B_vol_quality": block_b},
        "economic_identity": {"A": ident_a, "B": ident_b},
        "engine_identity": {"A": engine_ident_a, "B": engine_ident_b},
        "trade_statistics": {"A": stats_a, "B": stats_b},
        "benchmark_metrics": {
            "net": str(res["A"]["net"]), "round_trips": int(res["A"]["rt"]),
            "win_rate": float(res["A"]["win_rate"]), "max_drawdown": str(res["A"]["max_dd"]),
            "coverage_pct": round(res["A"]["coverage"], 2),
            "high_vol_net": str(res["A"]["hv_net"]), "mixed_net": str(res["A"]["mix_net"]),
        },
        "candidate_metrics": {
            "net": str(res["B"]["net"]), "round_trips": int(res["B"]["rt"]),
            "win_rate": float(res["B"]["win_rate"]), "max_drawdown": str(res["B"]["max_dd"]),
            "coverage_pct": round(res["B"]["coverage"], 2),
            "high_vol_net": str(res["B"]["hv_net"]), "mixed_net": str(res["B"]["mix_net"]),
        },
        "comparison": {
            "A": {k: str(v) for k, v in res["A"].items()},
            "B": {k: str(v) for k, v in res["B"].items()},
            "net_delta_B_minus_A": str(res["B"]["net"] - res["A"]["net"]),
            "rt_delta": stats_b["round_trips"] - stats_a["round_trips"],
            "retention_high_vol": (str(res["B"]["hv_net"] / res["A"]["hv_net"]) if res["A"]["hv_net"] > 0 else None),
            "retention_mixed": (str(res["B"]["mix_net"] / res["A"]["mix_net"]) if res["A"]["mix_net"] > 0 else None),
        },
        "winning_trade_analysis": {
            "A": winner_loser_analysis(rts_a, day_map, vol_bucket_map),
            "B": winner_loser_analysis(rts_b, day_map, vol_bucket_map),
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
        "suppression": {
            "suppressed_count": len(sup_b),
            "direct_benchmark_entries_suppressed": sup_direct,
            "secondary_path_suppressions": sup_secondary,
            "suppressed_records": sup_b,
            "suppressed_by_entry_day_regime": dict(sorted(sup_regime_count.items())),
            "suppressed_by_side": dict(sorted(sup_side_count.items())),
            "suppressed_by_quality_class": dict(sorted(sup_class_count.items())),
        },
        "opportunity_retention": opp,
        "regime_attribution": {
            "A_by_regime": dict(dict(a_reg)),
            "B_by_regime": dict(dict(b_reg)),
            "A_by_vol_bucket": dict(dict(a_vol)),
            "B_by_vol_bucket": dict(dict(b_vol)),
        },
        "volatility_quality_attribution": {
            "classes": VOL_QUALITY_CLASSES,
            "A": vol_quality_pivot(rts_a, cur, prev),
            "B": vol_quality_pivot(rts_b, cur, prev),
            "suppressed_class_breakdown": vol_quality_pivot_from_records(sup_b),
            "per_bar_class_histogram": dict(sorted(class_counts.items())),
        },
        "path_analysis": pa,
        "statefulness": {
            "trace_A": trace_a, "trace_B": trace_b,
            "divergence": div,
            "interpretation": ("shared same-bar-same-side positions are byte-identical including exit "
                               "bar/reason/net; A-only positions are either QUALITY-DENIED entries or "
                               "positions displaced because B already held; B-only positions are path "
                               "additions arising from B being flat while A held."),
        },
        "daily_attribution": {"A": day_attr_a, "B": day_attr_b, "affected_days": day_rows},
        "repeatability": repeat,
        "acceptance": acceptance,
        "classification": classification,
        "classification_rule_reference": "ITERATION_011 brief sections 13/14 (acceptance) and 27 (labels)",
        "determinism": {
            "b_signals_generated_once": True,
            "b_engine_replay_identical": deterministic,
            "note": "B replayed twice through the same engine on the same signal stream; identical results required",
        },
        "leakage_checks": {
            "causal_series_violations": len(violations),
            "no_oos_in_research": True,
            "signal_streams_research_only": True,
            "benchmark_artifact_byte_unchanged": _sha256(ITER9_OUT) == ITER9_RESULT_SHA256,
            "confirmation_uses_only_completed_sessions": len(violations) == 0,
            "entry_change_isolation": entry_change_check,
        },
        "safety_state": {
            "promotion": "NO", "algo_ready": "NO", "algorithm_health": "RED",
            "scope.live_trading": False, "live_gate": "CLOSED",
            "paper_only": True, "human_approval_required": True, "model_0_frozen": True,
        },
        "git_status": {"head_before": _git_head()},
        "trades": {"A": rts_a, "B": rts_b},
    }

    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    out_sha = _sha256(OUT_FILE)
    print(f"iteration_011 written: {OUT_FILE}  sha256={out_sha}")
    print(f"HEAD before: {_git_head()}")

    print("=" * 96)
    print("ITERATION 011 - VOLATILITY-EXPANSION QUALITY CONFIRMATION (pre-OOS A/B, research only)")
    print("=" * 96)
    print(f"research domain: {len(research_bars)} bars / {research_days} days (OOS firewall enforced, no OOS replay)")
    print(f"{'net':18s} {str(block_a['total_pnl']):>22s} {str(block_b['total_pnl']):>22s}")
    print(f"{'gross edge':18s} {str(block_a['gross_close_edge']):>22s} {str(block_b['gross_close_edge']):>22s}")
    print(f"{'round trips':18s} {block_a['round_trips']:>22d} {block_b['round_trips']:>22d}")
    print(f"{'fills':18s} {block_a['fills']:>22d} {block_b['fills']:>22d}")
    print(f"{'win rate':18s} {float(block_a['win_rate_pct']):>21.2f}% {float(block_b['win_rate_pct']):>21.2f}%")
    print(f"{'max drawdown':18s} {str(block_a['max_drawdown']):>22s} {str(block_b['max_drawdown']):>22s}")
    cov_a, cov_b = res["A"]["coverage"], res["B"]["coverage"]
    print(f"{'cost coverage':18s} {cov_a:>21.2f}% {cov_b:>21.2f}%")
    print(f"{'high_vol net':18s} {str(res['A']['hv_net']):>22s} {str(res['B']['hv_net']):>22s}")
    print(f"{'mixed net':18s} {str(res['A']['mix_net']):>22s} {str(res['B']['mix_net']):>22s}")
    print(f"suppressions: {len(sup_b)} (direct benchmark-entry suppressions {sup_direct}, "
          f"secondary path suppressions {sup_secondary})   by quality class: "
          + "; ".join(f"{k}={v}" for k, v in sorted(sup_class_count.items())))
    print(f"identity A: full {ident_a['full_identity_holds']} closed {ident_a['closed_identity_holds']}"
          f"  B: full {ident_b['full_identity_holds']} closed {ident_b['closed_identity_holds']}")
    print(f"path: common {pa['common_same_bar_same_side']} / A-only {pa['a_only']} "
          f"(confirmation_denied {pa['a_only_summary']['confirmation_denied']}, "
          f"displaced {pa['a_only_summary']['displaced_by_b_position']}) / B-only {pa['b_only']} "
          f"-> {pa['stateful_path_change']}")
    print(f"repeatability: h1 improves={repeat['h1_improves']} h2 improves={repeat['h2_improves']}")
    print(f"acceptance ALL: {acceptance['all_acceptance_satisfied']} "
          f"(satisfied {acceptance['satisfied']} failed {acceptance['failed']})")
    print(f"classification: {classification}")
    print("EXPLORATORY ONLY - no parameter change, no OOS use, no promotion, no ALGO READY.")
    print("=" * 96)
    print("STOP")
    return 0


def vol_quality_pivot_from_records(records) -> list[dict]:
    by_class = OrderedDict()
    for rec in records:
        by_class.setdefault(rec["quality_class"], []).append(rec)
    out = []
    for cls, gr in by_class.items():
        nets = [Decimal(r.get("benchmark_position_realized_net", "0"))
                for r in gr if r.get("benchmark_position_realized_net") not in (None, "no_round_trip")]
        out.append({
            "vol_quality_class": cls,
            "count": len(gr),
            "net_of_benchmark_round_trips": str(sum(nets, Decimal("0"))),
            "round_trip_records": len(nets),
        })
    return out


if __name__ == "__main__":
    raise SystemExit(main())