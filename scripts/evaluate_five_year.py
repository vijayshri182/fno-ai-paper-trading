"""WS 7.5 CLI: five-year historical replay over day-chunked datasets.

Loads every CSV in the datasets directory, chunks bars by trading day, and runs
an honest, resumable, day-by-day replay of the FROZEN MA(5,21) baseline. The
result never claims five years are complete — it reports exactly how many
trading days were processed, with a resumable JSON progress checkpoint.

Usage:
    python scripts/evaluate_five_year.py --datasets-dir datasets --out-dir reports
"""
from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

from fno_ai_paper_trading.data.dataset_store import load_dataset
from fno_ai_paper_trading.evaluation.five_year import (
    DayBars,
    FiveYearEvaluation,
    PeriodSplitConfig,
    ProgressStore,
    five_year_report_to_dict,
    five_year_report_to_html,
)
from fno_ai_paper_trading.strategies import MovingAverageCrossStrategy


def chunk_by_day(path: Path) -> list[DayBars]:
    dataset = load_dataset(path)
    grouped: dict[str, list] = {}
    for bar in dataset.bars:
        grouped.setdefault(bar.timestamp.date().isoformat(), []).append(bar)
    return [
        DayBars(
            day=date.fromisoformat(day_key),
            bars=tuple(grouped[day_key]),
            source_hash=dataset.data_hash,
        )
        for day_key in sorted(grouped)
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Five-year historical replay (WS 7.5)")
    parser.add_argument("--datasets-dir", default="datasets", help="directory of normalized CSVs")
    parser.add_argument("--out-dir", default="reports", help="directory for report artifacts")
    parser.add_argument("--name", default="five_year_replay", help="report name")
    parser.add_argument("--progress-file", default=None, help="resumable JSON checkpoint path")
    parser.add_argument("--start", default=None, help="inclusive start date YYYY-MM-DD")
    parser.add_argument("--end", default=None, help="inclusive end date YYYY-MM-DD")
    parser.add_argument("--training-ratio", type=float, default=0.60, help="training split ratio")
    parser.add_argument("--validation-ratio", type=float, default=0.20, help="validation split ratio")
    args = parser.parse_args(argv)

    datasets_dir = Path(args.datasets_dir)
    csv_files = sorted(datasets_dir.glob("*.csv"))
    days: list[DayBars] = []
    for path in csv_files:
        days.extend(chunk_by_day(path))
    if not days:
        print(f"no day-chunked data found under {datasets_dir} (no *.csv)")
        return 1

    start = date.fromisoformat(args.start) if args.start else None
    end = date.fromisoformat(args.end) if args.end else None
    if start or end:
        days = [d for d in days if (start is None or d.day >= start) and (end is None or d.day <= end)]

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    progress_path = Path(args.progress_file) if args.progress_file else out_dir / f"{args.name}.progress.json"
    progress = ProgressStore(progress_path)

    split = PeriodSplitConfig(
        training_ratio=args.training_ratio, validation_ratio=args.validation_ratio
    )
    evaluator = FiveYearEvaluation(period_split=split)
    strategy = MovingAverageCrossStrategy(fast=5, slow=21)
    report = evaluator.run(days, strategy, name=args.name, progress=progress)

    report_path = out_dir / f"{args.name}.json"
    html_path = out_dir / f"{args.name}.html"
    report_path.write_text(
        json.dumps(five_year_report_to_dict(report), indent=2), encoding="utf-8"
    )
    html_path.write_text(five_year_report_to_html(report), encoding="utf-8")

    print(
        f"replayed {report.days_processed}/{report.days_available} trading days "
        f"({report.status}); {report.days_skipped} invalid day(s) skipped"
    )
    if report.aggregate is not None:
        summary = report.aggregate.aggregate
        print(
            f"aggregate net P&L {summary.total_pnl:.2f} Rs "
            f"({summary.total_return_pct:.4f}%), {summary.num_trades} round trips"
        )
    print(f"wrote {report_path} and {html_path} (progress: {progress_path})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())