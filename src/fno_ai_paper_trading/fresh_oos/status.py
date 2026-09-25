"""Command line: read-only fresh-OOS status block.

Usage::

    python -m fno_ai_paper_trading.fresh_oos.status [--root DIR] [--datasets-dir DIR]

Prints the current fresh-OOS pool status: boundary, accepted trading days and
bars, first/last accepted date, data readiness
(``DATA_READY_FOR_SINGLE_USE_FRESH_OOS_VALIDATION`` when coverage is met),
validation state (always NOT RUN -- the collector never validates), the per-day
pool and the last recorded run. It reads the manifest and store only; it never
fetches data, never prints credentials, and never writes anything.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
if str(_REPO / "src") not in sys.path:
    sys.path.insert(0, str(_REPO / "src"))

from fno_ai_paper_trading.fresh_oos import factory  # noqa: E402
from fno_ai_paper_trading.fresh_oos.credential_provider import (  # noqa: E402
    RuntimeCredentialProvider,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fresh-OOS collector status (read-only; no network, no writes)."
    )
    parser.add_argument("--root", help="immutable fresh-OOS store root")
    parser.add_argument("--datasets-dir", help="established datasets/ directory")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    collector = factory.build_collector(root=args.root, datasets_dir=args.datasets_dir)
    status = collector.describe_status()

    print("FRESH OOS COLLECTOR STATUS (read-only)")
    print(f"Boundary (exclusive) : {status['boundary_exclusive']}  -- any bar on/before this fails the run")
    print(f"Instrument           : {status['instrument']}")
    print(f"Store                : {status['store_root']}")
    print(f"Datasets dir         : {status['datasets_dir']}")
    credential = RuntimeCredentialProvider().probe()
    print(f"Credential           : {credential.state}  ({credential.message})")
    print(f"Accepted days        : {status['accepted_days']}")
    print(f"Accepted bars        : {status['accepted_bars']}")
    print(f"First accepted date  : {status['first_date'] or '(none)'}")
    print(f"Last accepted date   : {status['last_date'] or '(none)'}")

    readiness = status["readiness"]
    print(f"Data readiness       : {readiness['label']} (days={readiness['days']}, "
          f"bars={readiness['bars']}, missing_days={readiness['missing_days']}, "
          f"missing_bars={readiness['missing_bars']})")
    for reason in readiness["reasons"]:
        print(f"                       - {reason}")
    print(f"Validation           : {status['validation']}  -- waiting on a separate controlled command")
    print(f"Minimum trades       : {readiness['min_trades']}  -- counted only by the validation job")

    print("Pool (accepted days, chronological):")
    if not status["pool"]:
        print("  (none)")
    for row in status["pool"]:
        print(f"  {row['day']}  {row['status']:<20} {row['num_bars']} bars  hash={row['data_hash']}")

    print("Established datasets detected: " + (", ".join(status["established_reused"]) if status["established_reused"] else "(none)"))

    last_run = status["last_run"]
    if last_run:
        print(
            f"Last run              : id={last_run['run_id']} status={last_run['status']} "
            f"({last_run['started_at']} -> {last_run['ended_at']})"
        )
        print(f"                       {last_run['message']}")
    else:
        print("Last run              : (no run recorded yet)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())