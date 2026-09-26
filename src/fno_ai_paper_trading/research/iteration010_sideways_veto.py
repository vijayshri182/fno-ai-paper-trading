"""ITERATION 010 - SIDEWAYS-VETO ECONOMIC TEST (pre-OOS A/B, research domain only).

Single controlled research experiment.  Question: can the Iteration-009 VOL-led
architecture retain its high-volatility / mixed-regime economic capture while
removing a disproportionate share of the sideways-regime loss pool?

EXPERIMENT (exactly one structural change, no parameter tuning):
  A = frozen Iteration-009 VOL-led benchmark (vol-confirmation-only entries,
      reproduced byte-for-byte and guarded against the persisted
      iteration_009_vol_led.json artifact).
  B = VOL-led + SINGLE SIDEWAYS ENTRY VETO: when the existing causal regime
      classification identifies the market as SIDEWAYS, a new VOL-led ENTRY is
      suppressed.  Everything else (VOL signal, VOL_GATE, volatility/regime
      calculations, exits, stop-loss, sizing, capital, costs, slippage, warmup,
      execution, multiplier, position-state machinery, dataset, config) is
      unchanged.

CAUSALITY: the project's day classifier is ``classify_day`` (Iteration-005
``classed_days``).  Its label for the CURRENT session requires that session's
close/high/low (end-of-day information, NOT available at an intraday entry
timestamp).  The only causal consumption of that existing classifier at the
decision timestamp is the class of the LAST COMPLETED SESSION (built from
strictly-earlier bars only).  The veto therefore consumes
``classify_day(day_stats(prior_session))`` - the SAME classifier, no substitute,
no future bars, no end-of-day information, no future labels, no hindsight.  A
bar-level violation scan asserts every veto input is strictly-before.

FIREWALL: protected OOS window (2025-10-06 .. 2026-09-11) is not used for
selection, tuning, threshold choice, or architecture choice.  Engine replays
run ONLY on the pre-OOS research domain (2022-01-03 .. 2025-10-03,
69,781 bars / 932 days).  Hard assertions abort with STOP on any OOS touch.

Classification (pre-registered): PROMISING RESEARCH RESULT / INCONCLUSIVE /
REJECTED (see ACCEPTANCE).  Even a full pass keeps PROMOTION=NO, ALGO READY=NO.
No commit, no push.
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
from fno_ai_paper_trading.models.enums import Signal
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
    classify_day,
    day_stats,
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

REPO = Path(__file__).resolve().parents[3]
DATASET = REPO / "datasets" / "upstox_Nifty_50_5m_20220103_20260911.csv"
FINGERPRINT_FILE = REPO / "runs" / "research" / "day_batch" / "iteration_006_n3_fingerprint.json"
ITER6_OUT = REPO / "runs" / "research" / "day_batch" / "iteration_006_protected_oos_n3.json"
ITER7_OUT = REPO / "runs" / "research" / "day_batch" / "iteration_007_robustness_audit_n3.json"
ITER8_OUT = REPO / "runs" / "research" / "day_batch" / "iteration_008_component_regime_attribution_n3.json"
ITER9_OUT = REPO / "runs" / "research" / "day_batch" / "iteration_009_vol_led.json"
ITER5_OUT = REPO / "runs" / "research" / "day_batch" / "iteration_005_economic_discovery.json"
OUT_FILE = REPO / "runs" / "research" / "day_batch" / "iteration_010_sideways_veto.json"

DATA_HASH = "6c400b016c1c6d3c99a80db9a8355bc3e195e17c43537bcb93b7e198e85b7f5c"
FINGERPRINT_SHA256 = "5fd5d1a2d8966ed4035a93911f93e4300575caaf3475fb742d4b202e4c257a7e"
ITER6_RESULT_SHA256 = "4456db0133bede4ff332ba07c6ae14e8d8c734472e2a4c98bdd00870336ed5b5"
ITER7_RESULT_SHA256 = "b21171123491945ed8316e57cfffde6c71e29523224121a503b22b5fe779c8ca"
ITER8_RESULT_SHA256 = "52e5ee8e5edc3caca3450d341e9d04416295f5d0993ce0d8b54e2c326cf0eca5"
ITER9_RESULT_SHA256 = "6e9713c30370207fc18ac5c8924ed5742ff3a8f23d639143b82164e6942a0dc2"

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
    "A_sideways_damage": "B reduces the sideways loss pool: (loss_A - loss_B) >= 0.30 x loss_A (loss = -net if net<0 else 0)",
    "B_high_vol_retention": "B high-vol P&L >= 0.80 x A high-vol P&L",
    "C_mixed_retention": "B mixed P&L >= 0.80 x A mixed P&L",
    "D_cost_coverage": "B cost coverage >= A cost coverage - 25pp and B coverage > 100 (%)",
    "E_drawdown": "B maxDD <= A maxDD OR (B net >= A net AND B maxDD <= 1.50 x A maxDD)",
    "F_accounting": "closed + full economic identity hold for A and B",
}
TRIVIAL_GUARD_NOTE = ("a strategy that simply stops trading is not successful; if most of the P&L "
                      "improvement comes from eliminating most trading activity the evidence is weak")


# ---------------------------------------------------------------------------
# causal regime series (existing classifier, strictly decision-time)
# ---------------------------------------------------------------------------


def causal_prior_regime_series(bars) -> tuple[list[str | None], list[str], list[dict]]:
    """Per-bar label of the LAST COMPLETED SESSION under the existing classifier.

    Returns (prior_regime_per_bar, per_session_labels, violation_records).
    ``prior_regime_per_bar[i]`` = ``classify_day(day_stats(session k))`` where
    session k is the last session strictly before bar i (bars[0..i-1] only).
    """
    n = len(bars)
    daily = DailySeries.build(bars)
    session_labels: list[str] = []
    session_ranges: list[tuple[int, int]] = []
    for k in range(len(daily.day_dates)):
        lo, hi = daily.first_idx[k], daily.last_idx[k]
        chunk = bars[lo:hi + 1]
        session_ranges.append((lo, hi))
        session_labels.append(classify_day(day_stats(chunk)))

    prior: list[str | None] = [None] * n
    for i in range(n):
        k = daily.bar_day_pos[i]  # last completed session strictly before bar i
        if k >= 0:
            prior[i] = session_labels[k]

    violations: list[dict] = []
    for i in range(n):
        k = daily.bar_day_pos[i]
        if k >= 0:
            lo, hi = session_ranges[k]
            if not (hi < i):
                violations.append({"bar_index": i, "session": k,
                                   "session_last_index": hi, "uses_future": hi >= i})
    return prior, session_labels, violations


# ---------------------------------------------------------------------------
# frozen-loop vetoed variant (byte-identical to ensemble_variant when veto off)
# ---------------------------------------------------------------------------


def ensemble_variant_sideways_veto(bars, params: EnsembleParams, prior_regime, veto: bool = True):
    """VOL-led candidate (use_trend=False, use_vol=True) with the sideways veto.

    With ``veto=False`` the emitted signal stream is byte-identical to
    ``ensemble_variant(bars, params, use_trend=False, use_vol=True)`` (the
    Iteration-009 benchmark), asserted in main().  With ``veto=True`` the ONLY
    difference is that a first-of-day VOL-led ENTRY is suppressed when
    ``prior_regime[i] == 'sideways'`` (existing causal classifier applied to the
    last completed session).  Exits, stops, holds, sizing and meta are untouched.
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
                suppress = bool(veto) and prior_regime[i] == "sideways" and target != 0
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
                        "causal_prior_session_regime": prior_regime[i],
                        "decision_benchmark": "enter " + ("long" if target == 1 else "short"),
                        "decision_candidate": "flat - sideways veto",
                    })
                    signals[i] = _hold(bridge, "FLAT - sideways veto (research)", meta)
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
# shared helpers
# ---------------------------------------------------------------------------


def _pivot_net(rts: Sequence[dict], key: str) -> dict[str, str]:
    val: OrderedDict = OrderedDict()
    for r in rts:
        val.setdefault(r[key], []).append(r)
    return {k: str(it5.amt_sum(gr, "net")) for k, gr in val.items()}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def signatures_are_actionable(signals, bars):
    if len(signals) != len(bars):
        return False
    for s in signals:
        if s is not None and not isinstance(s.actionable, bool):
            return False
    return True


def _streams_identical(s1, s2) -> bool:
    """Full byte-identity of two signal streams (used for the veto=False arm)."""
    if len(s1) != len(s2):
        return False
    for a, b in zip(s1, s2):
        if a is None or b is None:
            if a is not b:
                return False
            continue
        if (a.signal != b.signal or a.reason != b.reason
                or a.timestamp != b.timestamp or a.meta != b.meta):
            return False
    return True


def _action_maps(signals) -> tuple[dict[int, str], dict[int, str]]:
    """(opens, closes) by bar index from raw emitted signals (reason-based)."""
    opens: dict[int, str] = {}
    closes: dict[int, str] = {}
    for i, s in enumerate(signals):
        if s is None or s.signal not in (Signal.BUY, Signal.SELL):
            continue
        side = "LONG" if s.signal == Signal.BUY else "SHORT"
        reason = s.reason or ""
        if "enter" in reason:
            opens[i] = side
        elif "exit" in reason:
            closes[i] = side
    return opens, closes


def _position_before(opens: dict[int, str], closes: dict[int, str], i: int) -> str | None:
    """Position side (or None=flat) held strictly before bar i (chronological)."""

    def act(b, side, is_close):
        return (b, 0) if is_close else (b, 1 if side == "LONG" else -1)

    events = [act(b, s, False) for b, s in opens.items()]
    events += [act(b, s, True) for b, s in closes.items()]
    events.sort()
    pos = 0
    for b, d in events:
        if b >= i:
            break
        pos = d
    return None if pos == 0 else ("LONG" if pos == 1 else "SHORT")


def _entry_suppressions_only(sig_b, sig_bench, vetoed_records) -> tuple[bool, dict]:
    """The candidate changes ONLY entries via vetoes (plus downstream state).

    Final-stream byte identity is NOT required because downstream stateful
    flattening legitimately changes later HOLD/CLOSE emission.  Isolation
    claim, verified here:

      * DIRECT suppressions = benchmark OPEN signals suppressed by the veto.
        Every direct veto record sits on a benchmark-open bar (same side); the
        benchmark opens missing from the candidate are exactly the direct veto
        slots, except for bars where the candidate was already HOLDING a
        reorder position (displaced benchmark entries).
      * SECONDARY suppressions = veto records on bars where the benchmark was
        NOT opening (benchmark busy holding/closing a previously vetoed entry;
        the candidate still produces an additional flat).
      * Every candidate OPEN signal must be explained: benchmark opened the
        same side at the same bar, OR the benchmark was already holding or
        closing a position at that bar (downstream reorder).  A candidate entry
        in a market where the benchmark sat FLAT with no action would be an
        unexplained crafted entry.
    """
    bench_opens, bench_closes = _action_maps(sig_bench)
    cand_opens, cand_closes = _action_maps(sig_b)

    extra: list[dict] = []
    for i, side in sorted(cand_opens.items()):
        bs = bench_opens.get(i)
        if bs == side:
            continue
        if bs is not None:
            extra.append({"bar": i, "side": side, "benchmark_same_bar_side": bs,
                          "class": "side_conflict"})
            continue
        if bench_closes.get(i) is not None:
            extra.append({"bar": i, "side": side, "class": "reorder_after_vetoed_hold",
                          "benchmark_action": "closing_same_bar"})
            continue
        held = _position_before(bench_opens, bench_closes, i)
        if held is not None:
            extra.append({"bar": i, "side": side, "class": "reorder_after_vetoed_hold",
                          "benchmark_holding": held})
            continue
        extra.append({"bar": i, "side": side, "class": "unexplained_open"})

    direct = [i for i, side in vetoed_records if bench_opens.get(i) == side]
    direct_set = set(direct)
    secondary = [i for i, side in vetoed_records if i not in direct_set]
    missing = {i for i in bench_opens if i not in cand_opens}

    displaced = [i for i in sorted(missing)
                 if i not in direct_set and _position_before(cand_opens, cand_closes, i) is not None]

    no_craft = all(e["class"] not in ("side_conflict", "unexplained_open") for e in extra)
    exact = (direct_set <= missing and missing == direct_set | set(displaced))
    return (no_craft and exact), {
        "candidate_open_isolation": {
            "common_same_bar_same_side": sum(1 for i in cand_opens
                                             if bench_opens.get(i) == cand_opens[i]),
            "reorder_after_vetoed_hold_count": sum(1 for e in extra if e["class"] == "reorder_after_vetoed_hold"),
            "unexplained_or_conflicting": [e for e in extra if e["class"] != "reorder_after_vetoed_hold"],
        },
        "direct_benchmark_entries_suppressed": len(direct_set),
        "secondary_path_suppressions": len(secondary),
        "benchmark_opens_displaced_by_candidate_holding": displaced,
        "suppression_set_exact": exact,
        "missing_not_direct_not_displaced": sorted(missing - direct_set - set(displaced)),
        "direct_not_missing": sorted(direct_set - missing),
        "per_slot_class": {i: ("direct" if i in direct_set else "secondary")
                           for i, _ in vetoed_records},
    }


def _rt_key(rts: Sequence[dict]) -> dict[tuple[int, str], dict]:
    return {(r["entry"]["entry_index"], r["side"]): r for r in rts}


# ---------------------------------------------------------------------------
# path / veto / daily-layer analysis
# ---------------------------------------------------------------------------


def path_analysis(rts_a, rts_b, vetoed, day_map) -> dict:
    a_by = _rt_key(rts_a)
    b_by = _rt_key(rts_b)
    a_keys, b_keys = set(a_by), set(b_by)
    common, a_only, b_only = a_keys & b_keys, a_keys - b_keys, b_keys - a_keys
    veto_set = {v["bar_index"] for v in vetoed}

    a_only_tags = []
    for key in sorted(a_only):
        rt = a_by[key]
        idx = rt["entry"]["entry_index"]
        a_only_tags.append({
            "entry_index": idx, "side": rt["side"], "entry_day": rt["entry_day"],
            "exit_reason": rt["exit_reason"], "net": str(rt["net"]),
            "entry_day_regime": rt.get("entry_regime"),
            "reason_for_divergence": "vetoed" if idx in veto_set else "displaced_by_b_position",
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

    a_net = tag_net(a_only_tags)
    b_net = tag_net(b_only_tags)
    return {
        "common_same_bar_same_side": len(common),
        "a_only": len(a_only_tags),
        "b_only": len(b_only_tags),
        "exit_machinery_unchanged_shared_positions_identical": exit_identical,
        "a_only_tags": a_only_tags,
        "b_only_tags": b_only_tags,
        "a_only_summary": {
            "count": len(a_only_tags),
            "vetoed": sum(1 for t in a_only_tags if t["reason_for_divergence"] == "vetoed"),
            "displaced_by_b_position": sum(1 for t in a_only_tags if t["reason_for_divergence"] == "displaced_by_b_position"),
            "net": a_net,
            "regime_winloss": reg_winloss(a_only_tags),
            "winners": sum(1 for t in a_only_tags if Decimal(t["net"]) > 0),
            "losers": sum(1 for t in a_only_tags if Decimal(t["net"]) <= 0),
            "winners_net": tag_net([t for t in a_only_tags if Decimal(t["net"]) > 0]),
            "losers_net": tag_net([t for t in a_only_tags if Decimal(t["net"]) <= 0]),
        },
        "b_only_summary": {
            "count": len(b_only_tags),
            "net": b_net,
            "regime_winloss": reg_winloss(b_only_tags),
            "winners": sum(1 for t in b_only_tags if Decimal(t["net"]) > 0),
            "losers": sum(1 for t in b_only_tags if Decimal(t["net"]) <= 0),
            "winners_net": tag_net([t for t in b_only_tags if Decimal(t["net"]) > 0]),
            "losers_net": tag_net([t for t in b_only_tags if Decimal(t["net"]) <= 0]),
        },
        "stateful_path_change": ("SUPPRESSION_PLUS_DOWNSTREAM" if b_only else
                                 ("SUPPRESSION_ONLY" if a_only else "NONE")),
    }


def affected_day_rows(rts_a, rts_b, day_map, vetoed) -> dict:
    def by_day(rts):
        out = {}
        for r in rts:
            out.setdefault(r["entry_day"], []).append(r)
        return out

    a_d, b_d = by_day(rts_a), by_day(rts_b)
    all_days = sorted(set(a_d) | set(b_d))
    veto_by_day: Counter = Counter(v["day"] for v in vetoed)
    rows = []
    for d in all_days:
        ag, bg = a_d.get(d, []), b_d.get(d, [])
        a_net = sum(Decimal(r["net"]) for r in ag)
        b_net = sum(Decimal(r["net"]) for r in bg)
        vetoed_here = int(veto_by_day.get(d, 0))
        if len(ag) == len(bg) and a_net == b_net and vetoed_here == 0:
            continue
        rows.append({
            "day": d, "regime": day_map.get(d, "?"),
            "benchmark_trades": len(ag), "candidate_trades": len(bg),
            "benchmark_net": str(a_net), "candidate_net": str(b_net),
            "delta": str(b_net - a_net),
            "entries_suppressed": vetoed_here,
            "exits_changed": 0,
            "state_divergence": "veto-suppressed" if vetoed_here else "path-added/reordered",
        })
    return {
        "affected_day_count": len(rows), "rows": rows,
        "layer_note": ("LAYERS: RAW SIGNAL = per-bar meta 'target' (byte-identical between arms); "
                       "FINAL DECISION = open/close/hold signal emitted; POSITION STATE = engine state "
                       "after the bar; REALIZED TRADE RESULT = round-trip net (post-cost). The veto "
                       "changes only the FINAL DECISION layer for new entries."),
    }


# ---------------------------------------------------------------------------
# acceptance (sections 13 & 14) and classification (section 27)
# ---------------------------------------------------------------------------


def sideways_loss(net) -> Decimal:
    return -net if net < 0 else Decimal("0")


def b_only_nets(trades_a, trades_b) -> list:
    a_keys = {(r["entry"]["entry_index"], r["side"]) for r in trades_a}
    return [r["net"] for r in trades_b
            if (r["entry"]["entry_index"], r["side"]) not in a_keys]


def evaluate_acceptance(res, ident, trades_a, trades_b) -> dict:
    a, b = res["A"], res["B"]
    loss_a = sideways_loss(a["sideways_net"])
    loss_b = sideways_loss(b["sideways_net"])
    reduction = loss_a - loss_b
    crit = {
        "A_sideways_damage": bool(loss_a > 0 and reduction >= Decimal("0.30") * loss_a),
        "B_high_vol_retention": bool(a["hv_net"] > 0 and b["hv_net"] >= Decimal("0.80") * a["hv_net"]),
        "C_mixed_retention": bool(a["mix_net"] > 0 and b["mix_net"] >= Decimal("0.80") * a["mix_net"]),
        "D_cost_coverage": bool(b["coverage"] >= a["coverage"] - 25.0 and b["coverage"] > 100.0),
        "E_drawdown": bool(b["max_dd"] <= a["max_dd"]
                           or (b["net"] >= a["net"] and b["max_dd"] <= Decimal("1.50") * a["max_dd"])),
        "F_accounting": bool(ident["A"]["closed_identity_holds"] and ident["A"]["full_identity_holds"]
                             and ident["B"]["closed_identity_holds"] and ident["B"]["full_identity_holds"]),
    }
    removed = a["rt"] - b["rt"]
    removed_pct = removed / a["rt"] * 100.0 if a["rt"] else 0.0
    b_only_net = sum((Decimal(x) for x in b_only_nets(trades_a, trades_b)), Decimal("0"))
    weak = bool(removed_pct > 50.0 and b_only_net <= 0)
    return {
        "criteria": crit,
        "satisfied": [k for k, v in crit.items() if v],
        "failed": [k for k, v in crit.items() if not v],
        "all_acceptance_satisfied": bool(crit and all(crit.values())),
        "sideways_loss_A": str(loss_a), "sideways_loss_B": str(loss_b),
        "sideways_reduction": str(reduction),
        "sideways_reduction_pct_of_A": round(float(reduction / loss_a * 100), 2) if loss_a > 0 else None,
        "trivial_guard": {
            "trades_removed": removed, "trades_removed_pct": round(removed_pct, 2),
            "candidate_net_improvement": str(b["net"] - a["net"]),
            "net_of_path_additions_b_only": str(b_only_net),
            "weak_evidence": weak,
            "weak_note": TRIVIAL_GUARD_NOTE,
        },
    }


def classify_research(res, acceptance) -> str:
    if res["B"]["net"] <= res["A"]["net"]:
        return "REJECTED"
    if acceptance["all_acceptance_satisfied"]:
        return "PROMISING RESEARCH RESULT"
    return "INCONCLUSIVE"


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
# repeatability (section 17) - chronological halves of the research domain
# ---------------------------------------------------------------------------


def chronological_split(bars, days_in_first_half: int):
    """Chronological head/tail split by research-day ordinal (no tuning)."""
    seen: list[date] = []
    first_day_idx: list[int] = []
    for i, b in enumerate(bars):
        d = b.timestamp.date()
        if not seen or seen[-1] != d:
            seen.append(d)
            first_day_idx.append(i)
    if days_in_first_half >= len(seen):
        raise ValueError("split requires two halves")
    cut = first_day_idx[days_in_first_half]
    return bars[:cut], bars[cut:], seen[:days_in_first_half], seen[days_in_first_half:]


def half_experiment(half_bars, params, config) -> dict:
    """A/B on one chronological half (independently warmed, no tuning)."""
    half_regime, half_labels, violations = causal_prior_regime_series(half_bars)
    dmap, vmap = classed_days(half_bars)
    sig_a, _ = ensemble_variant_sideways_veto(half_bars, params, half_regime, veto=False)
    sig_b, vetoed = ensemble_variant_sideways_veto(half_bars, params, half_regime, veto=True)
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
    loss_a = sideways_loss(_dec(a_reg.get("sideways", "0")))
    loss_b = sideways_loss(_dec(b_reg.get("sideways", "0")))
    return {
        "benchmark": {"net": str(res_a.total_pnl), "round_trips": len(rts_a),
                      "wins": sum(1 for r in rts_a if r["net"] > 0),
                      "losses": sum(1 for r in rts_a if r["net"] <= 0),
                      "by_regime": a_reg, "by_vol_bucket": a_vol,
                      "sideways_net": a_reg.get("sideways", "0"),
                      "hv_net": a_vol.get("high_vol", "0")},
        "candidate": {"net": str(res_b.total_pnl), "round_trips": len(rts_b),
                      "wins": sum(1 for r in rts_b if r["net"] > 0),
                      "losses": sum(1 for r in rts_b if r["net"] <= 0),
                      "by_regime": b_reg, "by_vol_bucket": b_vol,
                      "sideways_net": b_reg.get("sideways", "0"),
                      "hv_net": b_vol.get("high_vol", "0")},
        "vetoed": len(vetoed),
        "causality_violations": len(violations),
        "sideways_loss_reduction_pct": (round(float((loss_a - loss_b) / loss_a * 100), 2)
                                        if loss_a > 0 else None),
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
        "evidence": ("repeatability assessed by whether the veto's sideways-loss reduction and "
                     "vol/mixed retention reproduce in both chronological halves (recorded numbers only; "
                     "no promotion inference)"),
    }


# ---------------------------------------------------------------------------
# entry / terminal (canonical pipeline)
# ---------------------------------------------------------------------------


def main() -> int:
    for p, h in ((FINGERPRINT_FILE, FINGERPRINT_SHA256), (ITER6_OUT, ITER6_RESULT_SHA256),
                 (ITER7_OUT, ITER7_RESULT_SHA256), (ITER8_OUT, ITER8_RESULT_SHA256),
                 (ITER9_OUT, ITER9_RESULT_SHA256)):
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

    prior_regime, session_labels, violations = causal_prior_regime_series(research_bars)
    if violations:
        raise SystemExit("STOP: causal regime series non-causal: " + json.dumps(violations[:5]))
    sideways_days_causal = sum(1 for x in session_labels if x == "sideways")
    sideways_follow_count = sum(1 for k in range(1, len(session_labels))
                                if session_labels[k - 1] == "sideways")

    sig_a, vetoed_a = ensemble_variant_sideways_veto(research_bars, params, prior_regime, veto=False)
    sig_b, vetoed_b = ensemble_variant_sideways_veto(research_bars, params, prior_regime, veto=True)
    if vetoed_a:
        raise SystemExit("STOP: veto=False produced veto records (logic fault).")

    sig_bench = ensemble_variant(research_bars, params, use_trend=False, use_vol=True)
    if not _streams_identical(sig_a, sig_bench):
        raise SystemExit("STOP: veto=False signal stream diverges from Iteration-009 benchmark generator.")
    entry_change_ok, entry_change_check = _entry_suppressions_only(
        sig_b, sig_bench,
        [(v["bar_index"], v["side"]) for v in vetoed_b])
    if not entry_change_ok:
        raise SystemExit("STOP: veto=True changed more than entry decisions (structural leak): "
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
    block_b = economic_block("vol_led+sideways_veto:CANDIDATE_B", research_bars, sig_b, result_b, journal_b, day_map, vol_bucket_map)

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

    a_by = _rt_key(rts_a)
    for v in vetoed_b:
        key = (v["bar_index"], v["side"])
        a_rt = a_by.get(key)
        v["benchmark_position_realized_net"] = str(a_rt["net"]) if a_rt else "no_round_trip"
    veto_regime_count = Counter(day_map.get(v["day"], "?") for v in vetoed_b)
    veto_side_count = Counter(v["side"] for v in vetoed_b)

    ctx = build_entry_context(research_bars, params)

    def trace_rows(rts):
        rows = []
        for r in rts:
            c = ctx[r["entry"]["entry_index"]]
            rows.append({
                "entry_index": r["entry"]["entry_index"], "entry_day": r["entry_day"],
                "side": r["side"],
                "raw_underlying_volmove": c["volmove"] if c else None,
                "vol_gate_state": "ACTIVE_RETAINED",
                "causal_prior_session_regime": prior_regime[r["entry"]["entry_index"]],
                "entry_day_regime_posthoc": r.get("entry_regime"),
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

    res = {
        "A": {
            "net": _dec(block_a["total_pnl"]),
            "rt": block_a["round_trips"], "max_dd": _dec(block_a["max_drawdown"]),
            "coverage": float(_dec(block_a["gross_close_edge"]) / (_dec(block_a["slippage"]) + _dec(block_a["commission"])) * 100),
            "win_rate": float(block_a["win_rate_pct"]),
            "sideways_net": _dec(a_reg.get("sideways", "0")),
            "hv_net": _dec(a_vol.get("high_vol", "0")),
            "mix_net": _dec(a_reg.get("mixed", "0")),
            "gross_loss": _dec(stats_a["gross_loss"]), "gross_profit": _dec(stats_a["gross_profit"]),
        },
        "B": {
            "net": _dec(block_b["total_pnl"]),
            "rt": block_b["round_trips"], "max_dd": _dec(block_b["max_drawdown"]),
            "coverage": float(_dec(block_b["gross_close_edge"]) / (_dec(block_b["slippage"]) + _dec(block_b["commission"])) * 100),
            "win_rate": float(block_b["win_rate_pct"]),
            "sideways_net": _dec(b_reg.get("sideways", "0")),
            "hv_net": _dec(b_vol.get("high_vol", "0")),
            "mix_net": _dec(b_reg.get("mixed", "0")),
            "gross_loss": _dec(stats_b["gross_loss"]), "gross_profit": _dec(stats_b["gross_profit"]),
        },
    }
    acceptance = evaluate_acceptance(res, {"A": ident_a, "B": ident_b}, rts_a, rts_b)
    classification = classify_research(res, acceptance)
    repeat = run_half_repeatability(research_bars, params, config)

    out = {
        "experiment": "ITERATION_010_SIDEWAYS_VETO_ECONOMIC_TEST",
        "objective": ("test whether the Iteration-009 VOL-led architecture retains high-volatility "
                      "and mixed-regime capture while removing a disproportionate share of the "
                      "sideways loss pool; exactly ONE structural change; no parameter tuning; no OOS"),
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
            "id": "VOL-led + SIDEWAYS ENTRY VETO (B)",
            "isolated_change": ("suppress a new VOL-led ENTRY when the existing causal regime "
                                "classification of the last completed session is SIDEWAYS"),
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
            "veto_basis": ("existing Iteration-005 classifier classify_day/day_stats applied to the "
                           "LAST COMPLETED SESSION before the entry decision timestamp"),
            "current_day_label_not_used": True,
            "reason": ("classify_day uses the session's own close/high/low (end-of-day info), so the "
                       "current session label is NOT available at an intraday entry bar"),
            "no_future_bars": True, "no_end_of_day_information": True,
            "no_future_labels": True, "no_hindsight": True,
            "violations": len(violations), "violation_sample": violations[:5],
            "sessions_sideways": sideways_days_causal,
            "sessions_following_sideways": sideways_follow_count,
        },
        "economics": {"A_vol_led": block_a, "B_sideways_veto": block_b},
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
        "veto": {
            "suppressed_count": len(vetoed_b),
            "direct_benchmark_entries_suppressed": veto_direct,
            "secondary_path_suppressions": veto_secondary,
            "suppressed_records": vetoed_b,
            "suppressed_by_entry_day_regime": dict(sorted(veto_regime_count.items())),
            "suppressed_by_side": dict(sorted(veto_side_count.items())),
        },
        "path_analysis": pa,
        "statefulness": {
            "trace_A": trace_a, "trace_B": trace_b,
            "divergence": div,
            "interpretation": ("shared same-bar-same-side positions are byte-identical including exit "
                               "bar/reason/net; A-only positions are either VETOED entries or positions "
                               "displaced because B already held; B-only positions are path additions "
                               "arising from B being flat while A held."),
        },
        "daily_attribution": {"A": day_attr_a, "B": day_attr_b, "affected_days": day_rows},
        "acceptance": acceptance,
        "classification": classification,
        "classification_rule_reference": "ITERATION_010 brief sections 13/14 (acceptance) and 27 (labels)",
        "repeatability": repeat,
        "leakage_checks": {
            "causal_series_violations": len(violations),
            "no_oos_in_research": True,
            "signal_streams_research_only": True,
            "benchmark_artifact_byte_unchanged": _sha256(ITER9_OUT) == ITER9_RESULT_SHA256,
            "veto_uses_only_prior_session": len(violations) == 0,
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
    print(f"iteration_010 written: {OUT_FILE}  sha256={out_sha}")
    print(f"HEAD before: {head_before}")

    print("=" * 96)
    print("ITERATION 010 - SIDEWAYS-VETO ECONOMIC TEST (pre-OOS A/B, research domain only)")
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
    print(f"{'sideways net':18s} {str(a_reg.get('sideways','0')):>22s} {str(b_reg.get('sideways','0')):>22s}")
    print(f"{'high_vol net':18s} {str(a_vol.get('high_vol','0')):>22s} {str(b_vol.get('high_vol','0')):>22s}")
    print(f"{'mixed net':18s} {str(a_reg.get('mixed','0')):>22s} {str(b_reg.get('mixed','0')):>22s}")
    print(f"vetoes applied: {len(vetoed_b)} (direct benchmark-entry suppressions {veto_direct}, "
          f"secondary path suppressions {veto_secondary})   suppressed by day-regime: "
          + "; ".join(f"{k}={v}" for k, v in sorted(veto_regime_count.items())))
    print(f"identity A: full {ident_a['full_identity_holds']} closed {ident_a['closed_identity_holds']}"
          f"  B: full {ident_b['full_identity_holds']} closed {ident_b['closed_identity_holds']}")
    print(f"path: common {pa['common_same_bar_same_side']} / A-only {pa['a_only']} "
          f"(vetoed {pa['a_only_summary']['vetoed']}, displaced {pa['a_only_summary']['displaced_by_b_position']}) "
          f"/ B-only {pa['b_only']}  -> {pa['stateful_path_change']}")
    print(f"acceptance ALL: {acceptance['all_acceptance_satisfied']} "
          f"(satisfied {acceptance['satisfied']} failed {acceptance['failed']})")
    print(f"classification: {classification}")
    print("EXPLORATORY ONLY - no parameter change, no OOS use, no promotion, no ALGO READY.")
    print("=" * 96)
    print("STOP")
    return 0


def _git_head() -> str:
    import subprocess
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                              cwd=REPO, capture_output=True, text=True).stdout.strip()
    except Exception:
        return "unknown"


if __name__ == "__main__":
    raise SystemExit(main())