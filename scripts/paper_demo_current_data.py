"""Generate an HTML paper-session report over real or synthetic market data.

Current-data demonstration runner for the V1 paper session. It follows the
documented "ingest once, replay offline" separation used by the research
pipeline (``research/real_data.py`` deliberately never fetches data itself):
the script optionally fetches historical OHLCV candles (read-only, Upstox V3),
validates and pins them under ``datasets/``, then replays the bars through the
existing deterministic ``PaperSession`` / ``InMemoryMarketDataProvider`` path
and renders the standard HTML report under ``reports/``.

Modes
-----
* ``--smoke``              deterministic synthetic bars (MA-crossing series;
                            no network, no credentials) that prove the wiring.
* ``--day YYYY-MM-DD``     fetch one trading day of ``--interval`` bars for the
                            NIFTY 50 index via ``UpstoxHistoricalDataProvider``.
* ``--csv <dataset.csv>``  offline replay of a previously saved dataset CSV.

Safety
------
* READ-ONLY: only ``GET /v3/historical-candle`` is issued; the Upstox provider
  has no order path and every order executes through ``PaperBroker`` only.
* Fail-closed: real ingestion refuses to run without ``UPSTOX_ACCESS_TOKEN``.
* The demo never writes ``paper_state/`` (no ``save_session`` call), so the
  test suite's "no persistence artefacts" assertions are untouched.
* Outputs go to ``datasets/`` and ``reports/`` (both git-ignored). This script
  is deliberately NOT part of the pytest suite (same convention as
  ``scripts/upstox_smoke_test.py``).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path

from dotenv import load_dotenv

# Make ``src/`` importable when run directly (same pattern as the other scripts).
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from fno_ai_paper_trading.config.settings import (  # noqa: E402
    Environment,
    PaperSettings,
    load_settings,
)
from fno_ai_paper_trading.data.dataset_store import (  # noqa: E402
    load_dataset,
    save_dataset,
)
from fno_ai_paper_trading.data.instrument_registry import get_research_instrument  # noqa: E402
from fno_ai_paper_trading.data.intervals import canonical_interval, interval_minutes  # noqa: E402
from fno_ai_paper_trading.data.market_hours import (  # noqa: E402
    is_trading_day,
    market_phase,
    next_open,
)
from fno_ai_paper_trading.data.mock_provider import (  # noqa: E402
    InMemoryMarketDataProvider,
    build_crossing_ohlcv,
)
from fno_ai_paper_trading.data.upstox_provider import (  # noqa: E402
    UpstoxHistoricalDataProvider,
)
from fno_ai_paper_trading.data.validation import format_report, validate_bars  # noqa: E402
from fno_ai_paper_trading.models.enums import MarketPhase  # noqa: E402
from fno_ai_paper_trading.models.instruments import Instrument  # noqa: E402
from fno_ai_paper_trading.models.market import MarketPrice  # noqa: E402
from fno_ai_paper_trading.services.paper_session import PaperSession  # noqa: E402
from fno_ai_paper_trading.services.session_monitoring import (  # noqa: E402
    build_report,
    report_to_dict,
    write_html_report,
)
from fno_ai_paper_trading.strategies.moving_average_cross import MovingAverageCrossStrategy  # noqa: E402

OPEN_TIME = time(9, 15)
CLOSE_TIME = time(15, 30)
DEFAULT_INTERVAL = "5m"


def _choose_when(last_bar: datetime, minutes: int) -> datetime:
    """A deterministic session decision time that completes every bar.

    Prefers ``last_bar + interval`` when that still falls inside the NSE OPEN
    phase on a trading day; otherwise falls back to a lunch-time check on the
    next trading open (naive IST, matching the domain models).
    """
    candidate = last_bar + timedelta(minutes=minutes)
    if is_trading_day(candidate) and market_phase(candidate) is MarketPhase.OPEN:
        return candidate
    return next_open(candidate) + timedelta(hours=3)


def _print_summary(report) -> None:
    print("Paper session report")
    print(f"  instrument : {report.instrument} @ {report.interval}")
    print(f"  data source: {report.data_source}")
    print(f"  replay mode: {report.replay_mode}")
    print(f"  live orders: {'Yes' if report.live_orders else 'No'}")
    print(f"  generated  : {report.generated_at.isoformat(timespec='seconds')}")
    print(f"  initial    : {report.initial_cash:f}")
    print(f"  cash       : {report.cash:f}")
    print(f"  equity     : {report.equity:f}")
    print(f"  realized   : {report.realized_pnl:f}")
    print(f"  today      : {report.realized_pnl_today:f}")
    print(f"  unrealized : {report.unrealized_pnl:f}")
    print(f"  open qty   : {report.open_quantity}")
    if report.return_pct is not None:
        print(f"  return %   : {report.return_pct:f}")
    if report.max_drawdown is not None:
        print(f"  max drawdown: {report.max_drawdown:f} ({report.max_drawdown_pct:f}%)")
    if report.win_rate is not None:
        print(
            f"  win-rate   : {report.win_rate:f} "
            f"(wins {report.wins}, losses {report.losses})"
        )
    else:
        print(f"  wins/loss  : {report.wins} / {report.losses}")
    print(
        f"  counters   : consumed={report.consumed_bars} round_trips={report.round_trips} "
        f"trade_events={report.trades} orders={report.orders_submitted} fills={report.fills} "
        f"rejects={report.rejections} "
        f"skips={report.skips}"
    )
    if report.ledger:
        print("  ledger:")
        for row in report.ledger:
            print(
                f"    {row.time.isoformat(timespec='seconds')} "
                f"{row.signal.value if row.signal is not None else '-'} {row.action}"
            )


def _run_session(
    instrument: Instrument,
    bars: list[MarketPrice],
    *,
    settings: PaperSettings,
    interval: str,
    quantity: int,
    when: datetime,
    out_html: str | None,
    out_json: str | None,
    data_source: str,
) -> int:
    """Replay ``bars`` through the existing PaperSession path and render reports."""
    provider = InMemoryMarketDataProvider(
        instruments=[instrument], history={instrument.symbol: bars}
    )
    strategy = MovingAverageCrossStrategy()
    sandbox = settings.environment in (Environment.TEST, Environment.DEVELOPMENT)

    session = PaperSession(
        settings,
        provider,
        strategy,
        quantity=quantity,
        clock=lambda: when,
        interval=interval,
        allow_sandbox=sandbox,
    )
    session.start()
    result = session.run_once(when)
    if result.provider_error:
        print(f"provider error: {result.provider_error}", file=sys.stderr)
        return 1

    report = build_report(
        session,
        [result],
        when=when,
        data_source=data_source,
        replay_mode="Offline paper replay",
        live_orders=False,
    )
    _print_summary(report)

    if out_html:
        written = write_html_report(report, out_html)
        print(f"html report    : {written} ({written.stat().st_size} bytes)")

    if out_json:
        target = Path(out_json)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(report_to_dict(report), indent=2), encoding="utf-8"
        )
        print(f"json report    : {target} ({target.stat().st_size} bytes)")

    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="paper_demo_current_data",
        description="Paper-session report over real or synthetic market data.",
        epilog="READ-ONLY demo: no live orders, no persistent session state.",
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--smoke", action="store_true", help="deterministic synthetic smoke run (no network)"
    )
    source.add_argument(
        "--day", metavar="YYYY-MM-DD", help="trading day to fetch from Upstox"
    )
    source.add_argument(
        "--csv", metavar="FILE", help="saved dataset CSV to replay offline"
    )
    parser.add_argument(
        "--interval",
        default=DEFAULT_INTERVAL,
        help=f"canonical bar interval token (default: {DEFAULT_INTERVAL})",
    )
    parser.add_argument(
        "--token",
        default="",
        help="Upstox access token (default: UPSTOX_ACCESS_TOKEN env var)",
    )
    parser.add_argument(
        "--outdir", default="datasets", help="directory for saved datasets (default: datasets)"
    )
    parser.add_argument(
        "--quantity", type=int, default=1, help="quantity per order (default: 1)"
    )
    parser.add_argument("--html", help="optional output path for the HTML report")
    parser.add_argument("--json", help="optional output path for the JSON report")
    args = parser.parse_args(argv)

    load_dotenv()
    settings = load_settings()

    interval = canonical_interval(args.interval)
    if interval is None:
        parser.error(f"unknown interval {args.interval!r}; expected a canonical bar interval")
    minutes = interval_minutes(interval)
    if minutes is None:
        parser.error("the session interval must be a fixed-minute interval")

    instrument = get_research_instrument("NIFTY 50")
    label = ""
    data_source = ""

    if args.smoke:
        bars = build_crossing_ohlcv(instrument)
        label = "smoke (synthetic, no network)"
        data_source = "Synthetic smoke data"
        print("Paper demo — deterministic smoke run (no network, no credentials)")
    elif args.day:
        try:
            day = date.fromisoformat(args.day)
        except ValueError as exc:
            parser.error(f"invalid --day {args.day!r}; expected YYYY-MM-DD")

        token = args.token or os.getenv("UPSTOX_ACCESS_TOKEN", "").strip()
        if not token:
            parser.error(
                "no Upstox access token; set UPSTOX_ACCESS_TOKEN (or pass --token). "
                "The current-data demo never runs without credentials."
            )

        provider = UpstoxHistoricalDataProvider(access_token=token)
        print("Paper demo — Upstox historical data (READ-ONLY, no orders)")
        start = datetime(day.year, day.month, day.day, OPEN_TIME.hour, OPEN_TIME.minute)
        end = datetime(day.year, day.month, day.day, CLOSE_TIME.hour, CLOSE_TIME.minute)
        bars = provider.get_historical_ohlcv(instrument, interval, start, end)
        if not bars:
            print(f"no bars returned for {day} (holiday or no data?)", file=sys.stderr)
            return 2

        print(f"candles fetched: {len(bars)}")
        report = validate_bars(bars, interval_minutes=minutes)
        print("data quality   :", format_report(report))
        if not report.ok:
            print("dataset failed validation; refusing to save or replay.", file=sys.stderr)
            return 2

        saved = save_dataset(
            bars,
            instrument=instrument,
            provider="upstox",
            interval=interval,
            directory=args.outdir,
        )
        print(f"saved          : {saved.path}")
        print(f"data hash      : {saved.data_hash}")
        label = f"upstox {day.isoformat()}"
        data_source = "Upstox historical data"
    else:
        stored = load_dataset(args.csv)
        bars = stored.bars
        instrument = stored.instrument()
        print(f"loaded         : {args.csv}")
        print(f"data hash      : {stored.data_hash}")
        report = validate_bars(bars, interval_minutes=minutes)
        print("data quality   :", format_report(report))
        if not report.ok:
            print("dataset failed validation; refusing to replay.", file=sys.stderr)
            return 2
        label = "offline CSV replay"
        provider_name = str(stored.metadata.get("provider", "saved")).strip()
        data_source = (
            "Upstox historical data"
            if provider_name.casefold() == "upstox"
            else f"Saved {provider_name} historical data"
        )

    if not bars:
        print("no bars to replay; nothing to do.", file=sys.stderr)
        return 2

    sample = bars[:3] + bars[-2:]
    print(f"source         : {label}")
    for bar in sample:
        print(
            f"  {bar.timestamp.isoformat()}  O={bar.open} H={bar.high} "
            f"L={bar.low} C={bar.close} V={bar.volume}"
        )

    when = _choose_when(bars[-1].timestamp, minutes)
    print(f"decision time  : {when.isoformat()} IST (completed-bar replay)")

    return _run_session(
        instrument,
        bars,
        settings=settings,
        interval=interval,
        quantity=args.quantity,
        when=when,
        out_html=args.html,
        out_json=args.json,
        data_source=data_source,
    )


if __name__ == "__main__":
    raise SystemExit(main())
