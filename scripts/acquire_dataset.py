"""Fetch -> validate -> store real historical market data (Upstox, READ-ONLY).

This is the production acquisition path for real research data: it pulls
normalized OHLCV bars from the read-only Upstox V3 ``/v3/historical-candle``
endpoint, runs the dataset validator, and (unless ``--no-save``) persists them
via the standard dataset store (CSV + ``.meta.json`` with a SHA-256
``data_hash``) into the git-ignored ``datasets/`` directory.

SAFETY: this script never places an order, never posts data, and never mutates
a remote resource. The only HTTP method ever used is ``GET`` against the
historical-candle endpoint. Like the smoke test it is opt-in: it exits non-zero
unless ``UPSTOX_ACCESS_TOKEN`` is set.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

# Allow running via `python scripts/acquire_dataset.py` from the repo root.
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from fno_ai_paper_trading.data.dataset_store import save_dataset  # noqa: E402
from fno_ai_paper_trading.data.instrument_registry import (  # noqa: E402
    get_research_instrument,
    instrument_from_upstox_key,
)
from fno_ai_paper_trading.data.upstox_provider import (  # noqa: E402
    UpstoxHistoricalDataProvider,
)
from fno_ai_paper_trading.data.validation import (  # noqa: E402
    format_report,
    validate_bars,
    validation_to_dict,
)

DEFAULT_INSTRUMENT = "NIFTY 50"


def _resolve_instrument(literal: str):
    if "|" in (literal or ""):
        return instrument_from_upstox_key(literal)
    return get_research_instrument(literal)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fetch, validate and store real historical data (Upstox, READ-ONLY).",
        epilog="This script places no orders and never posts data.",
    )
    parser.add_argument(
        "--instrument",
        default=DEFAULT_INSTRUMENT,
        help=(
            f"research instrument name (e.g. {DEFAULT_INSTRUMENT}, BANKNIFTY, FINNIFTY) "
            "or a raw Upstox key SEGMENT|SYMBOL"
        ),
    )
    parser.add_argument(
        "--interval", default="1d", help="bar interval token, e.g. 1m, 5m, 1h, 1d, 1w"
    )
    parser.add_argument("--start", help="start date YYYY-MM-DD (inclusive)")
    parser.add_argument("--end", help="end date YYYY-MM-DD (inclusive)")
    parser.add_argument(
        "--days", type=int, help="look-back window in calendar days (alternative to --start)"
    )
    parser.add_argument(
        "--token",
        default="",
        help="Upstox access token (default: UPSTOX_ACCESS_TOKEN env var)",
    )
    parser.add_argument(
        "--outdir", default="datasets", help="directory for the saved dataset (default: datasets)"
    )
    parser.add_argument(
        "--name", default=None, help="optional dataset name (default: auto from symbol/interval/dates)"
    )
    parser.add_argument("--json", action="store_true", help="emit a machine-readable JSON summary")
    parser.add_argument("--no-save", action="store_true", help="validate only, do not save")
    args = parser.parse_args(argv)

    load_dotenv()
    token = (args.token or os.getenv("UPSTOX_ACCESS_TOKEN", "") or "").strip()
    if not token:
        parser.error(
            "no Upstox access token; set UPSTOX_ACCESS_TOKEN (or pass --token). "
            "This script is opt-in — it never runs without credentials."
        )

    if (args.start or "") and args.days is not None:
        parser.error("use either --start or --days, not both")
    if (args.end or "") and args.days is not None:
        parser.error("cannot combine --end with --days")
    if args.days is not None and args.days < 0:
        parser.error("--days must be >= 0")

    try:
        instrument = _resolve_instrument(args.instrument)
    except (KeyError, ValueError) as exc:
        parser.error(str(exc))

    now = datetime.now()
    if args.start:
        start = datetime.fromisoformat(args.start)
    elif args.days is not None:
        start = now - timedelta(days=args.days)
    else:
        start = now - timedelta(days=30)
    start = start.replace(tzinfo=None)
    end = datetime.fromisoformat(args.end).replace(tzinfo=None) if args.end else now

    provider = UpstoxHistoricalDataProvider(access_token=token)
    bars = provider.get_historical_ohlcv(instrument, args.interval, start, end)
    report = validate_bars(bars, allow_empty=False)

    saved_meta = None
    exit_code = 0
    if not report.ok:
        exit_code = 2
    elif not args.no_save:
        saved = save_dataset(
            bars,
            instrument=instrument,
            provider="upstox",
            interval=args.interval,
            directory=args.outdir,
            name=args.name,
        )
        saved_meta = saved.metadata

    summary = {
        "instrument": {
            "name": args.instrument,
            "symbol": instrument.symbol,
            "instrument_type": instrument.instrument_type.value,
            "underlying_symbol": instrument.underlying_symbol,
            "exchange_token": instrument.exchange_token,
        },
        "interval": args.interval,
        "start": start.date().isoformat(),
        "end": end.date().isoformat(),
        "num_bars": len(bars),
        "validation": validation_to_dict(report),
        "saved": saved_meta,
    }
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True, default=str))
    else:
        print(
            f"instrument    : {instrument.symbol} ({instrument.exchange_token})"
        )
        print(f"interval      : {args.interval}")
        print(f"range         : {start.date()} .. {end.date()} ({len(bars)} bars)")
        print(f"data quality  : {format_report(report)}")
        if saved_meta:
            print(
                f"saved         : {saved_meta['start_date']}..{saved_meta['end_date']} "
                f"({saved_meta['num_bars']} bars)"
            )
            print(f"data hash     : {saved_meta['data_hash']}")

    if exit_code != 0:
        print("dataset failed validation; refusing to save.", file=sys.stderr)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())