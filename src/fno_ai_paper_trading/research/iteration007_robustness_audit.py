"""ITERATION 007 - ROBUSTNESS AUDIT OF THE n3_ensemble PROTECTED-OOS RESULT.

Single controlled research iteration.  The Iteration-006 protected-OOS result
(net +1,710.004256155 / +1.71% over 31 RTs on 2025-10-06..2026-09-11) is
dissected to decide whether it is BROADLY DISTRIBUTED and economically robust or
whether it is CONCENTRATED in a small number of trades / dates / regimes /
execution assumptions.

Discipline (Iter-007 brief):
  * ROBUSTNESS AUDIT only.  n3_ensemble is NOT optimized, NOT re-parameterised,
    NOT replaced, NOT re-searched.  The candidate stays frozen EXACTLY as the
    Iteration-006 fingerprint (sha256 5fd5d1a2d8966ed4035a93911f93e4300575c
    aaf3475fb742d4b202e4c257a7e): fast=9, slow=26, slope_window=5, lookback=20,
    stop_atr_mult=4.0, max_hold_days=25, warmup=54.
  * The OOS domain is NOT expanded: 2025-10-06..2026-09-11, 17,412 bars, 233
    trading days.
  * The engine runs EXACTLY ONE time, with the byte-identical pipeline used in
    Iteration-006 (full-series causal signal stream sliced at OOS_START; one
    engine replay starting flat).  The reconstructed economics are asserted
    equal to the recorded iteration_006_protected_oos_n3.json; any mismatch
    STOPs (an audit of a different run would be invalid).
  * EXECUTION-ASSUMPTION SENSITIVITY is computed ONLY on the recorded fills
    (algebraic cost scenarios).  No engine variant is executed; entry-exit time
    shifts / resized lots / different stops are out of scope and documented as
    excluded.
  * Verdict + thresholds are pre-registered as module constants (rationale in
    the docstrings) and are applied verbatim to the recorded outcomes.
  * No promotion under any verdict; ALGO READY stays NO; no commit, no push.
"""
from __future__ import annotations

import hashlib
import json
import statistics as st
import sys
from collections import OrderedDict
from decimal import Decimal
from pathlib import Path
from typing import Sequence

from fno_ai_paper_trading.backtest.engine import BacktestEngine
from fno_ai_paper_trading.data.dataset_store import load_dataset
from fno_ai_paper_trading.evaluation.records import EvaluationConfig
from fno_ai_paper_trading.research import iteration005_discovery as it5
from fno_ai_paper_trading.research.iteration005_discovery import (
    EnsembleParams,
    cost_stress_block,
    economic_block,
    engine_run,
    ensemble_signals,
)
from fno_ai_paper_trading.research.iteration006_oos_validation import (
    FROZEN_PARAMS,
    FROZEN_WARMUP,
    INITIAL_CAPITAL,
    OOS_START,
    active_trading_days,
    mandatory_metrics as it6_mandatory_metrics,
    oos_verdict,
    slice_oos,
)

REPO = Path(__file__).resolve().parents[3]
DATASET = REPO / "datasets" / "upstox_Nifty_50_5m_20220103_20260911.csv"
FINGERPRINT_FILE = REPO / "runs" / "research" / "day_batch" / "iteration_006_n3_fingerprint.json"
ITER6_OUT = REPO / "runs" / "research" / "day_batch" / "iteration_006_protected_oos_n3.json"
OUT_FILE = REPO / "runs" / "research" / "day_batch" / "iteration_007_robustness_audit_n3.json"

ITER6_RESULT_SHA256 = "4456db0133bede4ff332ba07c6ae14e8d8c734472e2a4c98bdd00870336ed5b5"
FINGERPRINT_SHA256 = "5fd5d1a2d8966ed4035a93911f93e4300575caaf3475fb742d4b202e4c257a7e"

# ---------------------------------------------------------------------------
# pre-registered thresholds (robustness audit - door not moved later).
# Rationale: standard concentration/coverage conventions. A 75% top-3 share is
# the classic "moderate concentration" boundary; a zero-cost gross t >= 1.96 is
# the conventional significance mark; a >= 55% hit rate keeps the margin out of
# pure-streak territory; >= 5 bps/fill of absorbable extra slippage is a two
# sigma tolerance of normal NIFTY 5m effective spread + execution slippage.
# ---------------------------------------------------------------------------
THRESHOLDS = {
    "R1_top3_share_net_lt": 0.75,
    "R2_top3_days_share_lt": 0.75,
    "R4_top_regime_share_net_lt": 0.90,
    "R5_slip2x_still_positive": True,
    "R5_min_break_even_extra_slip_bps_per_fill": 5.0,
    "R6_min_t_gross": 1.96,
    "R6_min_win_rate_pct": 55.0,
}

# Execution-sensitivity scenario grid (recorded fills only).
SLIPPAGE_MULTIPLIERS = (0.0, 0.5, 1.0, 2.0, 3.0)
COMMISSION_MULTIPLIERS = (0.5, 1.0, 2.0)
EXTRA_SLIP_BPS_PER_FILL = (0, 1, 5, 10, 25)


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _d(v):
    return Decimal(v) if not isinstance(v, Decimal) else v


# ---------------------------------------------------------------------------
# consistency guard
# ---------------------------------------------------------------------------


def audit_consistency(econ: dict, it6: dict) -> tuple[bool, list[str]]:
    """Assert the reconstructed economics equal the recorded Iter-006 values."""
    mandatory = it6["mandatory_metrics"]
    checks = [
        ("round_trips", econ["round_trips"], it6["economics"]["round_trips"]),
        ("fills", econ["fills"], it6["economics"]["fills"]),
        ("wins", econ["wins"], it6["economics"]["wins"]),
        ("losses", econ["losses"], it6["economics"]["losses"]),
        ("total_pnl", _d(econ["total_pnl"]), _d(it6["economics"]["total_pnl"])),
        ("gross_close_edge", _d(econ["gross_close_edge"]), _d(it6["economics"]["gross_close_edge"])),
        ("slippage", _d(econ["slippage"]), _d(it6["economics"]["slippage"])),
        ("commission", _d(econ["commission"]), _d(it6["economics"]["commission"])),
        ("m_drawdown", _d(econ["max_drawdown"]), _d(it6["economics"]["max_drawdown"])),
        ("m_net", _d(mandatory["net_pnl"]), _d(it6["mandatory_metrics"]["net_pnl"])),
        ("m_equity", _d(mandatory["ending_equity"]), _d(it6["mandatory_metrics"]["ending_equity"])),
        ("t_gross", econ["t_gross_per_rt"], it6["economics"]["t_gross_per_rt"]),
        ("carry_share", econ["carry_share_pct"], it6["economics"]["carry_share_pct"]),
        ("reconcile_all", econ["reconcile"]["reconcile_all"],
         it6["economics"]["reconcile"]["reconcile_all"]),
    ]
    mismatches = [f"{k}: reconstructed={a!r} recorded={b!r}" for k, a, b in checks if a != b]
    return (not mismatches), mismatches


# ---------------------------------------------------------------------------
# attribution frames
# ---------------------------------------------------------------------------


def trade_frame(rts: Sequence[dict]) -> list[dict]:
    """Per-round-trip audit records (net/gross already flagged)."""
    return [
        {
            "entry_day": r["entry_day"],
            "exit_day": r["exit_day"],
            "entry_regime": r["entry_regime"],
            "exit_regime": r["exit_regime"],
            "entry_vol_bucket": r["entry_vol_bucket"],
            "side": r["side"],
            "net": float(r["net"]),
            "gross": float(r["gross_close"]),
            "slippage": float(r["slippage"]),
            "commission": float(r["commission"]),
            "hold_minutes": int(r["hold_minutes"]),
            "mfe": float(r["mfe"]),
            "mae": float(r["mae"]),
            "giveback": float(r["giveback"]),
            "win": bool(r["net"] > 0),
            "reason": r["exit_reason"],
        }
        for r in rts
    ]


def _top_shares(values: Sequence[float], k: int) -> dict:
    total = float(sum(values))
    if not values or total == 0:
        return {"total": total, "count": len(values), "top_share": None,
                "ex_top_share_amount": None}
    ordered = sorted(values, reverse=True)
    top_sum = float(sum(ordered[:k]))
    return {
        "total": round(total, 6),
        "count": len(values),
        "top": [round(v, 6) for v in ordered[:k]],
        "top_share": round(top_sum / total, 6),
        "ex_top_share_amount": round(total - top_sum, 6),
    }


def hhi(values: Sequence[float]) -> float | None:
    """Herfindahl index on |value| weights (0 = flat, 1 = single-concentrated)."""
    total = sum(abs(v) for v in values)
    if total <= 0:
        return None
    return round(sum((abs(v) / total) ** 2 for v in values), 6)


def jackknife_total(values: Sequence[float]) -> dict:
    total = float(sum(values))
    if not values:
        return {"total": None, "drop_best": None, "drop_worst": None,
                "min_impact": None, "max_impact": None}
    outs = []
    for i in range(len(values)):
        outs.append(total - values[i])
    return {
        "total": round(total, 6),
        "drop_best": round(total - max(values), 6),
        "drop_worst": round(total - min(values), 6),
        "min_impact": round(min(outs), 6),
        "max_impact": round(max(outs), 6),
    }


def group_stats(rows: Sequence[dict], key: str, by: str) -> list[dict]:
    buckets = OrderedDict()
    for r in rows:
        buckets.setdefault(r[key], []).append(r)
    out = []
    for name, gr in buckets.items():
        net = float(sum(r["net"] for r in gr))
        gross = float(sum(r["gross"] for r in gr))
        wins = sum(1 for r in gr if r["win"])
        out.append({
            by: name, "count": len(gr), "wins": wins,
            "net": round(net, 6), "gross": round(gross, 6),
            "share_net_pct": round(net / sum(float(r["net"]) for r in rows) * 100, 2)
            if sum(float(r["net"]) for r in rows) else None,
        })
    return out


def month_concentration(rows: Sequence[dict], split_day: str) -> dict:
    """Daily net grouped by exit month + half-window split.

    ``split_day`` is a YEAR-MONTH key ("2026-04"): months with an exit key
    strictly < it compose the first half (H1).  (Comparing against a full date
    would mis-bucket the boundary month.)
    """
    months = OrderedDict()
    for r in rows:
        months.setdefault(r["exit_day"][:7], 0.0)
        months[r["exit_day"][:7]] += r["net"]
    total = float(sum(r["net"] for r in rows))
    if not months:
        return {"months": [], "positive_months": 0, "month_count": 0}
    ordered = sorted(months.items(), key=lambda kv: kv[0])
    nets = [v for _, v in ordered]
    best = max(nets)
    best_key = max(ordered, key=lambda kv: kv[1])[0]
    worst = min(nets)
    worst_key = min(ordered, key=lambda kv: kv[1])[0]
    positive = sum(1 for v in nets if v > 0)
    h1 = sum(v for k, v in ordered if k < split_day)
    h2 = sum(v for k, v in ordered if k >= split_day)
    return {
        "months": [{m: round(v, 6)} for m, v in ordered],
        "month_count": len(nets),
        "positive_months": positive,
        "best_month": best_key, "best_month_net": round(best, 6),
        "worst_month": worst_key, "worst_month_net": round(worst, 6),
        "net_ex_best_month": round(total - best, 6),
        "top3_months_share": round(
            sum(sorted(nets, reverse=True)[:3]) / total, 6) if total else None,
        "h1_net": round(h1, 6), "h2_net": round(h2, 6),
        "h1_count": sum(1 for r in rows if r["exit_day"] < split_day),
        "h2_count": sum(1 for r in rows if r["exit_day"] >= split_day),
    }


def day_concentration(rows: Sequence[dict]) -> dict:
    days = OrderedDict()
    for r in rows:
        days.setdefault(r["exit_day"], 0.0)
        days[r["exit_day"]] += r["net"]
    total = float(sum(days.values()))
    ordered = sorted(days.items(), key=lambda kv: kv[1], reverse=True)
    best = ordered[0][1] if ordered else 0.0
    best_day = ordered[0][0] if ordered else None
    worst = ordered[-1][1] if ordered else 0.0
    worst_day = ordered[-1][0] if ordered else None
    top3 = sum(v for _, v in ordered[:3])
    positive = sum(1 for _, v in ordered if v > 0)
    return {
        "relative_days": len(days),
        "active_positive_days": positive,
        "best_day": best_day, "best_day_net": round(best, 6),
        "worst_day": worst_day, "worst_day_net": round(worst, 6),
        "net_ex_best_day": round(total - best, 6),
        "top3_days_share": round(top3 / total, 6) if total else None,
        "days_share_net": round(positive / len(days) * 100, 2) if days else None,
    }


# ---------------------------------------------------------------------------
# execution-sensitivity (recorded fills only, algebraic)
# ---------------------------------------------------------------------------


def execution_stress(frames: Sequence[dict], avg_fill_price: float) -> dict:
    gross = float(sum(f["gross"] for f in frames))
    slip = float(sum(f["slippage"] for f in frames))
    comm = float(sum(f["commission"] for f in frames))
    fills = 2 * len(frames)
    net_1x = gross - slip - comm
    scenarios = {}
    for sm in SLIPPAGE_MULTIPLIERS:
        for cm in COMMISSION_MULTIPLIERS:
            if sm == 1.0 and cm == 1.0:
                continue
            n = gross - slip * sm - comm * cm
            scenarios[f"slipx{sm:_}commx{cm:_}"] = round(n, 6)
    net_at_slip2x = gross - slip * 2.0 - comm
    breakeven_bps = (net_1x / (0.0001 * avg_fill_price * fills)) if avg_fill_price and fills else None
    extra = {}
    for bps in EXTRA_SLIP_BPS_PER_FILL:
        if bps == 0:
            continue
        extra[f"extra_{bps}_bps_per_fill"] = round(net_1x - (0.0001 * bps * avg_fill_price * fills), 6)
    return {
        "fills": fills,
        "avg_fill_price": round(avg_fill_price, 4),
        "gross_edge": round(gross, 6),
        "slippage_1x": round(slip, 6),
        "commission_1x": round(comm, 6),
        "net_1x": round(net_1x, 6),
        "scenarios": scenarios,
        "net_at_slip2x": round(net_at_slip2x, 6),
        "extra_slippage_bps_per_fill": extra,
        "break_even_extra_slippage_bps_per_fill": round(breakeven_bps, 4) if breakeven_bps else None,
    }


def avg_fill_price(journal: Sequence[dict]) -> float:
    prices: list[float] = []
    for row in journal:
        price = row.get("fill_price")
        if price is None:
            price = row.get("stop_price")
        if price is not None:
            prices.append(float(Decimal(str(price))))
    return st.mean(prices) if prices else 0.0


# ---------------------------------------------------------------------------
# pre-registered robustness verdict (pure)
# ---------------------------------------------------------------------------


def robustness_verdict(stats: dict) -> dict:
    t = THRESHOLDS
    r1 = (stats["trade_concentrations"]["top3_share_net"] is not None
          and stats["trade_concentrations"]["top3_share_net"] < t["R1_top3_share_net_lt"]
          and stats["trade_concentrations"]["net_ex_top3"] > 0)
    r2 = (stats["day_concentrations"]["top3_days_share"] is not None
          and stats["day_concentrations"]["top3_days_share"] < t["R2_top3_days_share_lt"]
          and stats["day_concentrations"]["net_ex_best_day"] > 0
          and stats["months"]["net_ex_best_month"] > 0)
    r3 = stats["sides"]["min_side_net"] > 0
    r4 = (stats["regimes"]["positive_regime_count"] >= 2
          and stats["vol_buckets"]["positive_bucket_count"] >= 2
          and (stats["regimes"]["top_regime_share_net"] is not None
               and stats["regimes"]["top_regime_share_net"] < t["R4_top_regime_share_net_lt"]))
    r5 = (stats["execution"]["net_at_slip2x"] > 0
          and stats["execution"]["break_even_extra_slippage_bps_per_fill"] is not None
          and stats["execution"]["break_even_extra_slippage_bps_per_fill"]
          >= t["R5_min_break_even_extra_slip_bps_per_fill"])
    r6 = (stats["statistics"]["t_gross_per_rt"] is not None
          and stats["statistics"]["t_gross_per_rt"] >= t["R6_min_t_gross"]
          and stats["statistics"]["win_rate_pct"] >= t["R6_min_win_rate_pct"])

    failing = [name for name, ok in (("R1_trades", r1), ("R2_dates", r2),
                                     ("R3_sides", r3), ("R4_regimes", r4),
                                     ("R5_execution", r5), ("R6_statistics", r6)) if not ok]
    if not failing:
        code = "ROBUST_DISTRIBUTED"
        reason = ("R1-R6 all hold - the +net is broadly distributed across trades, "
                  "dates, sides, regimes and concentration is within tolerance "
                  "under realistic execution stress")
    elif len(failing) == 1:
        code = {
            "R1_trades": "CONCENTRATED_TRADES",
            "R2_dates": "CONCENTRATED_DATES",
            "R3_sides": "CONCENTRATED_SIDE",
            "R4_regimes": "CONCENTRATED_REGIME",
            "R5_execution": "EXECUTION_FRAGILE",
            "R6_statistics": "STATISTICALLY_WEAK",
        }[failing[0]]
        reason = (
            "robustness check exceeded pre-registered tolerance"
            if failing[0] != "R5_execution" else
            "positive only inside a narrow execution band (recorded-fill cost stress)")
    else:
        code = "MIXED_CONCENTRATION"
        reason = f"multiple pre-registered robustness checks failed: {', '.join(failing)}"

    returning = {
        "code": code,
        "reason": reason,
        "criteria": {
            "R1_trades": r1, "R2_dates": r2, "R3_sides": r3,
            "R4_regimes": r4, "R5_execution": r5, "R6_statistics": r6,
            "failing": failing,
        },
        "thresholds": t,
    }
    return returning


# ---------------------------------------------------------------------------
# the single controlled audit run
# ---------------------------------------------------------------------------


def run_audit() -> tuple[list[dict], dict, dict, dict]:
    if not FINGERPRINT_FILE.exists():
        raise SystemExit("STOP: iteration_006_n3_fingerprint.json missing - cannot audit.")
    if not ITER6_OUT.exists():
        raise SystemExit("STOP: iteration_006_protected_oos_n3.json missing - cannot audit.")
    fp = json.loads(FINGERPRINT_FILE.read_text(encoding="utf-8"))
    if fp.get("oos_run_status") != "COMPLETED":
        raise SystemExit("STOP: fingerprint not COMPLETED - candidate provenance unresolved.")
    if fp["candidate_freeze"]["parameters"] != FROZEN_PARAMS:
        raise SystemExit("STOP: fingerprint params drifted from frozen literals.")
    if FINGERPRINT_FILE.exists() and _sha256_file(FINGERPRINT_FILE) != FINGERPRINT_SHA256:
        raise SystemExit("STOP: iteration_006 fingerprint byte-drifted from the recorded value.")
    if _sha256_file(ITER6_OUT) != ITER6_RESULT_SHA256:
        raise SystemExit(
            "STOP: iteration_006_protected_oos_n3.json byte-drifted from the recorded run "
            f"({_sha256_file(ITER6_OUT)[:12]}.. != {ITER6_RESULT_SHA256[:12]}..).")

    stored = load_dataset(DATASET)
    all_bars = stored.bars
    params = EnsembleParams()
    if {p: getattr(params, p) for p in ("fast", "slow", "slope_window", "lookback",
                                        "stop_atr_mult", "max_hold_days")} != FROZEN_PARAMS:
        raise SystemExit("STOP: n3_ensemble no longer matches the frozen configuration.")

    all_signals = ensemble_signals(all_bars, params)
    oos_sig_bars, oos_sig = slice_oos(all_bars, all_signals)
    config = EvaluationConfig().backtest()
    engine = BacktestEngine()
    journal: list = []
    result = engine_run(oos_sig_bars, config, engine, oos_sig, journal)

    day_map, vol_bucket_map = it5.classed_days(all_bars)
    full_block = economic_block("n3_ensemble", oos_sig_bars, oos_sig, result,
                                journal, day_map, vol_bucket_map)
    full_block.update(cost_stress_block(full_block))
    full_block["reconcile_all"] = full_block["reconcile"]["reconcile_all"]
    rts, _, _ = it5.build_round_trips(oos_sig_bars, journal)
    for rt in rts:
        it5.add_flags(rt, oos_sig_bars, day_map, vol_bucket_map)
    metrics = it6_mandatory_metrics(rts, result, len({b.timestamp.date() for b in oos_sig_bars}),
                                    active_trading_days(journal))

    it6 = json.loads(ITER6_OUT.read_text(encoding="utf-8"))
    ok, mismatches = audit_consistency(full_block, it6)
    if not ok:
        raise SystemExit(
            "STOP: audit reconstruction diverges from the recorded Iteration-006 run:\n  "
            + "\n  ".join(mismatches))

    frames = trade_frame(rts)
    flows_net = [f["net"] for f in frames]
    flows_gross = [f["gross"] for f in frames]
    net_total = float(sum(flows_net))

    trade_tops = {k: _top_shares(flows_net, k) for k in (1, 3, 5)}
    gross_tops = {k: _top_shares(flows_gross, k) for k in (1, 3, 5)}
    days_net = day_concentration(frames)
    months = month_concentration(frames, "2026-04")
    regimes = group_stats(frames, "entry_regime", "regime")
    vol_buckets = group_stats(frames, "entry_vol_bucket", "vol_bucket")
    sides = group_stats(frames, "side", "side")
    reg_nets = [float(g["net"]) for g in regimes]
    top_regime_share = (max(reg_nets) / net_total) if net_total else None
    vol_nets = [float(g["net"]) for g in vol_buckets]
    sides_nets = [float(g["net"]) for g in sides]

    stats = {
        "net_total": round(net_total, 6),
        "trade_concentrations": {
            "top1": trade_tops[1], "top3": trade_tops[3], "top5": trade_tops[5],
            "top3_share_net": trade_tops[3]["top_share"],
            "net_ex_top3": trade_tops[3]["ex_top_share_amount"],
            "gross_top1": gross_tops[1], "gross_top3": gross_tops[3],
            "hhi_net": hhi(flows_net), "hhi_gross": hhi(flows_gross),
            "jackknife": jackknife_total(flows_net),
            "loses_net_sum": round(float(sum(min(0.0, v) for v in flows_net)), 6),
            "wins_net_sum": round(float(sum(max(0.0, v) for v in flows_net)), 6),
        },
        "day_concentrations": days_net,
        "months": months,
        "regimes": {
            "rows": regimes, "positive_regime_count": sum(1 for g in regimes if g["net"] > 0),
            "top_regime_share_net": round(top_regime_share, 6) if top_regime_share is not None else None,
        },
        "vol_buckets": {
            "rows": vol_buckets, "positive_bucket_count": sum(1 for g in vol_buckets if g["net"] > 0),
        },
        "sides": {
            "rows": sides, "min_side_net": round(min(sides_nets), 6) if sides_nets else None,
        },
        "execution": execution_stress(frames, avg_fill_price(journal)),
        "statistics": {
            "round_trips": len(frames),
            "win_rate_pct": metrics["win_rate_pct"],
            "t_gross_per_rt": full_block["t_gross_per_rt"],
            "t_net_per_rt": full_block["t_net_per_rt"],
            "net_per_rt_mean": metrics["expectancy_per_rt"],
        },
    }
    verdict = robustness_verdict(stats)
    return frames, stats, verdict, it6


def main() -> int:
    frames, stats, verdict, it6 = run_audit()
    out = {
        "experiment": "ITERATION_007_ROBUSTNESS_AUDIT_n3_ensemble",
        "audit_only": True,
        "promotion_notice": "Iteration 007 is a ROBUSTNESS AUDIT, NOT promotion evidence; no ALGO READY, no production change under any verdict.",
        "candidate_freeze": {
            "candidate_id": "n3_ensemble",
            "unchanged_since": "ITERATION_006 (fingerprint sha256 5fd5d1a2d8966ed4035a93911f93e4300575caaf3475fb742d4b202e4c257a7e)",
            "parameters": FROZEN_PARAMS,
            "warmup_sessions": FROZEN_WARMUP,
            "audited_result_sha256": ITER6_RESULT_SHA256,
        },
        "domain": {
            "protected_oos_start": OOS_START.isoformat(),
            "oos_window": it6["domain"]["oos_window"],
            "oos_bars": it6["domain"]["oos_bars"],
            "oos_trading_days": it6["domain"]["oos_trading_days"],
            "dataset_identity": {"name": "upstox_Nifty_50_5m_20220103_20260911.csv",
                                 "data_hash_sha256": "6c400b016c1c6d3c99a80db9a8355bc3e195e17c43537bcb93b7e198e85b7f5c"},
            "not_expanded": True,
        },
        "method": {
            "single_controlled_run": True,
            "pipeline_identical_to_iteration_006": True,
            "consistency_guard": "economics + mandatory metrics asserted equal to recorded iteration_006_protected_oos_n3.json before analysis",
            "execution_sensitivity": "algebraic scenarios on RECORDED fills only; no engine variant run",
            "excluded": ["entry/exit time shifts", "lot-size changes", "alternate stops",
                         "parameter changes", "fresh OOS replication (later iterations)"],
        },
        "iter6_guard": {"byte_sha256_matches": True, "economics_reconstructed_identically": True},
        "attribution": {"round_trips": len(frames), "trade_table": frames},
        "robustness": stats,
        "verdict": verdict,
    }
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    print(f"robustness audit written: {OUT_FILE}  sha256={_sha256_file(OUT_FILE)}")

    # ---------------- terminal summary ----------------
    tc = stats["trade_concentrations"]
    dc = stats["day_concentrations"]
    mo = stats["months"]
    rg = stats["regimes"]
    vb = stats["vol_buckets"]
    sd = stats["sides"]
    ex = stats["execution"]
    stt = stats["statistics"]
    print("=" * 92)
    print("ITERATION 007 - ROBUSTNESS AUDIT OF n3_ensemble PROTECTED-OOS RESULT")
    print("=" * 92)
    print(f"audited run        : Iter-006 protected OOS 2025-10-06..2026-09-11 (17,412 bars / 233 days)")
    print(f"frozen candidate   : n3_ensemble {FROZEN_PARAMS} warmup={FROZEN_WARMUP}  (fingerprint verified)")
    print("-" * 92)
    print(f"net total          : {stats['net_total']}")
    print(f"trade top3 share   : {tc['top3_share_net']}  net ex-top3: {tc['net_ex_top3']}  "
          f"hhi(net): {tc['hhi_net']}  hhi(gross): {tc['hhi_gross']}")
    print(f"jackknife          : drop-best {tc['jackknife']['drop_best']} | drop-worst {tc['jackknife']['drop_worst']}")
    print(f"day top3 share     : {dc['top3_days_share']}  best-day net: {dc['best_day_net']} ({dc['best_day']})  "
          f"net ex-best-day: {dc['net_ex_best_day']}  positive days: {dc['active_positive_days']}/{dc['relative_days']}")
    print(f"months             : {mo['month_count']} (positive {mo['positive_months']})  best {mo['best_month']} "
          f"({mo['best_month_net']})  worst {mo['worst_month']} ({mo['worst_month_net']})  ex-best-month {mo['net_ex_best_month']}")
    print(f"half split         : H1={mo['h1_net']} ({mo['h1_count']} RT)  H2={mo['h2_net']} ({mo['h2_count']} RT)")
    print(f"regimes (+)        : {rg['positive_regime_count']}/{len(rg['rows'])}  top-regime share {rg['top_regime_share_net']}")
    for g in rg["rows"]:
        print(f"                     {g['regime']}: {g['count']} RT net {g['net']} share {g['share_net_pct']}%")
    print(f"vol buckets (+)    : {vb['positive_bucket_count']}/{len(vb['rows'])}  "
          + "  ".join(f"{g['vol_bucket']}={g['net']}" for g in vb["rows"]))
    print(f"sides              : " + "  ".join(f"{g['side']}={g['net']} ({g['count']})" for g in sd["rows"])
          + f"  min-side {sd['min_side_net']}")
    print(f"execution stress   : net@slip2x={ex['net_at_slip2x']}  break-even extra slip "
          f"{ex['break_even_extra_slippage_bps_per_fill']} bps/fill (avg fill {ex['avg_fill_price']})")
    print(f"statistics         : t(gross/RT)={stt['t_gross_per_rt']} t(net/RT)={stt['t_net_per_rt']} "
          f"win {stt['win_rate_pct']}%")
    print("-" * 92)
    print("VERDICT: " + verdict["code"] + " - " + verdict["reason"])
    print("pre-registered criteria: " + "; ".join(f"{k}={v}" for k, v in verdict["criteria"].items()
                                                  if k in ("R1_trades", "R2_dates", "R3_sides",
                                                           "R4_regimes", "R5_execution", "R6_statistics")))
    print("AUDIT ONLY - frozen candidate untouched; no promotion, no ALGO READY, no production change.")
    print("=" * 92)
    print("STOP")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())