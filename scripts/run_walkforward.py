"""WS 7.18 CLI: walk-forward adaptive research & learning engine.

Loads every CSV in the datasets directory, chunks bars by trading day, and runs
the chronological walk-forward engine over the research domain ONLY (the domain
is bounded either by ``--last-date`` or by the protected out-of-sample boundary
detected from the five-year replay report / ``--protected-oos-start``). Every
trading day writes one immutable row of the 21-field algorithm evolution ledger.
The run is resumable via a config-hash-guarded checkpoint.

Paper/historical only: this script never enables live trading and never loads
protected out-of-sample days.

Usage:
    python scripts/run_walkforward.py --datasets-dir datasets --out-dir reports/walkforward
"""
from __future__ import annotations

import argparse
import dataclasses
import json
from datetime import date
from pathlib import Path
from typing import Any

from fno_ai_paper_trading.data.dataset_store import load_dataset
from fno_ai_paper_trading.evaluation.five_year import DayBars
from fno_ai_paper_trading.walkforward.config import WalkForwardConfig
from fno_ai_paper_trading.walkforward.engine import WalkForwardEngine
from fno_ai_paper_trading.walkforward.reports import write_reports


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


def detect_protected_oos_start(report: Path) -> date | None:
    """First protected out-of-sample day from a five-year replay report."""
    if not report.is_file():
        return None
    payload = json.loads(report.read_text(encoding="utf-8"))
    for day_row in payload.get("per_day", []):
        if day_row.get("period") == "out_of_sample":
            return date.fromisoformat(str(day_row["day"]))
    return None


def load_overrides(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("--config must contain a JSON object of config fields")
    return dict(raw)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Walk-forward adaptive research engine (WS 7.18)"
    )
    parser.add_argument("--datasets-dir", default="datasets", help="directory of normalized CSVs")
    parser.add_argument("--out-dir", default="reports/walkforward", help="directory for artifacts")
    parser.add_argument("--name", default="walkforward", help="run name / artifact prefix")
    parser.add_argument("--five-year-report", default="reports/five_year_replay.json",
                        help="five-year replay JSON used to auto-detect the protected OOS boundary")
    parser.add_argument("--protected-oos-start", default=None,
                        help="explicit protected OOS start date YYYY-MM-DD (default: auto-detect)")
    parser.add_argument("--first-date", default=None, help="inclusive research start date YYYY-MM-DD")
    parser.add_argument("--last-date", default=None, help="inclusive research end date YYYY-MM-DD")
    parser.add_argument("--max-research-days", type=int, default=None,
                        help="cap the number of research-domain days")
    parser.add_argument("--stop-after-days", type=int, default=None,
                        help="runtime-only limit; resume to continue (not part of the config hash)")
    parser.add_argument("--config", default=None, help="JSON file with WalkForwardConfig field overrides")
    parser.add_argument("--min-evidence-days", type=int, default=None)
    parser.add_argument("--research-cadence-days", type=int, default=None)
    parser.add_argument("--research-window-days", type=int, default=None)
    parser.add_argument("--nightly-review-window-days", type=int, default=None)
    parser.add_argument("--max-challengers-total", type=int, default=None)
    parser.add_argument("--max-challengers-per-round", type=int, default=None)
    parser.add_argument("--validation-window-days", type=int, default=None)
    parser.add_argument("--promotion-cadence-days", type=int, default=None)
    parser.add_argument("--resume", action="store_true", help="resume from the run checkpoint")
    args = parser.parse_args(argv)

    datasets_dir = Path(args.datasets_dir)
    csv_files = sorted(datasets_dir.glob("*.csv"))
    days: list[DayBars] = []
    for path in csv_files:
        days.extend(chunk_by_day(path))
    if not days:
        print(f"no day-chunked data found under {datasets_dir} (no *.csv)")
        return 1

    override = load_overrides(Path(args.config) if args.config else None)
    for flag_name, attr in (
        ("min_evidence_days", None), ("research_cadence_days", None),
        ("research_window_days", None), ("nightly_review_window_days", None),
        ("max_challengers_total", None), ("max_challengers_per_round", None),
        ("validation_window_days", None), ("promotion_cadence_days", None),
    ):
        cli_attr = flag_name.replace("_", "-")
        value = getattr(args, flag_name, None)
        if value is not None:
            override[flag_name] = value

    protected = None
    if args.protected_oos_start:
        protected = date.fromisoformat(args.protected_oos_start)
    else:
        detected = detect_protected_oos_start(Path(args.five_year_report))
        if detected is not None:
            print(f"auto-detected protected OOS start {detected} from "
                  f"{Path(args.five_year_report).name}")
            protected = detected
    if protected is not None:
        override["protected_oos_start"] = protected.isoformat()

    if not override.get("last_date") and not override.get("protected_oos_start"):
        raise SystemExit(
            "refusing to run without a research-domain boundary: pass "
            "--protected-oos-start or --last-date, or ensure the five-year "
            "report exists so the boundary can be auto-detected"
        )

    if args.first_date:
        override["first_date"] = date.fromisoformat(args.first_date).isoformat()
    if args.last_date:
        override["last_date"] = date.fromisoformat(args.last_date).isoformat()
    if args.max_research_days is not None:
        override["max_research_days"] = args.max_research_days

    base = WalkForwardConfig()
    known = {f.name for f in dataclasses.fields(base)}
    unknown = [k for k in override if k not in known]
    if unknown:
        raise SystemExit(f"unknown config field(s): {', '.join(sorted(unknown))}")
    for field_ in dataclasses.fields(base):
        value = override.get(field_.name)
        if value is None:
            continue
        if field_.type in ("date | None", "datetime.date | None", "datetime.date"):
            override[field_.name] = date.fromisoformat(str(value))
    config = dataclasses.replace(base, **override)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    engine = WalkForwardEngine(config)
    result = engine.run(
        days,
        out_dir=out_dir,
        run_name=args.name,
        resume=args.resume,
        stop_after_days=args.stop_after_days,
    )
    write_reports(out_dir, result)

    totals = result.champion_totals
    print(
        f"walked {result.days_processed}/{result.days_available} research days "
        f"({result.days_available} available in domain)"
    )
    print(
        f"champion net P&L {totals.get('net_pnl', 0)} over "
        f"{totals.get('round_trips', 0)} round trips; "
        f"{len(result.promotions)} gate decision(s), "
        f"{sum(1 for p in result.promotions if p.get('decision') == 'PROMOTE')} promotion(s)"
    )
    print(f"wrote reports + ledger + checkpoint to {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())