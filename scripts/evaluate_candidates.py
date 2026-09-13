"""Candidate evaluation and SELECTION on design + validation (OOS withheld).

Research protocol (WS 7.16):
    * DESIGN/TRAIN     = bars before 2025-07-01 (hypothesis insight + initial run)
    * VALIDATION       = 2025-07-01 .. 2025-12-31 (shortlist selection / robustness)
    * PROTECTED OOS    = >= 2026-01-01  (SINGLE-USE, withheld here; see
                         scripts/finalize_oos_confirmation.py)

Only design + validation numbers are printed; protected-OOS results are written to
the artifact but never printed or used to choose the shortlist. The promotion gate
is intentionally NOT run here (it requires OOS) — it runs once at finalization.

PAPER ONLY: everything flows through BacktestEngine / PaperBroker / Portfolio /
RiskManager. No live order route exists.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fno_ai_paper_trading.backtest.config import BacktestConfig
from fno_ai_paper_trading.data.dataset_store import load_dataset
from fno_ai_paper_trading.evaluation.candidates import (
    cost_scan,
    robustness_grid,
    run_candidate_plan,
)
from fno_ai_paper_trading.evaluation.fast_signal import moving_average_cross_signals
from fno_ai_paper_trading.strategies.research_candidates import CANDIDATES

DATASET_5M = ROOT / "datasets" / "upstox_Nifty_50_5m_20220103_20260911.csv"
OUTPUT = ROOT / "reports" / "model_performance" / "candidates_eval.json"

DESIGN_END = datetime(2025, 7, 1)
VALIDATION_END = datetime(2026, 1, 1)


def _pretty(segment: dict[str, object]) -> str:
    def num(key: str) -> str:
        value = segment.get(key)
        if isinstance(value, str) and value not in ("n/a",):
            try:
                return f"{Decimal(value):f}"
            except (ValueError, ArithmeticError, InvalidOperation):
                return value
        return str(value)

    return (
        f"trades={segment['num_trades']} win={num('win_rate_pct')}% "
        f"net={num('net_return_pct')}% ({num('net_pnl')}) "
        f"DD={num('max_drawdown_pct')}% costs={num('transaction_costs')} "
        f"PF={num('profit_factor')} recon={segment['reconciliation_ok']}"
    )


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

    report: dict[str, object] = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds") + "Z",
        "schema_version": "1",
        "deliverable": "candidate_evaluation_selection",
        "split_dates": {
            "design_end": DESIGN_END.isoformat(),
            "validation_end": VALIDATION_END.isoformat(),
        },
        "bar_counts": {
            seg: len(champion_plan["runs"][seg].bars)
            for seg in ("design", "validation", "protected_oos", "full")
        },
        "champion_design": champion_plan["segments"]["design"],
        "champion_validation": champion_plan["segments"]["validation"],
        "notes": [
            "Protected OOS (>=2026-01-01) is withheld from selection; its numbers are "
            "present in the artifact but must not be read until finalization.",
            "Robustness and cost scans below run on design+validation only.",
        ],
    }

    print(f"split bars  design={report['bar_counts']['design']} "
          f"validation={report['bar_counts']['validation']} "
          f"protected_oos={report['bar_counts']['protected_oos']} "
          f"full={report['bar_counts']['full']}")
    print()
    print("=== CHAMPION MA(5,21) ===")
    print(f"  design    : {_pretty(report['champion_design'])}")
    print(f"  validation: {_pretty(report['champion_validation'])}")

    candidate_rows: list[dict[str, object]] = []
    for name, spec in CANDIDATES.items():
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
        design_seg = plan["segments"]["design"]
        validation_seg = plan["segments"]["validation"]

        design_bars = plan["runs"]["design"].bars
        val_bars = plan["runs"]["validation"].bars
        dev_bars = design_bars + val_bars
        robustness = robustness_grid(
            dev_bars,
            name=name,
            provider=provider,
            base_params=params,
            perturb=dict(spec.get("perturbations", {})),
            config=config,
        )
        costs = cost_scan(dev_bars, name=name, provider=provider, params=params, base_config=config)

        cand_row = {
            "candidate": name,
            "params": params,
            "rationale": spec["rationale"],
            "design": design_seg,
            "validation": validation_seg,
            "design_validation_robustness": robustness,
            "design_validation_cost_scan": costs,
        }
        candidate_rows.append(cand_row)
        report.setdefault("candidates", []).append(cand_row)

        valuation_delta = None
        champ_val = report["champion_validation"]
        if champ_val and validation_seg:
            try:
                delta = float(validation_seg["net_pnl"]) - float(champ_val["net_pnl"])
            except (TypeError, ValueError):
                delta = None
            valuation_delta = delta

        print()
        print(f"=== {name} ===")
        print(f"  params     : {params}")
        print(f"  rationale  : {spec['rationale']}")
        print(f"  design     : {_pretty(design_seg)}")
        print(f"  validation : {_pretty(validation_seg)}  (net delta vs champ: {valuation_delta})")
        print(f"  robustness : {len(robustness)} variants -> "
              + "; ".join(f"{r['variant']}={r['net_return_pct']}%" for r in robustness))
        print(f"  costs      : " + "; ".join(
            f"{r['scenario']}={r['net_return_pct']}%" for r in costs))
    print()
    print("PROTECTED OOS (>=2026-01-01) results are withheld from this selection output.")
    print("Run scripts/finalize_oos_confirmation.py once the shortlist is locked.")

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print(f"wrote : {OUTPUT}")


if __name__ == "__main__":
    main()