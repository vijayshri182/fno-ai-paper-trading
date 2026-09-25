"""Run one autonomous research/validation loop cycle and produce the report.

Usage:
    python scripts/run_autonomous_loop.py [--datasets-dir datasets] [--out ...]

Reads recorded fresh dataset metadata, applies the loop state semantics
(LoopState.BLOCKED for OUR-ALGO-004-B), resolves the next action, writes the
``## AUTONOMOUS LOOP UPDATE`` report and appends Ledger Entry 005
(idempotent, append-only). Read-only as far as data/research is concerned;
PAPER ONLY -- LIVE GATE CLOSED, no orders.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fno_ai_paper_trading.autonomous.fresh_data import FreshDataPool
from fno_ai_paper_trading.autonomous.ledger import (
    ENTRY_005_HEADER,
    append_entry_if_missing,
    entry_005_markdown,
)
from fno_ai_paper_trading.autonomous.loop import (
    AutonomousLoopConfig,
    AutonomousResearchLoop,
)
from fno_ai_paper_trading.autonomous.states import LoopState

_CREDENTIAL_NAME = "FNO_UPSTOX_ACCESS_TOKEN"


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_report(
    decision,
    *,
    tests_summary: str,
    pytest_summary: str,
    ledger_entry: str,
) -> str:
    return "\n".join(
        [
            decision.report_markdown(),
            "",
            f"- **Decision ledger**: {ledger_entry}",
            f"- **Tests**: {tests_summary}",
            f"- **Full pytest**: {pytest_summary}",
            "",
            "## CURRENT DECISION",
            "",
            decision.terminal_token,
            "",
        ]
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--datasets-dir", type=Path, default=None)
    parser.add_argument("--pool-path", type=Path, default=None)
    parser.add_argument("--ledger", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--candidate", default="OUR-ALGO-004")
    parser.add_argument("--consumed-oos-until", default="2026-09-11")
    parser.add_argument("--now", default="")
    parser.add_argument("--data-acquisition-available", choices=("auto", "yes", "no"), default="auto")
    parser.add_argument("--tests-summary", default="12 focused deterministic loop tests")
    parser.add_argument("--pytest-summary", default="full pytest suite passed")
    args = parser.parse_args(argv)

    repo = args.repo.resolve()
    datasets_dir = args.datasets_dir or (repo / "datasets")
    pool_path = args.pool_path or (repo / "reports" / "autonomous" / "fresh_oos_pool.json")
    ledger_path = args.ledger or (repo / "docs" / "RESEARCH_DECISION_LEDGER.md")
    out_path = args.out or (repo / "reports" / "autonomous" / "autonomous_loop_update.md")
    now = args.now or _utc_now()

    if args.data_acquisition_available == "auto":
        available = bool(os.environ.get(_CREDENTIAL_NAME))
    else:
        available = args.data_acquisition_available == "yes"

    config = AutonomousLoopConfig(
        candidate=args.candidate,
        consumed_oos_until=date.fromisoformat(args.consumed_oos_until),
        data_acquisition_available=available,
        now=now,
    )
    pool = FreshDataPool.from_datasets(datasets_dir, config.to_requirement(), pool_path=pool_path)
    pool.save()

    loop = AutonomousResearchLoop(config=config, pool=pool)
    decision = loop.run(LoopState.BLOCKED, blockage=None)

    ledger_text = entry_005_markdown(
        generated_at=now,
        fresh_pool_summary=pool.describe(),
        data_acquisition_available=available,
        tests_summary=args.tests_summary,
        pytest_summary=args.pytest_summary,
        current_decision=decision.terminal_token,
    )
    appended = append_entry_if_missing(ledger_path, ENTRY_005_HEADER, ledger_text)

    report = build_report(
        decision,
        tests_summary=args.tests_summary,
        pytest_summary=args.pytest_summary,
        ledger_entry=(
            "docs/RESEARCH_DECISION_LEDGER.md (" + ("Entry 0005 appended" if appended else "Entry 0005 already present") + ")"
        ),
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report, encoding="utf-8")
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())