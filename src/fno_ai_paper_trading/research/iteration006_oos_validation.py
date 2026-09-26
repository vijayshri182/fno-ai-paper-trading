"""ITERATION 006 - PROTECTED OOS VALIDATION OF n3_ensemble (single experiment).

Validation only.  The Iteration-005 discovery candidate ``n3_ensemble`` is
frozen EXACTLY as recorded in ``research/iteration005_discovery.py`` (
``EnsembleParams`` defaults: fast=9, slow=26, slope_window=5, lookback=20,
stop_atr_mult=4.0, max_hold_days=25; warmup = slow+slope_window+lookback+3 =
54 sessions).  Parameters are re-verified against the frozen literals (asserted)
before the OOS run; any mismatch aborts instead of substituting a similar
candidate.

Only one dimension changes versus the Iteration-005 discovery experiment:
    DATA = PROTECTED OOS (>= 2025-10-06, 17,412 bars / 233 trading days,
                          2025-10-06 .. 2026-09-11).

Everything else is byte-identical to the Iteration-005 evaluation stack:
unchanged BacktestEngine, EvaluationConfig().backtest() (capital 100k, qty 1,
commission 0.0003/side, slippage 0.001 adverse, 2% LONG-only protective stop,
daily-loss 10000), the same audit conventions (build_round_trips + economic
block + cost stress), and the same causal full-series signal stream sliced to
the window (engine replays the OOS slice starting flat, exactly like the
Iteration-005 train/validation window methodology).

Pipeline order (intentionally immutable-first):
  1. fingerprint is materialised (iteration_006_n3_fingerprint.json) BEFORE
     the engine run, with `oos_run_status: PENDING`;
  2. exactly ONE engine run over the protected-OOS slice;
  3. metrics + verdict materialised (iteration_006_protected_oos_n3.json);
  4. terminal summary printed (ends with STOP).

No tuning, no re-runs, no parameter search, no OOS look-back editing.  No
promotion under any verdict: Iteration-006 is VALIDATION research only.
"""
from __future__ import annotations

import hashlib
import json
import statistics as st
import sys
from datetime import date
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

REPO = Path(__file__).resolve().parents[3]
DATASET = REPO / "datasets" / "upstox_Nifty_50_5m_20220103_20260911.csv"
FINGERPRINT_FILE = REPO / "runs" / "research" / "day_batch" / "iteration_006_n3_fingerprint.json"
OUT_FILE = REPO / "runs" / "research" / "day_batch" / "iteration_006_protected_oos_n3.json"

OOS_START = date(2025, 10, 6)
INITIAL_CAPITAL = Decimal("100000")

# Frozen n3_ensemble configuration (authoritative literals, from
# iteration005_discovery.EnsembleParams defaults / ensemble_signals).
FROZEN_PARAMS = {
    "fast": 9,
    "slow": 26,
    "slope_window": 5,
    "lookback": 20,
    "stop_atr_mult": 4.0,
    "max_hold_days": 25,
}
FROZEN_WARMUP = FROZEN_PARAMS["slow"] + FROZEN_PARAMS["slope_window"] + FROZEN_PARAMS["lookback"] + 3


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _verify_frozen_candidate() -> dict:
    """Re-verify the candidate matches the recorded frozen literal; else STOP."""
    params = EnsembleParams()
    actual = {
        "fast": params.fast,
        "slow": params.slow,
        "slope_window": params.slope_window,
        "lookback": params.lookback,
        "stop_atr_mult": params.stop_atr_mult,
        "max_hold_days": params.max_hold_days,
    }
    warmup = params.slow + params.slope_window + params.lookback + 3
    if dict(actual) != dict(FROZEN_PARAMS) or warmup != FROZEN_WARMUP:
        raise SystemExit(
            "STOP: n3_ensemble no longer matches the Iteration-005 frozen "
            f"configuration. recorded={FROZEN_PARAMS} actual={actual}. "
            "Refusing to substitute a similar candidate."
        )
    return {
        "parameters": actual,
        "warmup_sessions": warmup,
        "module": "research/iteration005_discovery.py",
        "function": "ensemble_signals",
        "params_class": "EnsembleParams",
        "verification": "parameters asserted equal to recorded frozen literals",
    }


def _eval_config_block() -> dict:
    cfg = EvaluationConfig().backtest()
    return {
        "initial_capital": int(cfg.initial_capital),
        "quantity": int(cfg.quantity),
        "commission_rate": str(cfg.commission_rate),
        "slippage_rate": str(cfg.slippage_rate),
        "commission_fixed": str(cfg.commission_fixed),
        "enable_risk_manager": bool(cfg.enable_risk_manager),
        "max_position_quantity": int(cfg.max_position_quantity),
        "max_order_notional": int(cfg.max_order_notional),
        "max_daily_loss": int(cfg.max_daily_loss),
        "enable_stop_loss": bool(cfg.enable_stop_loss),
        "stop_loss_pct": str(cfg.stop_loss_pct),
        "engine": "BacktestEngine (unchanged)",
        "cost_model": "slippage 0.001 adverse + commission 0.0003/side (Iter-005 identical)",
    }


def _code_config_block() -> dict:
    heads: list[str] = []
    try:
        import subprocess
        heads.append(subprocess.run(
            ["git", "-C", str(REPO), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=15).stdout.strip())
    except Exception:
        heads.append("")
    files = {
        "iteration006_oos_validation.py": (REPO / "src" / "fno_ai_paper_trading" / "research" / "iteration006_oos_validation.py"),
        "iteration005_discovery.py": (REPO / "src" / "fno_ai_paper_trading" / "research" / "iteration005_discovery.py"),
        "evaluation/records.py": (REPO / "src" / "fno_ai_paper_trading" / "evaluation" / "records.py"),
        "backtest/engine.py": (REPO / "src" / "fno_ai_paper_trading" / "backtest" / "engine.py"),
        "data/dataset_store.py": (REPO / "src" / "fno_ai_paper_trading" / "data" / "dataset_store.py"),
    }
    file_hashes = {}
    for label, path in files.items():
        if path.exists():
            file_hashes[label] = _sha256_file(path)
        else:
            file_hashes[label] = "MISSING"
    return {
        "git_head": heads[0] if heads else "",
        "python": sys.version.split()[0],
        "file_hashes_sha256": file_hashes,
    }


def build_fingerprint(stored, research_bars, oos_bars, oos_days) -> dict:
    oos_first = min(b.timestamp for b in oos_bars).date()
    oos_last = max(b.timestamp for b in oos_bars).date()
    research_last = max(b.timestamp for b in research_bars).date()
    frozen = _verify_frozen_candidate()
    return {
        "experiment": "ITERATION_006_PROTECTED_OOS_VALIDATION_n3_ensemble",
        "validation_only": True,
        "promotion_notice": (
            "Iteration 006 is a VALIDATION experiment, NOT promotion evidence. "
            "No verdict changes any production setting; ALGO READY stays NO."
        ),
        "recorded_before_oos_run": True,
        "candidate_freeze": {
            "candidate_id": "n3_ensemble",
            "family": "META_DECISION_ENSEMBLE",
            "discovered_in": "ITERATION_005",
            "status": "FROZEN (no modification, no tuning, no repair)",
            **frozen,
            "component_strategies": {
                "trend": "day-lagged EMA fast/slow + slope_window prior-fasts crossing (state target +/-1/0)",
                "vol_confirm": "prior completed session range >= avg range of the lookback sessions before it AND same-direction close",
            },
            "ensemble_semantics": "AND-gate (3-of-3 confluence) - equal weight, hard confluence only",
            "entry_rules": "session-open bar, state==0 -> BUY/SELL when target +/-1 (trend AND vol confirmation)",
            "exit_rules": (
                "provider ATR stop (entry_ref - state*stop_atr_mult*atr_ref, both sides, every bar); "
                "max-hold timeout at session open when hold_days >= max_hold_days; "
                "confluence broken (state != target != 0) or confluence flat (target==0) exits at session open"
            ),
            "stop_loss": "provider ATR stop (signals) + engine protective 2% LONG-only stop (unchanged engine)",
            "position_sizing": "qty 1 (unchanged)",
            "warmup_note": "warmup 54 sessions is satisfied by construction: the signal stream is computed over the FULL 2022-01-03..2026-09-11 series (causal), then sliced to the OOS window; engine replays the OOS slice starting flat exactly like Iteration-005 window slices.",
        },
        "dataset": {
            "name": stored.path.name,
            "data_hash_sha256": stored.data_hash,
            "total_bars": len(stored.bars),
            "first_bar": stored.bars[0].timestamp.date().isoformat(),
            "last_bar": stored.bars[-1].timestamp.date().isoformat(),
        },
        "boundary": {
            "protected_oos_start": OOS_START.isoformat(),
            "research_end": research_last.isoformat(),
            "oos_window": [oos_first.isoformat(), oos_last.isoformat()],
            "oos_bars": len(oos_bars),
            "oos_trading_days": oos_days,
            "oos_used_for_selection": False,
            "only_changed_dimension": "DATA = PROTECTED OOS; all other dimensions identical to Iteration-005",
        },
        "evaluation_config": _eval_config_block(),
        "methodology": {
            "same_framework_as_iteration_005": True,
            "signal_stream": "full-series causal signal stream (ensemble_signals over all 87,193 bars), sliced at OOS_START",
            "engine_replay": "unchanged BacktestEngine on the OOS slice, starting flat (Iter-005 window convention)",
            "audit": "build_round_trips + economic_block + cost_stress_block (Iter-005 identical)",
            "regime_labels": "classed_days over full series (per-day stats; Iter-005 identical)",
        },
        "pre_registered_decision_criteria": {
            "C1_trades": "round_trips >= 10 over the 233-day OOS window",
            "C2_gross_edge": "gross_close_edge > 0 (pre-cost gross edge present on OOS)",
            "C3_economic": "total_pnl > 0 at real 1x costs AND gross/costs coverage > 100%",
            "C4_evidence_quality": "t_gross_per_rt >= 1.0 AND carry_share_pct >= 50.0",
            "verdict_map": {
                "INCONCLUSIVE_LOW_TRADES": "C1 fails - too few OOS trades for any economic statement",
                "FAIL_NO_GROSS_EDGE_OOS": "C1 ok, C2 fails - no positive gross edge on unseen data",
                "FAIL_COSTS_OOS": "C2 ok, C3 fails - edge present but destroyed by costs on OOS",
                "PARTIAL_NEEDS_REPLICATION": "C1-C3 ok, C4 fails - positive but statistically weak OOS evidence",
                "PASS_SURVIVES_PROTECTED_OOS": "C1-C4 all hold - economically meaningful on protected OOS (STILL research-only)",
            },
        },
        "code_config": _code_config_block(),
        "oos_run_status": "PENDING",
    }


def slice_oos(all_bars, all_signals):
    """Bars/signals for the protected OOS window (>= OOS_START)."""
    bars_out: list = []
    sig_out: list = []
    for b, s in zip(all_bars, all_signals):
        if b.timestamp.date() >= OOS_START:
            bars_out.append(b)
            sig_out.append(s)
    assert bars_out, "empty OOS slice"
    assert len(bars_out) == len(sig_out)
    return bars_out, sig_out


def streak_stats(rts: Sequence[dict]) -> dict:
    best_w = cur_w = 0
    best_l = cur_l = 0
    for r in rts:
        if r["net"] > 0:
            cur_w += 1
            cur_l = 0
        else:
            cur_l += 1
            cur_w = 0
        best_w = max(best_w, cur_w)
        best_l = max(best_l, cur_l)
    return {"longest_winning_streak": best_w, "longest_losing_streak": best_l}


def mandatory_metrics(rts: Sequence[dict], result, days_traded: int, active_days: int) -> dict:
    n = len(rts)
    gross_pos = [Decimal(r["gross_close"]) for r in rts if r["gross_close"] > 0]
    gross_neg = [Decimal(r["gross_close"]) for r in rts if r["gross_close"] <= 0]
    net_pos = [Decimal(r["net"]) for r in rts if r["net"] > 0]
    net_neg = [Decimal(r["net"]) for r in rts if r["net"] <= 0]
    gross_profit = sum(gross_pos, Decimal("0"))
    gross_loss = -sum(gross_neg, Decimal("0"))
    slippage = it5.amt_sum(rts, "slippage")
    commission = it5.amt_sum(rts, "commission")
    total_costs = slippage + commission
    gross_edge = it5.amt_sum(rts, "gross_close")
    net_pnl = result.total_pnl
    profit_factor_gross = float(gross_profit / gross_loss) if gross_loss else None
    net_closed = it5.amt_sum(rts, "net")
    pf_net_num = sum(net_pos, Decimal("0"))
    pf_net_den = -sum(net_neg, Decimal("0"))
    profit_factor_net = float(pf_net_num / pf_net_den) if pf_net_den else None
    win_vals = [float(r["net"]) for r in rts if r["net"] > 0]
    loss_vals = [float(r["net"]) for r in rts if r["net"] <= 0]
    holds = [float(r["hold_minutes"]) for r in rts]
    streaks = streak_stats(rts)
    return {
        "starting_capital": str(INITIAL_CAPITAL),
        "ending_equity": str(INITIAL_CAPITAL + net_pnl),
        "net_pnl": str(net_pnl),
        "return_pct": round(float(result.total_return_pct), 4),
        "gross_profit": str(gross_profit),
        "gross_loss": str(gross_loss),
        "gross_pnl": str(gross_profit - gross_loss),
        "gross_edge": str(gross_edge),
        "commissions": str(commission),
        "slippage": str(slippage),
        "total_costs": str(total_costs),
        "cost_coverage_pct": round(float(gross_edge / total_costs * 100), 2) if total_costs else None,
        "profit_factor_gross": round(profit_factor_gross, 4) if profit_factor_gross is not None else None,
        "profit_factor_net": round(profit_factor_net, 4) if profit_factor_net is not None else None,
        "round_trips": n,
        "fills": result.orders_filled,
        "wins": sum(1 for r in rts if r["net"] > 0),
        "losses": sum(1 for r in rts if r["net"] <= 0),
        "win_rate_pct": round(sum(1 for r in rts if r["net"] > 0) / n * 100, 2) if n else 0.0,
        "avg_winning_trade": round(st.mean(win_vals), 4) if win_vals else None,
        "avg_losing_trade": round(st.mean(loss_vals), 4) if loss_vals else None,
        "expectancy_per_rt": round(net_closed / n, 4) if n else None,
        "max_drawdown": str(result.max_drawdown),
        "max_drawdown_pct": round(float(Decimal(result.max_drawdown) / INITIAL_CAPITAL * 100), 4),
        **streaks,
        "trading_days": days_traded,
        "active_trading_days": active_days,
        "no_trade_days": days_traded - active_days,
        "trades_per_day": round(n / days_traded, 4) if days_traded else None,
        "avg_holding_period_minutes": round(st.mean(holds), 1) if holds else None,
        "median_holding_period_minutes": round(st.median(holds), 1) if holds else None,
    }


def active_trading_days(journal: Sequence[dict]) -> int:
    days = set()
    for row in journal:
        if "fill_price" in row or "stop_price" in row:
            days.add(row["timestamp"].split("T")[0])
    return len(days)


def oos_verdict(metrics: dict, full_block: dict) -> dict:
    """Pre-registered verdict from the C1..C4 criteria (pure, testable)."""
    rts = metrics["round_trips"]
    gross_edge = Decimal(metrics["gross_edge"])
    total_pnl = Decimal(metrics["net_pnl"])
    t_gross = full_block.get("t_gross_per_rt")
    carry_share = full_block.get("carry_share_pct") or 0.0
    coverage = metrics.get("cost_coverage_pct")
    c1 = rts >= 10
    c2 = gross_edge > 0
    c3 = total_pnl > 0 and (coverage is not None and coverage > 100.0)
    c4 = (t_gross is not None and float(t_gross) >= 1.0) and carry_share >= 50.0
    criteria = {
        "C1_trades": c1, "C2_gross_edge": c2, "C3_economic": c3, "C4_evidence_quality": c4,
        "round_trips": rts, "gross_edge": str(gross_edge), "total_pnl": str(total_pnl),
        "cost_coverage_pct": coverage, "t_gross_per_rt": t_gross, "carry_share_pct": carry_share,
    }
    if not c1:
        code = "INCONCLUSIVE_LOW_TRADES"
    elif not c2:
        code = "FAIL_NO_GROSS_EDGE_OOS"
    elif not c3:
        code = "FAIL_COSTS_OOS"
    elif not c4:
        code = "PARTIAL_NEEDS_REPLICATION"
    else:
        code = "PASS_SURVIVES_PROTECTED_OOS"
    verdict_map = {
        "INCONCLUSIVE_LOW_TRADES": "C1 fails - too few OOS trades for any economic statement",
        "FAIL_NO_GROSS_EDGE_OOS": "C1 ok, C2 fails - no positive gross edge on unseen data",
        "FAIL_COSTS_OOS": "C2 ok, C3 fails - edge present but destroyed by costs on OOS",
        "PARTIAL_NEEDS_REPLICATION": "C1-C3 ok, C4 fails - positive but statistically weak OOS evidence",
        "PASS_SURVIVES_PROTECTED_OOS": "C1-C4 all hold - economically meaningful on protected OOS (STILL research-only)",
    }
    return {"code": code, "reason": verdict_map[code], "criteria": criteria}


def main() -> int:
    stored = load_dataset(DATASET)
    all_bars = stored.bars
    research_bars = [b for b in all_bars if b.timestamp.date() < OOS_START]
    oos_bars = [b for b in all_bars if b.timestamp.date() >= OOS_START]
    assert research_bars and oos_bars
    assert max(b.timestamp for b in research_bars).date() < OOS_START
    assert min(b.timestamp for b in oos_bars).date() >= OOS_START
    oos_days = len({b.timestamp.date() for b in oos_bars})

    fp = build_fingerprint(stored, research_bars, oos_bars, oos_days)
    FINGERPRINT_FILE.parent.mkdir(parents=True, exist_ok=True)
    FINGERPRINT_FILE.write_text(json.dumps(fp, indent=2, default=str), encoding="utf-8")
    print(f"fingerprint recorded pre-run: {FINGERPRINT_FILE}  sha256={_sha256_file(FINGERPRINT_FILE)}")

    # ---- signal stream over the FULL series (causal), then slice OOS ----
    params = EnsembleParams()
    all_signals = ensemble_signals(all_bars, params)
    oos_sig_bars, oos_sig = slice_oos(all_bars, all_signals)

    # ---- the ONE protected-OOS engine run ----
    config = EvaluationConfig().backtest()
    engine = BacktestEngine()
    journal: list = []
    result = engine_run(oos_sig_bars, config, engine, oos_sig, journal)

    # ---- economic audit, identical to Iteration-005 ----
    day_map, vol_bucket_map = it5.classed_days(all_bars)
    full_block = economic_block(
        "n3_ensemble", oos_sig_bars, oos_sig, result, journal, day_map, vol_bucket_map
    )
    full_block.update(cost_stress_block(full_block))
    full_block["reconcile_all"] = full_block["reconcile"]["reconcile_all"]
    rts, seen_open, seen_close = it5.build_round_trips(oos_sig_bars, journal)
    for rt in rts:
        it5.add_flags(rt, oos_sig_bars, day_map, vol_bucket_map)
    metrics = mandatory_metrics(
        rts,
        result,
        oos_days,
        active_trading_days(journal),
    )
    verdict = oos_verdict(metrics, full_block)

    fp["oos_run_status"] = "COMPLETED"
    FINGERPRINT_FILE.write_text(json.dumps(fp, indent=2, default=str), encoding="utf-8")

    reference = {}
    iter5_json = REPO / "runs" / "research" / "day_batch" / "iteration_005_economic_discovery.json"
    if iter5_json.exists():
        try:
            c5 = json.loads(iter5_json.read_text(encoding="utf-8"))["competition"]["n3_ensemble"]
            f5 = c5["full_domain"]
            reference = {
                "research_domain_reserved": {
                    "window": "2022-01-03..2025-10-03",
                    "net_at_1x": f5["net_at_1x"],
                    "round_trips": f5["round_trips"],
                    "gross_per_rt_mean": f5["gross_per_rt_mean"],
                    "net_per_rt_mean": f5["net_per_rt_mean"],
                    "carry_share_pct": f5["carry_share_pct"],
                    "t_gross_per_rt": f5["t_gross_per_rt"],
                    "val_net": c5["walkforward"]["val_net_pnl"],
                    "classification": c5["classification"]["code"],
                }
            }
        except Exception as exc:  # pragma: no cover - best-effort reference only
            reference = {"error": str(exc)}

    out = {
        "experiment": "ITERATION_006_PROTECTED_OOS_VALIDATION_n3_ensemble",
        "validation_only": True,
        "promotion_notice": (
            "Iteration 006 is VALIDATION research and is NOT promotion evidence; "
            "no ALGO READY, no production change under any verdict."
        ),
        "changed_dimension": "DATA = PROTECTED OOS (>= 2025-10-06). All other "
                             "dimensions identical to Iteration-005.",
        "fingerprint": {
            "path": str(FINGERPRINT_FILE.relative_to(REPO)),
            "sha256": _sha256_file(FINGERPRINT_FILE),
            "frozen_matches_iteration_005": True,
        },
        "oos_run": {"engine_runs": 1, "signals_computed_over_full_series": True},
        "domain": {
            "protected_oos_start": OOS_START.isoformat(),
            "oos_window": [min(b.timestamp for b in oos_sig_bars).date().isoformat(),
                           max(b.timestamp for b in oos_sig_bars).date().isoformat()],
            "oos_bars": len(oos_sig_bars),
            "oos_trading_days": oos_days,
        },
        "economics": full_block,
        "mandatory_metrics": metrics,
        "verdict": verdict,
        "reference_iteration_005": reference,
    }

    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    print(f"protected-OOS result written: {OUT_FILE}  sha256={_sha256_file(OUT_FILE)}")

    # ---------------- terminal summary ----------------
    print("=" * 92)
    print("ITERATION 006 - PROTECTED OOS VALIDATION OF n3_ensemble (SINGLE EXPERIMENT)")
    print("=" * 92)
    print(f"OOS window        : {out['domain']['oos_window'][0]} .. {out['domain']['oos_window'][1]}"
          f"  ({out['domain']['oos_bars']} bars / {out['domain']['oos_trading_days']} trading days)")
    print(f"frozen params     : {FROZEN_PARAMS}  warmup={FROZEN_WARMUP}")
    print("-" * 92)
    print(f"round trips       : {metrics['round_trips']}   fills: {metrics['fills']}")
    print(f"wins/losses       : {metrics['wins']}/{metrics['losses']}   win rate: {metrics['win_rate_pct']}%")
    print(f"gross edge        : {metrics['gross_edge']}   costs: {metrics['total_costs']}   coverage: {metrics['cost_coverage_pct']}%")
    print(f"gross profit/loss : {metrics['gross_profit']} / {metrics['gross_loss']}   PF(gross): {metrics['profit_factor_gross']}")
    print(f"net PnL (1x)      : {metrics['net_pnl']}   return: {metrics['return_pct']}%   expectancy/RT: {metrics['expectancy_per_rt']}")
    print(f"net at 3x/5x      : {full_block['net_at_3x']} / {full_block['net_at_5x']}")
    print(f"max drawdown      : {metrics['max_drawdown']} ({metrics['max_drawdown_pct']}%)   streaks W/L: "
          f"{metrics['longest_winning_streak']}/{metrics['longest_losing_streak']}")
    print(f"trades/day        : {metrics['trades_per_day']}   active days: {metrics['active_trading_days']}/{metrics['trading_days']}"
          f"   avg hold: {metrics['avg_holding_period_minutes']} min")
    print(f"carry share       : {full_block['carry_share_pct']}%  side: "
          + " ".join(f"{s['side']}:{s['count']}" for s in full_block['side_breakdown']))
    print(f"gross/RT net/RT   : {full_block['gross_per_rt_mean']} / {full_block['net_per_rt_mean']}   t(gross): {full_block['t_gross_per_rt']}")
    r = full_block["reconcile"]
    print(f"reconcile         : strict={full_block['reconcile_all']} identity={r['economic_identity']}")
    print("-" * 92)
    print("VERDICT: " + verdict["code"] + " - " + verdict["reason"])
    print("pre-registered criteria: " + "; ".join(f"{k}={v}" for k, v in verdict["criteria"].items() if k in ("C1_trades", "C2_gross_edge", "C3_economic", "C4_evidence_quality")))
    print("VALIDATION ONLY - no promotion, no ALGO READY, no production change.")
    print("=" * 92)
    print("STOP")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())