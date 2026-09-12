"""WS 7.11 CLI: champion vs challenger comparison report.

Compares the FROZEN MA(5,21) champion against challenger candidates on shared,
validated datasets with identical cost/execution assumptions, and writes a
JSON artifact + HTML report. Use the day-based mode for full period splits
(training / validation / out-of-sample):

    python scripts/evaluate_champion_challenger.py --datasets-dir datasets --out-dir reports

Evidence only: the comparison never promotes a challenger. Promotion/rollback
gating is a later workstream (WS 7.12).
"""
from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

from fno_ai_paper_trading.data.dataset_store import load_dataset
from fno_ai_paper_trading.evaluation.champion_challenger import (
    ChampionChallenger,
    multi_period_comparison_to_dict,
    multi_period_comparison_to_html,
)
from fno_ai_paper_trading.evaluation.five_year import DayBars, PeriodSplitConfig
from fno_ai_paper_trading.strategies import (
    MovingAverageCrossStrategy,
    RegimeFilteredMovingAverageCross,
)


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
    parser = argparse.ArgumentParser(description="Champion vs challenger comparison (WS 7.11)")
    parser.add_argument("--datasets-dir", default="datasets", help="directory of normalized CSVs")
    parser.add_argument("--out-dir", default="reports", help="directory for report artifacts")
    parser.add_argument("--name", default="champion_vs_challenger", help="report name")
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

    split = PeriodSplitConfig(
        training_ratio=args.training_ratio, validation_ratio=args.validation_ratio
    )
    runner = ChampionChallenger(split=split)

    champion = MovingAverageCrossStrategy(fast=5, slow=21)
    candidates = [
        RegimeFilteredMovingAverageCross(
            allowed_trends=("UP",),
            trend_threshold_pct="0.05",
        ),
        RegimeFilteredMovingAverageCross(
            allowed_trends=("UP", "SIDEWAYS"),
            trend_threshold_pct="0.05",
        ),
    ]
    comparison = runner.run_days(days, champion, candidates, title=args.name)

    report_path = out_dir / f"{args.name}.json"
    html_path = out_dir / f"{args.name}.html"
    report_path.write_text(
        json.dumps(multi_period_comparison_to_dict(comparison), indent=2), encoding="utf-8"
    )
    html_path.write_text(multi_period_comparison_to_html(comparison), encoding="utf-8")

    print(f"compared {champion.name} vs {len(candidates)} challenger(s) over {len(days)} day(s)")
    for label, count in comparison.period_counts.items():
        print(f"  {label}: {count} day(s)")
    overall = comparison.overall
    c_agg = overall.champion.aggregate
    print(
        f"champion total P&L {c_agg.total_pnl:.2f} Rs over {c_agg.num_trades} round trips"
    )
    for entry in overall.entries:
        sign = "beats champion" if entry.delta and entry.delta.beats_champion else "does NOT beat champion"
        print(f"  {entry.name}: {sign}")
    print(f"wrote {report_path} and {html_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())