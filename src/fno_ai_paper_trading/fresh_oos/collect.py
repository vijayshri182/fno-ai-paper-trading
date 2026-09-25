"""Command line: one fresh-OOS acquisition pass.

Usage::

    python -m fno_ai_paper_trading.fresh_oos.collect --once
    python -m fno_ai_paper_trading.fresh_oos.collect --once --date 2026-09-17
    python -m fno_ai_paper_trading.fresh_oos.collect --once --max-days 5

``--once`` (the default action) acquires every eligible completed trading day
strictly after 2026-09-11 in chronological order; already-accepted days are
NOOPs (or DATA_CONFLICT on hash drift). ``--date`` forces exactly one date
(still boundary- and integrity-enforced). The command is GET-only historical
data, credential-gated (exits ``AUTH_REQUIRED`` without the Upstox analytics
token), places no orders and never triggers validation. No credentials are ever
printed.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date as _date
from pathlib import Path

# Allow `python src/fno_ai_paper_trading/fresh_oos/collect.py` from the repo
# root without the package being installed (pytest uses the root conftest).
_REPO = Path(__file__).resolve().parents[3]
if str(_REPO / "src") not in sys.path:
    sys.path.insert(0, str(_REPO / "src"))

from fno_ai_paper_trading.fresh_oos import factory  # noqa: E402
from fno_ai_paper_trading.fresh_oos.protocol import (  # noqa: E402
    STATUS_AUTH_REQUIRED,
    STATUS_LOCKED,
    STATUS_PROTOCOL_VIOLATION,
)

# Exit codes: 0 clean / no-op, 1 failure status, 2 usage error.
_EXIT_OK = 0
_EXIT_FAILURE = 1
_EXIT_USAGE = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fresh-OOS collector: acquire NIFTY 50 5m bars strictly after "
        "2026-09-11 (READ-ONLY, no orders, no validation).",
        epilog="Never writes to protected OOS/research data; GET-only historical data.",
    )
    parser.add_argument("--once", action="store_true", help="run one collection pass (default)")
    parser.add_argument("--date", help="force exactly one target date YYYY-MM-DD")
    parser.add_argument("--root", help="immutable fresh-OOS store root")
    parser.add_argument("--datasets-dir", help="established datasets/ directory")
    parser.add_argument("--max-days", help="cap on dates per pass (0 = unlimited)")
    parser.add_argument("--no-verify", action="store_true",
                        help="skip re-fetch verification of already-acquired days (NOOP)")
    parser.add_argument("--json", action="store_true", help="emit a machine-readable summary")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    force_date: _date | None = None
    if args.date:
        try:
            force_date = _date.fromisoformat(args.date)
        except ValueError:
            print(f"invalid --date {args.date!r}; expected YYYY-MM-DD", file=sys.stderr)
            return _EXIT_USAGE

    collector = factory.build_collector(root=args.root, datasets_dir=args.datasets_dir, max_days=args.max_days)
    if args.no_verify:
        collector.config.verify_present = False

    outcome = collector.collect_once(force_date=force_date)

    if args.json:
        print(json.dumps(outcome.to_dict(), indent=2, sort_keys=True))
    else:
        print(f"run_id      : {outcome.run_id}")
        print(f"status      : {outcome.status}")
        print(f"dates       : {', '.join(outcome.dates_attempted) or '(none)'}")
        print(f"accepted    : {', '.join(outcome.accepted) or '(none)'}")
        print(f"noop        : {', '.join(outcome.noop) or '(none)'}")
        print(f"conflicts   : {', '.join(outcome.conflicts) or '(none)'}")
        print(f"message     : {outcome.message}")

    if outcome.status in (STATUS_AUTH_REQUIRED, STATUS_LOCKED, STATUS_PROTOCOL_VIOLATION):
        print(f"exit        : {outcome.status} (no data was modified)", file=sys.stderr)
    return _EXIT_OK if outcome.ok else _EXIT_FAILURE


if __name__ == "__main__":
    raise SystemExit(main())