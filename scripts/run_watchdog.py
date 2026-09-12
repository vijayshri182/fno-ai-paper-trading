"""WS 7.14 CLI: watchdog / health / fail-safe report over paper inputs.

Checks the paper system's inputs (dataset bar sequences, experience store,
model registry) and emits a health report plus a fail-safe decision and alerts.
The decision is data for operators — the watchdog never executes.

    python scripts/run_watchdog.py --datasets-dir datasets --store experience_store \
        --registry model_registry --out-dir reports

By default the freshness check compares the latest dataset bar against a
self-provided "as-of" time, so an offline snapshot reads as current; pass
--as-of YYYY-MM-DDTHH:MM:SS (and --max-bar-age-hours) to enforce a wall-clock
staleness bound instead.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta
from pathlib import Path

from fno_ai_paper_trading.alerting.alerts import AlertCategory, AlertLevel
from fno_ai_paper_trading.alerting.engine import AlertEngine, FileAlertSink
from fno_ai_paper_trading.alerting.health import (
    Watchdog,
    WatchdogRun,
    bar_sequence_is_valid,
)
from fno_ai_paper_trading.data.dataset_store import load_dataset
from fno_ai_paper_trading.persistence.experience_store import ExperienceStore
from fno_ai_paper_trading.promotion.registry import VersionRegistry
from fno_ai_paper_trading.research.report import CSS, escape
from fno_ai_paper_trading.utils.functions import to_decimal


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Watchdog / health report (WS 7.14)")
    parser.add_argument("--datasets-dir", default="datasets")
    parser.add_argument("--store", default=None, help="experience store directory (optional)")
    parser.add_argument("--registry", default=None, help="model registry path or directory (optional)")
    parser.add_argument("--out-dir", default="reports")
    parser.add_argument("--name", default="watchdog")
    parser.add_argument("--as-of", default=None, help="staleness reference time ISO; default = latest dataset bar")
    parser.add_argument("--max-bar-age-hours", type=float, default=5.0, help="max fresh bar age in hours")
    args = parser.parse_args(argv)

    datasets_dir = Path(args.datasets_dir)
    csv_files = sorted(datasets_dir.glob("*.csv"))
    components: dict[str, bool] = {}
    details: dict[str, str] = {}
    latest_bar_time: datetime = datetime.min

    for path in csv_files:
        try:
            dataset = load_dataset(path)
        except Exception as exc:  # noqa: BLE001 - cite the failing dataset
            components[path.stem] = False
            details[path.stem] = f"load failed: {exc}"
            continue
        timestamps = [bar.timestamp for bar in dataset.bars]
        if not timestamps:
            components[path.stem] = False
            details[path.stem] = "no bars in dataset"
            continue
        if not bar_sequence_is_valid(timestamps):
            components[path.stem] = False
            details[path.stem] = "bar timestamps are not strictly chronological/unique"
            continue
        components[path.stem] = True
        details[path.stem] = f"{len(timestamps)} bars"
        latest_bar_time = max(latest_bar_time, max(timestamps))

    if args.store:
        try:
            store = ExperienceStore(directory=args.store, name="experiences")
            components["experience_store"] = True
            details["experience_store"] = f"{store.count} records"
        except Exception as exc:  # noqa: BLE001
            components["experience_store"] = False
            details["experience_store"] = f"load failed: {exc}"

    if args.registry:
        try:
            registry = VersionRegistry(args.registry)
            components["model_registry"] = True
            details["model_registry"] = (
                f"{len(registry.versions)} version(s); "
                f"active {registry.active.strategy_name if registry.active else 'none'}"
            )
        except Exception as exc:  # noqa: BLE001
            components["model_registry"] = False
            details["model_registry"] = f"load failed: {exc}"

    if not csv_files:
        print(f"no datasets found under {datasets_dir}; nothing to watch")
        return 1

    as_of = (
        datetime.fromisoformat(args.as_of)
        if args.as_of
        else (latest_bar_time if latest_bar_time != datetime.min else datetime.now())
    )
    watchdog = Watchdog(
        max_bar_age=timedelta(hours=to_decimal(str(args.max_bar_age_hours))),
        now=lambda: as_of,
    )
    report = watchdog.evaluate(
        latest_bar_time=latest_bar_time if latest_bar_time != datetime.min else as_of,
        components=components,
        details=details,
    )
    safety = watchdog.safety(report)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    engine = AlertEngine(sinks=[FileAlertSink(out_dir / f"{args.name}-alerts.jsonl")])
    generated = [engine.emit(alert) for alert in watchdog.alerts_for(report, safety)]
    run = WatchdogRun(report=report, safety=safety, alerts=tuple(generated))

    json_path = out_dir / f"{args.name}.json"
    html_path = out_dir / f"{args.name}.html"
    json_path.write_text(json.dumps(run.to_dict(), indent=2), encoding="utf-8")

    rows = "\n".join(
        f"<tr><td>{escape(f.component)}</td><td>{escape(f.status.value)}</td>"
        f"<td>{escape(f.message)}</td><td>{escape(f.detail)}</td></tr>"
        for f in report.findings
    )
    alert_rows = "\n".join(
        f"<tr><td>{escape(a.category.value)}</td><td>{escape(a.level.value)}</td>"
        f"<td>{escape(a.title)}</td></tr>"
        for a in run.alerts
    )
    html_path.write_text(
        f"<!doctype html><html><head><meta charset='utf-8'><style>{CSS}</style></head>"
        f"<body><div class='wrap'>"
        f"<header><h1>Watchdog health — {escape(safety.decision.value)}</h1>"
        f"<p>{escape(safety.instruction)}</p></header>"
        f"<div class='note'>PAPER TRADING — NO LIVE ORDER. Watchdog decisions are "
        f"advisory data; they never place orders or change risk controls.</div>"
        f"<section><h2>Decision</h2><pre>{escape(json.dumps(safety.to_dict(), indent=2))}</pre></section>"
        f"<section><h2>Health findings</h2><table><thead><tr><th>Component</th><th>Status</th>"
        f"<th>Message</th><th>Detail</th></tr></thead><tbody>{rows}</tbody></table></section>"
        f"<section><h2>Alerts</h2><table><thead><tr><th>Category</th><th>Level</th>"
        f"<th>Title</th></tr></thead><tbody>{alert_rows or '<tr><td colspan=3>none</td></tr>'}</tbody></table></section>"
        f"<div class='footer'>Watchdog &middot; paper trading only</div></div></body></html>"
    )

    print(f"health {report.status.value} — decision {safety.decision.value}")
    for finding in report.findings:
        print(f"  {finding.component:<24} {finding.status.value:<9} {finding.message}")
    print(f"safety: {safety.instruction}")
    print(f"wrote {json_path} and {html_path} (+ {len(run.alerts)} alert(s))")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())