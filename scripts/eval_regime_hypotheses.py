"""Evaluate the §17e.5 regime-filter hypotheses on recorded champion evidence.

WS 7.6 — evaluation-only. Reads ``reports/model_performance/trades.csv``
(recorded champion replay), separates the protected OOS window, computes
per-regime / per-trend / per-volatility group statistics and short-side gate
simulations on the safe design+validation slice, then writes
``reports/regime_eval/regime_eval.json`` (git-ignored). The protected OOS is
never used for any computation.

Usage:  python scripts/eval_regime_hypotheses.py
"""
from __future__ import annotations

import json
import sys
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from fno_ai_paper_trading.evaluation.regime_eval import evaluate_recorded_regime_hypotheses

TRADES_CSV = ROOT / "reports" / "model_performance" / "trades.csv"
OUT_JSON = ROOT / "reports" / "regime_eval" / "regime_eval.json"


def _print_stats(title: str, stats) -> None:
    print(f"{title}: n={stats.count} win={stats.win_rate_pct}% "
          f"net={stats.net_pnl} exp={stats.expectancy}/trade")


def main() -> int:
    evaluation = evaluate_recorded_regime_hypotheses(str(TRADES_CSV))

    print("WS 7.6 regime-aware evaluation (recorded champion MA(5,21) replay)\n")
    print(f"recorded trades: {evaluation.total_trades}; "
          f"safe design+validation slice: {evaluation.safe_trades}; "
          f"protected OOS separated/never used: {evaluation.protected_separated}")
    print(f"long entries: {evaluation.long_entries_total} total, "
          f"{evaluation.long_entries_safe} on safe slice (100% SELL evidence)\n")
    _print_stats("baseline (safe slice)", evaluation.baseline)

    print("\nby entry regime:")
    for row in evaluation.by_regime:
        _print_stats(f"  {row.label}", row)
    print("\nby trend group (short entries only):")
    for row in evaluation.by_trend:
        _print_stats(f"  {row.label}", row)
    print("\nby volatility group:")
    for row in evaluation.by_volatility:
        _print_stats(f"  {row.label}", row)

    print("\nshort-side gate simulations (residual after suppressing group):")
    for label, residual, removed in evaluation.gates:
        print(f"  {label}: removed={removed} -> "
              f"n={residual.count} win={residual.win_rate_pct}% "
              f"net={residual.net_pnl} exp={residual.expectancy}")

    print("\nhypotheses (objective, recorded evidence):")
    for hypothesis in evaluation.hypotheses:
        print(f"  [{hypothesis.status}] {hypothesis.hypothesis_id} "
              f"{hypothesis.name}")
        print(f"      {hypothesis.verdict}")

    verdict = max((h.status for h in evaluation.hypotheses if h.verified), default="verified")
    print(f"\nCONCLUSION: regime-filter hypotheses "
          f"{'show no credible edge on recorded evidence' if verdict in ('structural_no_op', 'rejected') else 'require fresh recorded data'}; "
          f"frozen baseline MA(5,21) unchanged; no promotion.")

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "deliverable": "WS 7.6 regime-aware strategy evaluation",
        "evidence_source": str(TRADES_CSV),
        "protected_oos_start": "2026-01-01",
        "protected_oos_used": False,
        "evaluation": asdict(evaluation),
    }
    OUT_JSON.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    print(f"\nwrote {OUT_JSON}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())