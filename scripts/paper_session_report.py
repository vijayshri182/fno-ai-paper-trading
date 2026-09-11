"""Render a stored paper-session report (WS 6.6 operations CLI).

Usage::

    python scripts/paper_session_report.py paper_state/2026-09-11_job.json [--html reports/session.html] [--json reports/session.json]

Loads a persisted session payload via ``load_session``, builds an offline
:class:`~fno_ai_paper_trading.services.session_monitoring.SessionReport` and
prints a text summary to stdout. ``--html`` / ``--json`` additionally write the
rendered report (``reports/`` is git-ignored). Purely offline: no network, no
real orders, no secret data.

Scheduling: an OS scheduler (cron / Task Scheduler) invoking this command is
the operator-level "scheduled run" seam for recurring session reporting; the
running session itself is polled in-process by ``PaperSession.run_loop``.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

# Make ``src/`` importable when run directly (keeps the script runnable from any CWD).
SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from fno_ai_paper_trading.persistence.session_store import load_session  # noqa: E402
from fno_ai_paper_trading.services.session_monitoring import (  # noqa: E402
    report_to_dict,
    report_to_html,
)

DEFAULT_DIR = Path("paper_state")


def _print_summary(report) -> None:
    print("Paper session report")
    print(f"  instrument : {report.instrument}")
    print(f"  interval   : {report.interval}")
    print(f"  source     : {report.source}")
    print(f"  generated  : {report.generated_at.isoformat(timespec='seconds')}")
    print(f"  initial    : {report.initial_cash:f}")
    print(f"  cash       : {report.cash:f}")
    print(f"  equity     : {report.equity:f}")
    print(f"  realized   : {report.realized_pnl:f}")
    print(f"  today      : {report.realized_pnl_today:f}")
    print(f"  unrealized : {report.unrealized_pnl:f}")
    print(f"  open qty   : {report.open_quantity}")
    if report.win_rate is not None:
        print(f"  win-rate   : {report.win_rate:f} (wins {report.wins}, losses {report.losses})")
    else:
        print(f"  wins/loss  : {report.wins} / {report.losses}")
    print(
        f"  counters   : consumed={report.consumed_bars} orders={report.orders_submitted} "
        f"fills={report.fills} trades={report.trades} rejects={report.rejections} skips={report.skips}"
    )
    if report.ledger:
        print("  ledger:")
        for row in report.ledger:
            print(
                f"    {row.time.isoformat(timespec='seconds')} "
                f"{row.signal.value if row.signal is not None else '-'} {row.action}"
            )


def _resolve_payload(payload: str, directory: Path) -> Path:
    candidate = Path(payload)
    if candidate.suffix == ".json" and candidate.exists():
        return candidate
    return directory / f"{payload}.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="paper_session_report",
        description="Render an offline report for a stored paper session payload.",
    )
    parser.add_argument(
        "payload",
        help="stored session payload JSON path, or a session name resolved under --dir",
    )
    parser.add_argument("--dir", default=str(DEFAULT_DIR), help="directory for session payloads")
    parser.add_argument("--html", help="optional output path for the labelled HTML report")
    parser.add_argument("--json", help="optional output path for the JSON report")
    args = parser.parse_args(argv)

    payload = _resolve_payload(args.payload, Path(args.dir))
    if not payload.exists():
        parser.error(f"session payload not found: {payload}")

    stored = load_session(payload)
    report = report_from_snapshot(stored.snapshot, when=datetime.now())
    _print_summary(report)

    if args.html:
        from fno_ai_paper_trading.services.session_monitoring import write_html_report

        written = write_html_report(report, args.html)
        print(f"Wrote {written} ({written.stat().st_size} bytes)")

    if args.json:
        target = Path(args.json)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report_to_dict(report), indent=2), encoding="utf-8")
        print(f"Wrote {target} ({target.stat().st_size} bytes)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())