"""Controlled single-use fresh-OOS validation command.

This is a SEPARATE, human-invoked command.  It is never wired into the
collector, scheduler or any automatic path: without ``--explicit`` every run is
refused, and even with it the run is refused until the pool genuinely reaches
the protocol thresholds (20 trading days / 1500 bars) and the frozen
OUR-ALGO-004 / MA(5,21) parameters verify.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from fno_ai_paper_trading.fresh_oos_validation.protocol import EVIDENCE_LIMITATIONS
from fno_ai_paper_trading.fresh_oos_validation.job import run_controlled_validation

DEFAULT_ROOT = Path("data") / "fresh_oos"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m fno_ai_paper_trading.fresh_oos_validation",
        description="Controlled single-use validation over the fresh-OOS pool.",
    )
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="fresh-OOS store root")
    parser.add_argument(
        "--datasets-dir",
        type=Path,
        default=None,
        help="established datasets dir (default: sibling of root)",
    )
    parser.add_argument(
        "--explicit",
        action="store_true",
        help="MUST be passed to authorize a real controlled run; otherwise refused",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="read-only: print the pool snapshot and the gate's refusal reasons; never consumes",
    )
    args = parser.parse_args(argv)

    if not args.explicit and not args.check:
        parser.error("--explicit is required to run the controlled validation (--check is read-only)")

    outcome = run_controlled_validation(
        args.root,
        explicit_invoke=args.explicit,
        datasets_dir=args.datasets_dir,
    )
    if args.check:
        snapshot = {
            "mode": "CHECK-ONLY",
            "status": outcome.status,
            "dates": list(outcome.dates),
            "bars": outcome.bars,
            "per_day_hashes": outcome.per_day_hashes,
            "reasons": list(outcome.reasons),
            "consumed": outcome.consumed,
        }
        print(json.dumps(snapshot, indent=2, sort_keys=True, default=str))
        status = 0
    else:
        print(json.dumps(outcome.to_dict(), indent=2, sort_keys=True, default=str))
        status = 0 if outcome.status == "SUCCESS" else 1
        if outcome.status != "SUCCESS":
            print("\nVALUE + LIMITATIONS (single-use):", file=sys.stderr)
            for limitation in EVIDENCE_LIMITATIONS:
                print(f"  - {limitation}", file=sys.stderr)
    return status


if __name__ == "__main__":
    raise SystemExit(main())