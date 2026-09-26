"""ITERATION 008 - COMPONENT + REGIME ATTRIBUTION OF n3_ensemble (OOS).

Single controlled research iteration.  Primary question: WHICH COMPONENTS AND
REGIMES PRODUCE THE n3_ensemble EDGE, AND WHICH CONDITIONS PRODUCE ITS LOSSES?

This is an ATTRIBUTION experiment, NOT a parameter-optimization experiment.
The frozen candidate (n3_ensemble, Iteration-006 fingerprint:
5fd5d1a2d8966ed4035a93911f93e4300575caaf3475fb742d4b202e4c257a7e;
fast=9, slow=26, slope_window=5, lookback=20, stop_atr_mult=4.0,
max_hold_days=25, warmup=54) is NOT modified in any run here.

Means of attribution:
  1. BASE run  - the recorded Iteration-006 protected-OOS run, re-executed
     byte-identically (authoritative ensemble_signals + unchanged engine) and
     asserted equal to the recorded artifact on the 14-field consistency guard.
  2. Per-trade COMPONENT CONTEXT on the base run - for every round trip the
     trend-level / trend-slope / vol-confirmation strengths and signs at the
     entry bar (decision-time, from the same DailySeries primitives the
     strategy uses), plus regime, vol-bucket, weekday, month half, hold bucket,
     side, exit reason.
  3. Diagnostic COMPONENT ABLATIONS on the same OOS window (same params, same
     engine, same config): structural toggles ONLY (vol-confirmation removed /
     trend removed / slope-removed).  These are DECOMPOSITION diagnostics used
     to attribute marginal edge to each internal condition; they are NOT
     candidates, are NOT tuned, are NOT promotable, and their results are
     attribution-only.
  4. LOSS attribution - a condition pivot showing where and why the base-run
     losses concentrate (exit reason x regime x side).

No parameter is tuned anywhere.  The OOS window is not expanded.  No promotion
under any result; ALGO READY stays NO; no commit, no push.
"""
from __future__ import annotations

import hashlib
import json
import statistics as st
import sys
from collections import OrderedDict
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Callable, Sequence

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
    _trend_state_target,
    _vol_move_confirm,
    cost_stress_block,
    economic_block,
    engine_run,
    ensemble_signals,
)
from fno_ai_paper_trading.research.iteration006_oos_validation import (
    FROZEN_PARAMS,
    FROZEN_WARMUP,
    OOS_START,
    slice_oos,
)
from fno_ai_paper_trading.research.iteration007_robustness_audit import (
    audit_consistency,
)

REPO = Path(__file__).resolve().parents[3]
DATASET = REPO / "datasets" / "upstox_Nifty_50_5m_20220103_20260911.csv"
FINGERPRINT_FILE = REPO / "runs" / "research" / "day_batch" / "iteration_006_n3_fingerprint.json"
ITER6_OUT = REPO / "runs" / "research" / "day_batch" / "iteration_006_protected_oos_n3.json"
OUT_FILE = REPO / "runs" / "research" / "day_batch" / "iteration_008_component_regime_attribution_n3.json"

ITER6_RESULT_SHA256 = "4456db0133bede4ff332ba07c6ae14e8d8c734472e2a4c98bdd00870336ed5b5"
FINGERPRINT_SHA256 = "5fd5d1a2d8966ed4035a93911f93e4300575caaf3475fb742d4b202e4c257a7e"

# Ablation registry (structural toggles only - pre-registered).
ABLATION_DEFS: dict[str, dict] = {
    "base": {"use_trend": True, "use_vol": True, "use_level": True, "use_slope": True,
             "desc": "recorded n3_ensemble (frozen; equals Iteration-006/007)"},
    "vol_off": {"use_trend": True, "use_vol": False, "use_level": True, "use_slope": True,
                "desc": "trend gating only (level+slope); vol-confirmation removed (diagnostic)"},
    "trend_off": {"use_trend": False, "use_vol": True, "use_level": True, "use_slope": True,
                  "desc": "vol-confirmation only; trend state machine removed (diagnostic)"},
    "slope_off": {"use_trend": True, "use_vol": True, "use_level": True, "use_slope": False,
                  "desc": "level(EMA fast/slow cross)+vol; slope/prior-fasts condition removed (diagnostic)"},
}
ABLATION_ORDER = ("base", "vol_off", "trend_off", "slope_off")
HOLD_BUCKETS = (("intraday_or_1d", lambda d: d <= 1), ("2d_5d", lambda d: 2 <= d <= 5),
                ("6d_15d", lambda d: 6 <= d <= 15), ("16d_25d", lambda d: 16 <= d <= 25))


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _dec(v):
    return Decimal(v) if not isinstance(v, Decimal) else Decimal(str(v))


def _rank_pct(value: float, series: Sequence[float]) -> float:
    if not series:
        return None
    return round(sum(1 for s in series if s <= value) / len(series) * 100, 2)


# ---------------------------------------------------------------------------
# frozen-loop variant (identical to iteration005.ensemble_signals EXCEPT the
# meta_target composition; all-options-on must equal ensemble_signals exactly)
# ---------------------------------------------------------------------------


def _trend_variant_signs(ema_fast, ema_slow, prior, use_level: bool, use_slope: bool) -> int:
    level = 0
    if ema_fast is not None and ema_slow is not None:
        level = 1 if ema_fast > ema_slow else (-1 if ema_fast < ema_slow else 0)
    slope = 0
    if ema_fast is not None and prior is not None:
        slope = 1 if ema_fast > prior else (-1 if ema_fast < prior else 0)
    if use_level and use_slope:
        return _trend_state_target(ema_fast, ema_slow, prior)
    if use_level and not use_slope:
        return level
    if use_slope and not use_level:
        return slope
    return 0


def ensemble_variant(
    bars: Sequence, params: EnsembleParams,
    use_trend: bool = True, use_vol: bool = True,
    use_level: bool = True, use_slope: bool = True,
) -> list:
    """Exact copy of iteration005.ensemble_signals with component toggles.

    No parameters are changed; only the presence of the trend state machine,
    the vol-confirmation gate, and the level/slope sub-conditions are toggled.
    With every toggle on, ``meta_target`` composition is identical to the
    authoritative function.
    """
    n = len(bars)
    if n == 0:
        return []
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
        prior_p = k - params.slope_window
        prior = ema_fast[prior_p] if prior_p >= 0 else None
        trend = _trend_variant_signs(ema_fast[k], ema_slow[k], prior, use_level, use_slope) if use_trend else 0
        volmove = _vol_move_confirm(daily, k + 1, params.lookback) if use_vol else 0
        meta_target: int = 0
        if use_trend and use_vol:
            if trend == 1 and volmove == 1:
                meta_target = 1
            elif trend == -1 and volmove == -1:
                meta_target = -1
        elif use_trend:
            meta_target = trend
        elif use_vol:
            meta_target = volmove
        targets[i] = meta_target

    state = 0
    hold_days = 0
    entry_ref: float | None = None
    atr_ref: float | None = None
    entry_day: int | None = None
    signals: list = [None] * n
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
                if target == 1:
                    state = 1; hold_days = 0; entry_day = k + 1
                    atr_ref = daily.avg_range_before(k + 1, params.lookback)
                    entry_ref = _f(bridge.close)
                    signals[i] = _open_signal(bridge, 1, "ensemble confluence up - enter long", meta)
                    continue
                if target == -1:
                    state = -1; hold_days = 0; entry_day = k + 1
                    atr_ref = daily.avg_range_before(k + 1, params.lookback)
                    entry_ref = _f(bridge.close)
                    signals[i] = _open_signal(bridge, -1, "ensemble confluence down - enter short", meta)
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
    return signals


# ---------------------------------------------------------------------------
# per-trade component context (decision-time, frozen primitives)
# ---------------------------------------------------------------------------


def build_entry_context(all_bars, params):
    """Per-bar decision-time component context aligned to the full series."""
    n = len(all_bars)
    daily = DailySeries.build(all_bars)
    ema_fast = daily.day_ema(params.fast)
    ema_slow = daily.day_ema(params.slow)
    ctx: list[dict | None] = [None] * n
    for i in range(n):
        k = daily.bar_day_pos[i]
        if k < 0 or ema_fast[k] is None or ema_slow[k] is None:
            ctx[i] = {"k": k, "trend": 0, "volmove": 0, "level": 0, "slope": 0,
                      "gap_pct": None, "slope_pct": None, "expansion": None, "atr": None}
            continue
        prior_p = k - params.slope_window
        prior = ema_fast[prior_p] if prior_p >= 0 else None
        trend = _trend_state_target(ema_fast[k], ema_slow[k], prior)
        volmove = _vol_move_confirm(daily, k + 1, params.lookback)
        level = 1 if ema_fast[k] > ema_slow[k] else (-1 if ema_fast[k] < ema_slow[k] else 0)
        slope = 1 if prior is not None and ema_fast[k] > prior else (-1 if prior is not None and ema_fast[k] < prior else 0)
        gap_pct = abs(ema_fast[k] - ema_slow[k]) / ema_slow[k] * 100.0
        slope_pct = (abs(ema_fast[k] - prior) / prior * 100.0) if prior else None
        avg = daily.avg_range_before(k + 1, params.lookback)
        expansion = (daily.ranges[k] / avg) if (avg and avg > 0) else None
        ctx[i] = {
            "k": k, "trend": trend, "volmove": volmove, "level": level, "slope": slope,
            "gap_pct": round(gap_pct, 4), "slope_pct": round(slope_pct, 4) if slope_pct is not None else None,
            "expansion": round(expansion, 4) if expansion is not None else None,
            "atr": round(float(avg), 4) if avg else None,
        }
    return ctx


def enrich_trades(rts: Sequence[dict], ctx: Sequence[dict | None], oos_start_idx: int,
                  oos_atr_series: list[float]) -> list[dict]:
    """Attach decision-time component context.

    Journal ``entry_index`` values are offsets into the OOS slice; the context
    array is indexed over the full series, so map ``i -> oos_start_idx + i``.
    """
    enriched = []
    for r in rts:
        full_i = oos_start_idx + r["entry"]["entry_index"]
        c = ctx[full_i] if full_i < len(ctx) else None
        hold_days = (date.fromisoformat(r["exit_day"]) - date.fromisoformat(r["entry_day"])).days
        bucket = next((name for name, cond in HOLD_BUCKETS if cond(hold_days)), "25dplus")
        wd = date.fromisoformat(r["entry_day"]).weekday()
        enriched.append({
            **r,
            "trend_sign": c["trend"] if c else None,
            "vol_sign": c["volmove"] if c else None,
            "level_sign": c["level"] if c else None,
            "slope_sign": c["slope"] if c else None,
            "gap_pct": c["gap_pct"] if c else None,
            "slope_pct": c["slope_pct"] if c else None,
            "expansion": c["expansion"] if c else None,
            "atr": c["atr"] if c else None,
            "atr_pctile_oos": _rank_pct(c["atr"], oos_atr_series) if c and c["atr"] else None,
            "hold_days": hold_days,
            "hold_bucket": bucket,
            "weekday": wd,
            "weekday_name": date.fromisoformat(r["entry_day"]).strftime("%A"),
            "month": r["entry_day"][:7],
            "half": "H1" if r["entry_day"] < "2026-04" else "H2",
        })
    return enriched


def condition_pivot(rows: Sequence[dict], key: str) -> list[dict]:
    buckets = OrderedDict()
    for r in rows:
        buckets.setdefault(r[key], []).append(r)
    out = []
    for name, gr in buckets.items():
        out.append({
            key: name, "count": len(gr),
            "wins": sum(1 for r in gr if r["net"] > 0),
            "losses": sum(1 for r in gr if r["net"] <= 0),
            "net": round(float(sum(r["net"] for r in gr)), 6),
            "gross": round(float(sum(r["gross_close"] for r in gr)), 6),
        })
    return out


def binary_split(rows: Sequence[dict], label: str, lo_key: str, hi_key: str) -> dict:
    lo = [r for r in rows if r[lo_key] is not None and r[lo_key] <= r[hi_key]]
    hi = [r for r in rows if r[hi_key] is not None and r[lo_key] is not None
          and r[lo_key] > r[hi_key]]
    return {
        label + "_lo": {
            "count": len(lo),
            "wins": sum(1 for r in lo if r["net"] > 0),
            "net": round(float(sum(r["net"] for r in lo)), 6),
        },
        label + "_hi": {
            "count": len(hi),
            "wins": sum(1 for r in hi if r["net"] > 0),
            "net": round(float(sum(r["net"] for r in hi)), 6),
        },
    }


# ---------------------------------------------------------------------------
# ablation engine (diagnostics only)
# ---------------------------------------------------------------------------


def run_ablation(name: str, all_bars, oos_sig_bars, config, day_map, vol_bucket_map) -> dict:
    params = EnsembleParams()
    defs = ABLATION_DEFS[name]
    if name == "base":
        signals_full = ensemble_signals(all_bars, params)
    else:
        signals_full = ensemble_variant(all_bars, params, defs["use_trend"], defs["use_vol"],
                                        defs["use_level"], defs["use_slope"])
    _, oos_sig = slice_oos(all_bars, signals_full)
    engine = BacktestEngine()
    journal: list = []
    result = engine_run(oos_sig_bars, config, engine, oos_sig, journal)
    block = economic_block("n3_ensemble:" + name, oos_sig_bars, oos_sig, result,
                           journal, day_map, vol_bucket_map)
    block.update(cost_stress_block(block))
    block["reconcile_all"] = block["reconcile"]["reconcile_all"]
    return {"name": name, "desc": defs["desc"], "block": block,
            "journal": journal, "signals": oos_sig, "result": result}


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main() -> int:
    if not FINGERPRINT_FILE.exists() or not ITER6_OUT.exists():
        raise SystemExit("STOP: Iteration-006 provenance artifacts missing.")
    if _sha256_file(ITER6_OUT) != ITER6_RESULT_SHA256 or _sha256_file(FINGERPRINT_FILE) != FINGERPRINT_SHA256:
        raise SystemExit("STOP: Iteration-006 artifacts byte-drifted from recorded hashes.")
    fp = json.loads(FINGERPRINT_FILE.read_text(encoding="utf-8"))
    if fp["candidate_freeze"]["parameters"] != FROZEN_PARAMS or fp["oos_run_status"] != "COMPLETED":
        raise SystemExit("STOP: frozen candidate provenance unresolved.")

    stored = load_dataset(DATASET)
    all_bars = stored.bars
    params = EnsembleParams()
    if {p: getattr(params, p) for p in ("fast", "slow", "slope_window", "lookback",
                                        "stop_atr_mult", "max_hold_days")} != FROZEN_PARAMS:
        raise SystemExit("STOP: n3_ensemble no longer matches frozen configuration.")
    config = EvaluationConfig().backtest()
    oos_sig_bars, _ = slice_oos(all_bars, [None] * len(all_bars))
    day_map, vol_bucket_map = it5.classed_days(all_bars)

    runs: dict[str, dict] = {}
    for name in ABLATION_ORDER:
        runs[name] = run_ablation(name, all_bars, oos_sig_bars, config, day_map, vol_bucket_map)

    base = runs["base"]
    it6 = json.loads(ITER6_OUT.read_text(encoding="utf-8"))
    ok, mismatches = audit_consistency(base["block"], it6)
    if not ok:
        raise SystemExit("STOP: base run diverged from recorded Iteration-006:\n  "
                         + "\n  ".join(mismatches))

    rts, _, _ = it5.build_round_trips(oos_sig_bars, base["journal"])
    for rt in rts:
        it5.add_flags(rt, oos_sig_bars, day_map, vol_bucket_map)
    daily_all = DailySeries.build(all_bars)
    oos_atr_series = [float(x) for x in (
        daily_all.avg_range_before(k, params.lookback)
        for k in range(params.slow + 1, len(daily_all.closes)))
        if x is not None]
    oos_start_idx = next(i for i, b in enumerate(all_bars)
                         if b.timestamp.date() >= OOS_START)
    ctx = build_entry_context(all_bars, params)
    base_trades = enrich_trades(rts, ctx, oos_start_idx, oos_atr_series)

    def engine_row(b: dict) -> dict:
        res = b["result"]
        tc = res.transaction_costs
        return {
            "basis": "engine_authoritative",
            "round_trips": res.num_trades, "fills": res.orders_filled,
            "wins": res.winning_trades, "losses": res.losing_trades,
            "win_rate_pct": round(float(res.win_rate), 2),
            "net": str(res.total_pnl),
            "gross_profit": str(res.gross_profit),
            "gross_loss": str(res.gross_loss),
            "slippage": str(res.slippage_cost),
            "commission": str(res.total_commission),
            "transaction_costs": str(tc),
            "coverage_pct": round(float(res.gross_profit) / float(tc) * 100, 2) if float(tc) else None,
            "max_drawdown": str(res.max_drawdown),
            "reconciled_journal": bool(b["block"]["reconcile"]["reconcile_all"]),
            "t_gross_journal": b["block"]["t_gross_per_rt"]
            if b["block"]["reconcile"]["reconcile_all"] else None,
            "journal_derived_metrics": b["block"]["reconcile"]["reconcile_all"],
        }

    ablation_rows = []
    for name in ABLATION_ORDER:
        ablation_rows.append({"name": name, "desc": ABLATION_DEFS[name]["desc"],
                              "engine_authoritative": engine_row(runs[name])})

    bnet = _dec(runs["base"]["result"].total_pnl)
    vnet = _dec(runs["vol_off"]["result"].total_pnl)
    tnet = _dec(runs["trend_off"]["result"].total_pnl)
    snet = _dec(runs["slope_off"]["result"].total_pnl)
    marginal = {
        "vol_gate_value_net": str(bnet - vnet),
        "trend_gate_value_net": str(bnet - tnet),
        "slope_gate_value_net": str(bnet - snet),
        "vol_off_net": str(vnet), "trend_off_net": str(tnet), "slope_off_net": str(snet),
    }
    pos = {k: (float(marginal[k]) > 0.0) for k in ("vol_gate_value_net", "trend_gate_value_net", "slope_gate_value_net")}
    if pos["vol_gate_value_net"] and pos["trend_gate_value_net"]:
        primary = "JOINT_TREND_AND_VOL_GATES"
    elif pos["vol_gate_value_net"]:
        primary = "VOL_GATE"
    elif pos["trend_gate_value_net"]:
        primary = "TREND_GATE"
    else:
        primary = "JOINT_INTERACTION_ONLY"

    exit_pivot = condition_pivot(base_trades, "exit_reason")
    regime_pivot = condition_pivot(base_trades, "entry_regime")
    side_pivot = condition_pivot(base_trades, "side")
    vol_label = condition_pivot(base_trades, "entry_vol_bucket")
    hold_pivot = condition_pivot(base_trades, "hold_bucket")
    half_pivot = condition_pivot(base_trades, "half")
    weekday_pivot = condition_pivot(base_trades, "weekday_name")
    sign_bucket = condition_pivot(base_trades, "sign_class") if False else None
    gaps = [t for t in base_trades if t["gap_pct"] is not None]
    gap_med = st.median([t["gap_pct"] for t in gaps]) if gaps else None
    exps = [t for t in base_trades if t["expansion"] is not None]
    exp_med = st.median([t["expansion"] for t in exps]) if exps else None
    strength_split = {
        "gap_median": gap_med,
        "gap": {"low": {"count": sum(1 for t in gaps if t["gap_pct"] <= (gap_med or 9e9)),
                        "net": round(float(sum(t["net"] for t in gaps if t["gap_pct"] <= (gap_med or 9e9))), 6)},
                "high": {"count": sum(1 for t in gaps if t["gap_pct"] > (gap_med or -9e9)),
                         "net": round(float(sum(t["net"] for t in gaps if t["gap_pct"] > (gap_med or -9e9))), 6)}},
        "expansion_median": exp_med,
        "expansion": {"low": {"count": sum(1 for t in exps if t["expansion"] <= (exp_med or 9e9)),
                              "net": round(float(sum(t["net"] for t in exps if t["expansion"] <= (exp_med or 9e9))), 6)},
                      "high": {"count": sum(1 for t in exps if t["expansion"] > (exp_med or -9e9)),
                               "net": round(float(sum(t["net"] for t in exps if t["expansion"] > (exp_med or -9e9))), 6)}},
    }
    losses = sorted([t for t in base_trades if t["net"] <= 0], key=lambda t: t["net"])
    losses_block = {
        "count": len(losses),
        "net": round(float(sum(t["net"] for t in losses)), 6),
        "by_exit_reason": condition_pivot(losses, "exit_reason"),
        "by_regime": condition_pivot(losses, "entry_regime"),
        "by_side": condition_pivot(losses, "side"),
        "by_half": condition_pivot(losses, "half"),
        "rows": [{
            "exit_day": t["exit_day"], "side": t["side"], "net": round(float(t["net"]), 6),
            "reason": t["exit_reason"], "regime": t["entry_regime"],
            "vol_bucket": t["entry_vol_bucket"], "half": t["half"],
        } for t in losses],
    }
    winners_block = {
        "count": sum(1 for t in base_trades if t["net"] > 0),
        "net": round(float(sum(t["net"] for t in base_trades if t["net"] > 0)), 6),
        "by_exit_reason": condition_pivot([t for t in base_trades if t["net"] > 0], "exit_reason"),
        "by_regime": condition_pivot([t for t in base_trades if t["net"] > 0], "entry_regime"),
    }
    sign_consistency = {
        "trades": len(base_trades),
        "trend_sign_matches_side": sum(1 for t in base_trades
                                       if (t["trend_sign"] == 1 and t["side"] == "LONG")
                                       or (t["trend_sign"] == -1 and t["side"] == "SHORT")),
        "vol_sign_matches_side": sum(1 for t in base_trades
                                     if (t["vol_sign"] == 1 and t["side"] == "LONG")
                                     or (t["vol_sign"] == -1 and t["side"] == "SHORT")),
        "level_sign_matches_side": sum(1 for t in base_trades
                                       if (t["level_sign"] == 1 and t["side"] == "LONG")
                                       or (t["level_sign"] == -1 and t["side"] == "SHORT")),
        "slope_sign_matches_side": sum(1 for t in base_trades
                                       if (t["slope_sign"] == 1 and t["side"] == "LONG")
                                       or (t["slope_sign"] == -1 and t["side"] == "SHORT")),
    }

    out = {
        "experiment": "ITERATION_008_COMPONENT_REGIME_ATTRIBUTION_n3_ensemble",
        "attribution_only": True,
        "promotion_notice": "Iteration 008 is ATTRIBUTION research, NOT promotion evidence; no ALGO READY, no production change under any result.",
        "candidate_freeze": {
            "candidate_id": "n3_ensemble",
            "unchanged_since": "ITERATION_006 (fingerprint sha256 5fd5d1a2d8966ed4035a93911f93e4300575caaf3475fb742d4b202e4c257a7e)",
            "parameters": FROZEN_PARAMS,
            "warmup_sessions": FROZEN_WARMUP,
            "component_structure": {
                "trend_level": "EMA fast vs slow",
                "trend_slope": "EMA fast vs slope_window priors (prior-fasts)",
                "vol_confirmation": "prior session range >= avg(lookback) AND same-direction close",
                "confluence": "hard AND-gate of trend(level+slope) and vol-confirmation",
            },
        },
        "domain": {
            "protected_oos_start": OOS_START.isoformat(),
            "oos_window": ["2025-10-06", "2026-09-11"],
            "oos_bars": 17412, "oos_trading_days": 233,
            "not_expanded": True,
        },
        "method": {
            "single_controlled_iteration": True,
            "diagnostics_only": True,
            "ablations_are_not_candidates": True,
            "no_parameter_changed": True,
            "base_consistency_guard": ok,
            "base_sha256": ITER6_RESULT_SHA256,
        },
        "ablation_rows": ablation_rows,
        "base_consistency": {"ok": ok, "mismatches": mismatches},
        "component_marginal_attribution": marginal,
        "primary_edge_source": primary,
        "sign_consistency": sign_consistency,
        "trades_enriched": base_trades,
        "pivots": {
            "by_exit_reason": exit_pivot,
            "by_entry_regime": regime_pivot,
            "by_side": side_pivot,
            "by_vol_bucket": vol_label,
            "by_hold_bucket": hold_pivot,
            "by_half": half_pivot,
            "by_weekday": weekday_pivot,
        },
        "strength_buckets": strength_split,
        "losses": losses_block,
        "winners": winners_block,
        "attribution_answer": {
            "primary_edge_source": primary,
            "marginal_net": marginal,
            "edge_conditions": [r["entry_regime"] for r in regime_pivot if r["net"] > 0],
            "loss_conditions": {
                "regimes_net_negative": [r["entry_regime"] for r in regime_pivot if r["net"] < 0],
                "exit_reasons_net_negative": [r["exit_reason"] for r in exit_pivot if r["net"] < 0],
            },
        },
    }
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    print(f"attribution written: {OUT_FILE}  sha256={_sha256_file(OUT_FILE)}")

    print("=" * 92)
    print("ITERATION 008 - COMPONENT + REGIME ATTRIBUTION OF n3_ensemble (OOS)")
    print("=" * 92)
    print("ablation rows (engine-authoritative totals; same window/params; structural toggles only, diagnostics):")
    for row in ablation_rows:
        b = row["engine_authoritative"]
        print(f"  {row['name']:9s} RT {b['round_trips']:3d}  W/L {b['wins']:2d}/{b['losses']:2d}  "
              f"net {b['net']:>14s}  gp {b['gross_profit']:>12s}  cov {str(b['coverage_pct']):>6s}%  "
              f"tJ {str(b['t_gross_journal']):>5s}  MDD {b['max_drawdown']:>10s}  rec {b['reconciled_journal']}")
    print("component marginal value of each gate (base-net minus gate-off-net):")
    print(f"  vol gate   : {marginal['vol_gate_value_net']}")
    print(f"  trend gate : {marginal['trend_gate_value_net']}")
    print(f"  slope cond : {marginal['slope_gate_value_net']}")
    print(f"primary edge source: {primary}")
    print("-" * 92)
    print(f"pivots (base run, 31 trades):")
    print(f"  exit reason : " + "; ".join(f"{r['exit_reason']}: {r['count']}RT net {r['net']}" for r in exit_pivot))
    print(f"  regime      : " + "; ".join(f"{r['entry_regime']}: {r['count']}RT net {r['net']}" for r in regime_pivot))
    print(f"  side        : " + "; ".join(f"{r['side']}: net {r['net']}" for r in side_pivot))
    print(f"  vol bucket  : " + "; ".join(f"{r['entry_vol_bucket']}: net {r['net']}" for r in vol_label))
    print(f"  half        : " + "; ".join(f"{r['half']}: {r['count']}RT net {r['net']}" for r in half_pivot))
    print(f"  hold bucket : " + "; ".join(f"{r['hold_bucket']}: {r['count']}RT net {r['net']}" for r in hold_pivot))
    print(f"  weekday(+)  : " + "; ".join(f"{r['weekday_name']}: {r['net']}" for r in weekday_pivot if r['net'] > 0))
    print(f"sign/side consistency: trend {sign_consistency['trend_sign_matches_side']}/31  "
          f"vol {sign_consistency['vol_sign_matches_side']}/31  level {sign_consistency['level_sign_matches_side']}/31  "
          f"slope {sign_consistency['slope_sign_matches_side']}/31")
    print(f"losses ({losses_block['count']}, net {losses_block['net']}):")
    print(f"  by regime : " + "; ".join(f"{r['entry_regime']}: {r['count']} net {r['net']}" for r in losses_block["by_regime"]))
    print(f"  by reason : " + "; ".join(f"{r['exit_reason']}: {r['count']} net {r['net']}" for r in losses_block["by_exit_reason"]))
    print("ATTRIBUTION ONLY - diagnostics on the frozen candidate; no parameter change, no promotion, no ALGO READY.")
    print("=" * 92)
    print("STOP")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())