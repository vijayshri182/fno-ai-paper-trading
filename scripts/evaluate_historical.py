"""Run a deterministic historical evaluation over local validated datasets.

Evaluates the frozen MA(5,21) baseline (or an explicit candidate) over every
dataset in a directory and writes a JSON + HTML report. Offline and read-only —
no API calls, no live orders.

Usage:
    python scripts/evaluate_historical.py --datasets datasets --out-dir reports
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fno_ai_paper_trading.data.dataset_store import load_dataset
from fno_ai_paper_trading.evaluation import (
    EvaluationConfig,
    HistoricalEvaluator,
    evaluation_run_to_dict,
    evaluation_run_to_html,
)
from fno_ai_paper_trading.strategies import MovingAverageCrossStrategy

BASELINE = MovingAverageCrossStrategy(fast=5, slow=21)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--datasets", default="datasets", help="directory of *.csv validated datasets"
    )
    parser.add_argument("--out-dir", default="reports", help="output directory")
    parser.add_argument("--name", default="historical-evaluation", help="run name")
    args = parser.parse_args()

    dataset_dir = Path(args.datasets)
    if not dataset_dir.is_dir():
        print(f"no dataset directory found at {dataset_dir} (nothing evaluated)", file=sys.stderr)
        return 1
    csv_files = sorted(dataset_dir.glob("*.csv"))
    if not csv_files:
        print(f"no *.csv datasets found in {dataset_dir}", file=sys.stderr)
        return 1

    datasets = []
    for path in csv_files:
        try:
            datasets.append(load_dataset(path))
        except Exception as exc:  # noqa: BLE001 - report and skip
            print(f"skipping {path.name}: {exc}", file=sys.stderr)

    if not datasets:
        print("no valid datasets loaded; nothing evaluated", file=sys.stderr)
        return 1

    evaluator = HistoricalEvaluator(config=EvaluationConfig())
    run = evaluator.evaluate_strategy(
        datasets, BASELINE, name=args.name, strategy_params={"fast": 5, "slow": 21}, baseline=True
    )

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    json_text = json.dumps(evaluation_run_to_dict(run), indent=2, default=str)
    (out_dir / f"{args.name}.json").write_text(json_text, encoding="utf-8")
    (out_dir / f"{args.name}.html").write_text(
        evaluation_run_to_html(run), encoding="utf-8"
    )

    aggregate = run.aggregate
    print(
        f"evaluated {aggregate.sessions} session(s), {aggregate.bars_processed} bars, "
        f"{aggregate.num_trades} round trips, net P&L {aggregate.total_pnl} Rs "
        f"({aggregate.total_return_pct}%)"
    )
    print(f"wrote {out_dir / (args.name + '.json')} and {out_dir / (args.name + '.html')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())