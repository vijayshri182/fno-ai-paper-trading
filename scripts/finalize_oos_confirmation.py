"""SINGLE-USE protected-OOS confirmation for the locked shortlist (WS 7.16).

Pre-registered candidate set: c1..c5 (locked before any segment results).
Selection on DESIGN + VALIDATION only (scripts/evaluate_candidates.py): the
shortlist is c2 (slow long-only MA 20/50) and c4 (trend-gated MA 5/21) — the
only candidates that were not worse than the champion on validation, kept the
sign of their net result across bounded perturbations, and did not implode at
zero friction. c1/c3/c5 were dropped on those design+validation grounds.

This script performs the ONE allowed read of >=2026-01-01 for those two, applies
the existing PromotionGate, and prints the scepticism checks (cross-validation
stability of gate blockers, cost scan, perturbation grid on the OOS itself).

PAPER ONLY. Never used to fit anything; never run twice.
"""
from __future__ import annotations

import json
import math
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fno_ai_paper_trading.backtest.config import BacktestConfig
from fno_ai_paper_trading.data.dataset_store import load_dataset
from fno_ai_paper_trading.evaluation.candidates import (
    build_delta_views,
    cost_scan,
    robustness_grid,
    run_candidate_plan,
)
from fno_ai_paper_trading.evaluation.fast_signal import moving_average_cross_signals
from fno_ai_paper_trading.promotion.gate import PromotionCriteria, PromotionGate
from fno_ai_paper_trading.strategies.research_candidates import CANDIDATES

DATASET_5M = ROOT / "datasets" / "upstox_Nifty_50_5m_20220103_20260911.csv"
OUTPUT = ROOT / "reports" / "model_performance" / "oos_confirmation.json"

DESIGN_END = datetime(2025, 7, 1)
VALIDATION_END = datetime(2026, 1, 1)

SHORTLIST = ("c2_slow_long_only_ma_cross", "c4_trend_gated_ma_cross")


def _fmt(segment: dict[str, object]) -> str:
    return (
        f"trades={segment['num_trades']} win={segment['win_rate_pct']}% "
        f"net={segment['net_return_pct']}% ({segment['net_pnl']}) "
        f"DD={segment['max_drawdown_pct']}% costs={segment['transaction_costs']} "
        f"recon={segment['reconciliation_ok']}"
    )


def _per_trade_tstat(run) -> dict[str, str]:
    """Slippage-inclusive, pre-commission per-trade P&L t-stat (price_pnl)."""
    trips = run.round_trips
    gross = [trip.price_pnl for trip in trips]
    if not gross:
        return {"trades": "0", "mean": "n/a", "t": "n/a"}
    try:
        mean = sum(gross) / len(gross)
        var = sum((x - mean) ** 2 for x in gross) / (len(gross) - 1)
        count = len(gross)
        se = var.sqrt() / Decimal(count).sqrt()
        t = mean / se
    except Exception:
        return {"trades": str(len(gross)), "mean": "n/a", "t": "n/a"}
    return {"trades": str(len(gross)), "mean": str(mean), "t": str(t)}


def _strict_verdict(deltas, counts, name: str):
    """Decisive gate call: credible edge requires POSITIVE OOS net P&L."""
    criteria = PromotionCriteria(require_positive_oos_pnl=True)
    return PromotionGate().evaluate(deltas, counts, name, criteria=criteria)


def main() -> None:
    if not DATASET_5M.exists():
        raise SystemExit("missing 5m dataset")

    config = BacktestConfig()
    bars = load_dataset(DATASET_5M).bars

    champion_plan = run_candidate_plan(
        bars,
        name="moving_average_cross",
        provider=moving_average_cross_signals,
        params={"fast": 5, "slow": 21},
        config=config,
        design_end=DESIGN_END,
        validation_end=VALIDATION_END,
    )

    oos_bars = champion_plan["runs"]["protected_oos"].bars

    report: dict[str, object] = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds") + "Z",
        "schema_version": "1",
        "deliverable": "protected_oos_single_use_confirmation",
        "shortlist": list(SHORTLIST),
        "oops": "This OOS read is single-use; any re-run or further tuning invalidates it.",
        "bar_counts": {
            seg: len(champion_plan["runs"][seg].bars)
            for seg in ("design", "validation", "protected_oos", "full")
        },
    }

    print(f"split bars  design={report['bar_counts']['design']} "
          f"validation={report['bar_counts']['validation']} "
          f"protected_oos={report['bar_counts']['protected_oos']}")

    first_close = oos_bars[0].close
    last_close = oos_bars[-1].close
    buy_hold = ((last_close - first_close) / first_close) * 100
    print()
    print("=== BUY & HOLD IN OOS ===")
    print(f"  {first_close:f} -> {last_close:f}  = {buy_hold:f}%  ({len(oos_bars)} bars)")
    report["oos_buy_and_hold_pct"] = str(buy_hold)

    verdicts: dict[str, object] = {}
    for name in SHORTLIST:
        spec = CANDIDATES[name]
        provider = spec["provider"]  # type: ignore[assignment]
        params = dict(spec["params"])  # type: ignore[arg-type]
        plan = run_candidate_plan(
            bars,
            name=name,
            provider=provider,
            params=params,
            config=config,
            design_end=DESIGN_END,
            validation_end=VALIDATION_END,
        )
        deltas, counts = build_delta_views(champion_plan, plan)
        verdict = PromotionGate().evaluate(deltas, counts, name)
        strict = _strict_verdict(deltas, counts, name)
        oos_run = plan["runs"]["protected_oos"]
        tstat = _per_trade_tstat(oos_run)
        oos_seg = plan["segments"]["protected_oos"]
        oos_robustness = robustness_grid(
            plan["runs"]["protected_oos"].bars,
            name=name,
            provider=provider,
            base_params=params,
            perturb=dict(spec.get("perturbations", {})),
            config=config,
        )
        oos_costs = cost_scan(
            oos_bars, name=name, provider=provider, params=params, base_config=config
        )

        print()
        print(f"=== {name}  (single OOS read) ===")
        print(f"  params     : {params}")
        print(f"  design     : {_fmt(plan['segments']['design'])}")
        print(f"  validation : {_fmt(plan['segments']['validation'])}")
        print(f"  PROTECTED OOS : {_fmt(oos_seg)}")
        print(f"  OOS per-trade (slippage-adj, pre-commission): "
              f"mean={tstat['mean']} t={tstat['t']}  (n={tstat['trades']})")
        print(f"  OOS robustness: " + "; ".join(
            f"{r['variant']}={r['net_return_pct']}%" for r in oos_robustness))
        print(f"  OOS costs  : " + "; ".join(
            f"{r['scenario']}={r['net_return_pct']}%" for r in oos_costs))
        print(f"  GATE (default criteria): {verdict.decision}  "
              f"(i.e. challenger loses less than the champion)")
        print(f"  GATE (credible edge, positive-OOS-P&L): {strict.decision}")
        for reason in strict.reasons:
            print(f"      - {reason}")

        verdicts[name] = {
            "candidate": name,
            "gate_default": {"decision": verdict.decision, "reasons": list(verdict.reasons)},
            "gate_credible_positive_oos": {
                "decision": strict.decision,
                "reasons": list(strict.reasons),
                "evidence": strict.evidence,
            },
            "oos_per_trade_slippage_adj_pre_commission": tstat,
            "design": plan["segments"]["design"],
            "validation": plan["segments"]["validation"],
            "protected_oos": oos_seg,
            "oos_robustness": oos_robustness,
            "oos_cost_scan": oos_costs,
        }

    report["verdicts"] = verdicts
    report["conclusion"] = (
        "A"
        if any(
            v["gate_credible_positive_oos"]["decision"] == "PROMOTE"
            for v in verdicts.values()
        )
        else "B"
    )

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print()
    print(f"Conclusion: {report['conclusion']}  (A: a challenger passed the gate with a "
          f"credible positive-OOS edge; B: no credible, robust, reproducible edge "
          f"demonstrated)")
    print(f"wrote : {OUTPUT}")


if __name__ == "__main__":
    main()