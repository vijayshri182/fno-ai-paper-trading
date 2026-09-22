"""Command-line runner for the Daily Paper Trading Track.

PAPER-ONLY. This script never places a live order, never enables the Windows
Task Scheduler, and never reads credentials for execution. ``upstox`` uses the
read-only historical-data provider for market bars; every order is simulated by
:class:`~fno_ai_paper_trading.broker.paper_broker.PaperBroker`.
"""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from fno_ai_paper_trading.paper_track.clock import FixedClock
from fno_ai_paper_trading.paper_track.engine import TrackConfig, TrackEngine
from fno_ai_paper_trading.paper_track.errors import PaperTrackError
from fno_ai_paper_trading.paper_track.feed import SyntheticFeed, trading_day_sequence
from fno_ai_paper_trading.paper_track.lock import DEFAULT_STALE_AFTER
from fno_ai_paper_trading.paper_track.report import assert_report_clean
from fno_ai_paper_trading.paper_track.store import TrackStore

PAPER_ONLY_BANNER = "PAPER-ONLY: no live orders; scheduler remains DISABLED — NOT YET AUTHORIZED"


def build_config(args) -> TrackConfig:
    return TrackConfig(
        account=args.account,
        store_dir=Path(args.store_dir),
        initial_cash=Decimal(str(args.initial_cash)),
        interval="5m",
    )


def day_ticks(day: date) -> list[datetime]:
    """Minimal set of instants that fully exercise a session (78 ticks).

    09:14 (pre-open), 09:15 (open boundary), every 5-minute bar completion
    09:20..15:30, then 15:36 (post-close finalize).
    """
    start = datetime(day.year, day.month, day.day, 9, 15, 0)
    instants = [
        datetime(day.year, day.month, day.day, 9, 14, 0),
        datetime(day.year, day.month, day.day, 9, 15, 0),
    ]
    instants += [start + timedelta(minutes=5 * (k + 1)) for k in range(75)]
    instants += [datetime(day.year, day.month, day.day, 15, 36, 0)]
    return instants


class ProviderBarsSource:
    """Adapter feeding a MarketDataProvider's historical 5m bars per tick.

    Explicitly requests the canonical ``5m`` interval so an unset provider
    default can never silently downgrade the engine to daily bars.
    """

    def __init__(self, provider, instrument: str = "NIFTY 50") -> None:
        from fno_ai_paper_trading.paper_track.feed import TRACK_INSTRUMENT

        self.provider = provider
        self.instrument = provider.get_instrument(instrument) or TRACK_INSTRUMENT(instrument)

    def bars_up_to(self, moment: datetime):
        start = moment - timedelta(days=7)
        return self.provider.get_historical_ohlcv(
            self.instrument, interval="5m", start=start, end=moment
        )


def run_sessions(
    *,
    config: TrackConfig,
    store: TrackStore,
    feed,
    days: list[date],
    verbose: bool = False,
    failpoints: set[str] | None = None,
    run_id: str | None = None,
    acquire_lock: bool = True,
    resume: bool = False,
) -> TrackEngine:
    """Drive one cumulative engine across ``days`` at every session tick.

    ``resume=True`` continues an existing persistent account: the latest
    checkpoint is restored first so the next trading day opens on the prior
    closing equity (cash, realized P&L, trade history and bar history all carry
    forward).  A day that already produced a report is refused (once-per-day).
    """
    clock = FixedClock(datetime(1970, 1, 1))
    engine = TrackEngine(
        config, store=store, clock=clock, bars_source=feed, failpoints=failpoints, run_id=run_id
    )
    # Lock liveness is wall-clock-based (the lock file stores real timestamps and
    # staleness is compared against elapsed real time), so the engine's
    # deterministic FixedClock is never used for lock bookkeeping.
    if acquire_lock and not engine.lock.try_acquire(engine.run_id, now=datetime.now()):
        raise RuntimeError(f"another process holds the run lock for {config.account!r}")
    try:
        if resume:
            engine.load(None)  # restore the latest checkpoint (or start fresh)
            for day in days:
                if engine.store.load_report(day) is not None:
                    raise PaperTrackError(
                        f"{day.isoformat()} trading day already completed; refusing to run it twice"
                    )
        tick_index = 0
        for day in days:
            for tick in day_ticks(day):
                clock.set(tick)
                result = engine.step()
                tick_index += 1
                if verbose:
                    print(f"  {result}")
                # Keep the account run-lock alive on long (multi-day) runs so a
                # fast accumulation of simulated time can never look stale.
                if acquire_lock and tick_index % 75 == 0:
                    engine.lock.heartbeat(datetime.now())
        engine.save_report_payload()
    finally:
        if acquire_lock:
            engine.lock.release()
    return engine


def print_summary(engine: TrackEngine) -> None:
    print("final state:")
    print(f"  position={engine.position_quantity} eod_status={engine.eod_status}")
    print(f"  realized_pnl={engine.portfolio.realized_pnl:g} cash={engine.portfolio.cash:g}")
    print(
        f"  fills={len(engine.broker.fills)} max_drawdown={engine.max_intraday_drawdown:g} "
        f"data_skips={len(engine.counters['data_skips'])} errors={len(engine.counters['errors'])}"
    )


def invariant_violations(engine: TrackEngine) -> list[str]:
    from fno_ai_paper_trading.paper_track.invariants import verify_day_end_invariants

    return verify_day_end_invariants(engine)


def cmd_smoke(args) -> int:
    print(PAPER_ONLY_BANNER)
    config = build_config(args)
    store = TrackStore(config.store_dir, config.account)
    day = trading_day_sequence(date.today(), 1)[0]
    feed = SyntheticFeed.build([day], seed=args.seed, instrument=config.instrument)
    engine = run_sessions(
        config=config,
        store=store,
        feed=feed,
        days=[day],
        verbose=args.verbose,
        failpoints=set(args.failpoint or []),
    )
    report = engine.report()
    violations = invariant_violations(engine)
    leaked = assert_report_clean(report)
    if violations or leaked:
        print(f"  INVARIANT VIOLATIONS: {violations}")
        print(f"  REPORT TOKEN LEAKS: {leaked}")
        return 2
    print("smoke OK")
    print(f"  {report['accounting']}")
    print(f"  fingerprint={report['fingerprint']}")
    print(f"  report={store.report_path(day)}")
    print_summary(engine)
    return 0


def cmd_simulate(args) -> int:
    print(PAPER_ONLY_BANNER)
    config = build_config(args)
    store = TrackStore(config.store_dir, config.account)
    days = trading_day_sequence(date.today(), args.days)
    feed = SyntheticFeed.build(days, seed=args.seed, instrument=config.instrument)
    run_id = f"paper-run-{args.days}d-{args.seed}-{date.today():%Y%m%d}"
    print(f"simulating {args.days} session(s) starting {days[0]} across "
          f"{len(days)} trading days ...")
    engine = run_sessions(
        config=config,
        store=store,
        feed=feed,
        days=days,
        verbose=args.verbose,
        run_id=run_id,
    )
    violations = invariant_violations(engine)
    if violations:
        print(f"  INVARIANT VIOLATIONS: {violations}")
        return 2
    if engine.position_quantity != 0:
        print("  ERROR: not flat at end of simulation")
        return 3
    print(f"simulation OK across {args.days} session(s):")
    print_summary(engine)
    return 0


def cmd_list(args) -> int:
    store = TrackStore(Path(args.store_dir), args.account)
    runs = store.list_runs()
    if not runs:
        print("no runs recorded")
        return 0
    for run_id, meta in sorted(runs.items()):
        print(f"{run_id}: {json.dumps(meta, sort_keys=True)}")
    return 0


def cmd_checkpt(args) -> int:
    store = TrackStore(Path(args.store_dir), args.account)
    days = sorted(
        p.name.split(".")[-2]
        for p in store.checkpoints_dir.glob(f"{args.account}.*.json")
        if p.suffix == ".json"
    )
    if not days:
        print("no checkpoints")
        return 0
    for day in days:
        payload = store.load_checkpoint(date.fromisoformat(day))
        if payload is None:
            continue
        print(
            f"{day}: run={payload.get('run_id')} bars={len(payload.get('history', []))} "
            f"eod={payload.get('eod_status')} "
            f"filled={len(payload.get('broker', {}).get('fills', []))}"
        )
    return 0


def cmd_upstox(args) -> int:
    print(PAPER_ONLY_BANNER)
    import os

    token = (os.environ.get("FNO_UPSTOX_ACCESS_TOKEN") or "").strip()
    if not token:
        print(
            "FNO_UPSTOX_ACCESS_TOKEN not set; nothing fetched "
            "(set it in .env, never pass it on any command line)"
        )
        return 1
    from fno_ai_paper_trading.data.instrument_registry import get_research_instrument
    from fno_ai_paper_trading.data.upstox_provider import UpstoxHistoricalDataProvider

    config = build_config(args)
    research = get_research_instrument("NIFTY 50")
    provider = UpstoxHistoricalDataProvider(
        access_token=token, interval="5m", instruments=[research]
    )
    source = ProviderBarsSource(provider, instrument=research.symbol)
    store = TrackStore(config.store_dir, config.account)
    day = trading_day_sequence(date.today(), 1)[0]
    engine = run_sessions(config=config, store=store, feed=source, days=[day], verbose=args.verbose)
    violations = invariant_violations(engine)
    if violations:
        print(f"  INVARIANT VIOLATIONS: {violations}")
        return 2
    if engine.clock.now() == datetime(1970, 1, 1):
        print(
            f"no real bars arrived for {day}; nothing was traded or persisted "
            "(check the token scope and the NIFTY 50 5m instrument)"
        )
        return 2
    print(f"upstox session OK for {day}:")
    print_summary(engine)
    return 0


def _latest_report_day(store) -> date | None:
    days = sorted(
        p.name.split(".")[-2]
        for p in store.reports_dir.glob(f"{store.account}.*.json")
        if p.suffix == ".json"
    )
    return date.fromisoformat(days[-1]) if days else None


def cmd_status(args) -> int:
    """Observer command: last-day summary + lifetime cumulative reconciliation."""
    from fno_ai_paper_trading.paper_track.errors import TrackCheckpointError
    from fno_ai_paper_trading.paper_track.report import build_cumulative_report

    store = TrackStore(Path(args.store_dir), args.account)
    last_day = _latest_report_day(store)
    if last_day is None:
        print(f"no persisted days for account {store.account!r}")
        return 0
    try:
        cum = build_cumulative_report(store)
    except TrackCheckpointError as exc:
        print(f"STATE INVALID: {exc}")
        return 2
    last = store.load_report(last_day)
    accounting = (last or {}).get("accounting") or {}
    print(f"account       = {cum['account']}")
    print(f"span          = {cum['days']} day(s) ({cum['first_day']} .. {cum['last_day']})")
    print(f"start_cash    = {cum['start_cash']}")
    print(f"final_cash    = {cum['final_cash']}")
    print(
        f"lifetime net  = {cum['lifetime']['net_pnl']} "
        f"(gross {cum['lifetime']['gross_pnl']} - costs {cum['lifetime']['costs']})"
    )
    print(
        f"fills         = {cum['cumulative']['fills']} "
        f"entries={cum['cumulative']['entries']} exits={cum['cumulative']['exits']} "
        f"stops={cum['cumulative']['stops']} flattens={cum['cumulative']['flattens']} "
        f"errors={cum['cumulative']['errors']}"
    )
    last_clean = not (accounting.get("violations") or [])
    print(f"last day {last_day}  eod={last.get('eod_status')}  accounting_clean={last_clean}")
    for violation in cum["violations"]:
        print(f"  VIOLATION: {violation}")
    print(f"reconciled    = {cum['reconciled']}")
    print(f"fingerprint   = {cum['fingerprint']}")
    return 0 if cum["reconciled"] else 2


def cmd_report(args) -> int:
    """Observer command: print one persisted day's full paper report (JSON)."""
    from fno_ai_paper_trading.paper_track.errors import TrackCheckpointError

    store = TrackStore(Path(args.store_dir), args.account)
    day = args.day if args.day is not None else _latest_report_day(store)
    if day is None:
        print(f"no persisted reports for account {store.account!r}")
        return 1
    try:
        report = store.load_report(day)
    except TrackCheckpointError as exc:
        print(f"STATE INVALID: {exc}")
        return 2
    if report is None:
        print(f"no report persisted for {day}")
        return 1
    print(json.dumps(report, sort_keys=True, indent=2))
    return 0


def _add_common(parser: argparse.ArgumentParser) -> None:
    """Shared options, registered on the main parser AND every subparser so
    ``<command> --opt value`` works (argparse only honours a subparser's own
    options after the subcommand token)."""
    parser.add_argument("--account", default="nifty_5m_daily")
    parser.add_argument("--store-dir", default="data/paper_trading")
    parser.add_argument("--initial-cash", type=Decimal, default=Decimal("100000"))
    parser.add_argument("--seed", type=int, default=20260921)
    parser.add_argument("--days", type=int, default=1)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument(
        "--failpoint",
        action="append",
        metavar="LETTER",
        help="inject a crash at failpoint A..J (dev/test only)",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python scripts/run_paper_track.py",
        description="Daily Paper Trading Track — paper-only, fail-closed, deterministic.",
    )
    _add_common(parser)
    sub = parser.add_subparsers(dest="command")
    sub_help = {
        "smoke": "one synthetic session, then report",
        "simulate": "N deterministic synthetic sessions",
        "list": "list recorded runs in the manifest",
        "checkpt": "list checkpoints and their integrity",
        "upstox": "one real-5m-bar session (read-only history)",
        "status": "last-day summary + lifetime cumulative reconciliation",
        "report": "print one persisted day's full paper report (JSON)",
    }
    for name, help_text in sub_help.items():
        subparser = sub.add_parser(name, help=help_text)
        _add_common(subparser)
        if name == "report":
            subparser.add_argument(
                "--day", type=date.fromisoformat, default=None,
                help="YYYY-MM-DD (defaults to the latest persisted day)",
            )

    args = parser.parse_args(argv)
    command = args.command or "smoke"
    dispatch = {
        "smoke": cmd_smoke,
        "simulate": cmd_simulate,
        "list": cmd_list,
        "checkpt": cmd_checkpt,
        "upstox": cmd_upstox,
        "status": cmd_status,
        "report": cmd_report,
    }
    handler = dispatch.get(command)
    if handler is None:
        parser.error(f"unknown command {command!r}")
        return 2
    return handler(args)


if __name__ == "__main__":
    raise SystemExit(main())