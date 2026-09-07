"""Optional real-data connectivity smoke test for the Upstox V3 historical API.

READ-ONLY — NO ORDERS, NO WRITES
-------------------------------
This script is deliberately NOT part of the pytest suite. It only issues
``GET /v3/historical-candle`` requests against live Upstox servers and verifies
normalization + data quality; it never places an order, never posts data, and
never mutates any remote resource. The small fetched window is saved to
``datasets/`` (git-ignored) via the standard dataset store.

It exits non-zero when the access token is missing or a live call fails, so it
can be wired into CI as an explicit, opt-in connectivity check.

Prerequisites: ``UPSTOX_ACCESS_TOKEN`` in the environment (or ``--token``) and
network access to ``api.upstox.com``.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv

# Allow running via `python scripts/upstox_smoke_test.py` from the repo root.
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from fno_ai_paper_trading.data.dataset_store import save_dataset  # noqa: E402
from fno_ai_paper_trading.data.upstox_provider import (  # noqa: E402
    UpstoxHistoricalDataProvider,
    upstox_instrument_key,
)
from fno_ai_paper_trading.data.validation import (  # noqa: E402
    format_report,
    validate_bars,
)
from fno_ai_paper_trading.models.enums import InstrumentType  # noqa: E402
from fno_ai_paper_trading.models.instruments import Instrument  # noqa: E402

DEFAULT_INSTRUMENT_KEY = "NSE_INDEX|Nifty 50"


def _build_instrument(key: str) -> Instrument:
    try:
        segment, symbol = key.split("|", 1)
    except ValueError as exc:
        raise SystemExit(f"instrument key must be SEGMENT|SYMBOL, got {key!r}") from exc
    return Instrument(
        symbol=symbol.strip(),
        instrument_type=InstrumentType.FUTURE,  # nominal type; data is index OHLCV
        underlying_symbol=symbol.strip(),
        exchange=segment.split("_")[0].strip() or "NSE",
        exchange_token=key,
        tick_size=Decimal("0.05"),
        multiplier=1,
        lot_size=1,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Upstox historical-data connectivity smoke test (READ-ONLY).",
        epilog="This script places no orders and never posts data.",
    )
    parser.add_argument(
        "--instrument-key",
        default=os_env("UPSTOX_TEST_INSTRUMENT_KEY", DEFAULT_INSTRUMENT_KEY),
        help=f"Upstox instrument key SEGMENT|SYMBOL (default: {DEFAULT_INSTRUMENT_KEY})",
    )
    parser.add_argument(
        "--interval", default="1d", help="bar interval token, e.g. 1d, 1h, 5m, 1w"
    )
    parser.add_argument(
        "--days", type=int, default=30, help="look-back window in calendar days"
    )
    parser.add_argument(
        "--token",
        default="",
        help="Upstox access token (default: UPSTOX_ACCESS_TOKEN env var)",
    )
    parser.add_argument(
        "--outdir",
        default="datasets",
        help="directory for the saved dataset (default: datasets)",
    )
    parser.add_argument("--no-save", action="store_true", help="validate only, do not save")
    args = parser.parse_args(argv)

    load_dotenv()
    token = args.token or os_env("UPSTOX_ACCESS_TOKEN", "")
    if not token:
        parser.error(
            "no Upstox access token; set UPSTOX_ACCESS_TOKEN (or pass --token). "
            "This script is opt-in — it never runs without credentials."
        )

    key = upstox_instrument_key(*(args.instrument_key.split("|", 1)))
    instrument = _build_instrument(key)

    print("Upstox historical-data smoke test — READ-ONLY (no orders, no writes)")
    print(f"instrument key : {key}")
    print(f"interval       : {args.interval}")

    provider = UpstoxHistoricalDataProvider(access_token=token)
    now = datetime.now()
    start = now - timedelta(days=args.days)
    bars = provider.get_historical_ohlcv(instrument, args.interval, start, now)
    print(f"candles fetched: {len(bars)}")

    report = validate_bars(bars)
    print("data quality   :", format_report(report))
    if not report.ok:
        print("dataset failed validation; refusing to save.", file=sys.stderr)
        return 2

    if bars:
        sample = bars[:3] + bars[-2:]
        for bar in sample:
            print(
                f"  {bar.timestamp.isoformat()}  O={bar.open} H={bar.high} "
                f"L={bar.low} C={bar.close} V={bar.volume} OI={bar.open_interest}"
            )
        if not args.no_save:
            saved = save_dataset(
                bars,
                instrument=instrument,
                provider="upstox",
                interval=args.interval,
                directory=args.outdir,
            )
            print(f"saved          : {saved.path}")
            print(f"data hash      : {saved.data_hash}")
    print("OK — connectivity and normalization verified (no orders placed).")
    return 0


def os_env(name: str, default: str) -> str:
    import os

    return os.getenv(name, default).strip()


if __name__ == "__main__":
    raise SystemExit(main())