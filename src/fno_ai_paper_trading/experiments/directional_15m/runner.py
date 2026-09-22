"""CLI driver for the isolated 15-minute directional experiment.

Paper-only by construction: the only broker reachable is ``PaperBroker``
(``is_live=False``); there is no scheduler, no live data path and no token or
credential pathway in this module.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime
from pathlib import Path

from fno_ai_paper_trading.experiments.directional_15m.contract import EXPERIMENT_ID
from fno_ai_paper_trading.experiments.directional_15m.executor import (
    DayAlreadyReported,
    DirectionalOptionsConfig,
    DirectionalOptionsEngine,
)
from fno_ai_paper_trading.experiments.directional_15m.report import (
    baseline_metrics_from_engine,
)
from fno_ai_paper_trading.paper_track.clock import FixedClock
from fno_ai_paper_trading.paper_track.feed import SyntheticFeed, trading_day_sequence
from fno_ai_paper_trading.paper_track.runner import day_ticks, run_sessions
from fno_ai_paper_trading.paper_track.store import TrackStore, stable_dumps

__all__ = ["PAPER_ONLY_BANNER", "replay_experiment", "cmd_replay", "cmd_status", "cmd_report", "cmd_compare", "main"]

PAPER_ONLY_BANNER = (
    f"[{EXPERIMENT_ID}] paper-only experiment - live_trading disabled, "
    "no broker order is ever routed outside PaperBroker."
)


def replay_experiment(
    *,
    config: DirectionalOptionsConfig,
    store: TrackStore,
    feed,
    days: list[date],
    verbose: bool = False,
    failpoints: set[str] | None = None,
    run_id: str | None = None,
    resume: bool = False,
    baseline_metrics: dict | None = None,
) -> DirectionalOptionsEngine:
    """Drive one isolated experiment account across ``days`` at every session tick.

    ``resume=True`` restores the latest checkpoint first (state carries
    forward). A day that already produced a report is refused so a session can
    never be replayed or double-counted.
    """
    clock = FixedClock(datetime(1970, 1, 1))
    engine = DirectionalOptionsEngine(
        config, store=store, clock=clock, bars_source=feed, failpoints=failpoints, run_id=run_id
    )
    if baseline_metrics is not None:
        engine.baseline_metrics = baseline_metrics
    if resume:
        engine.load()
    for day in days:
        if store.load_report(day) is not None:
            raise DayAlreadyReported(
                f"{day.isoformat()} trading day already completed; refusing to re-run it"
            )
    tick_index = 0
    for day in days:
        for tick in day_ticks(day):
            clock.set(tick)
            engine.step()
            tick_index += 1
            if verbose:
                print(f"  {engine._tick_summary(tick)}")
    # The final 15:36 tick finalizes the last day (window_close passed).
    return engine


def _build_config(args) -> DirectionalOptionsConfig:
    return DirectionalOptionsConfig(
        account=args.account,
        store_dir=Path(args.store_dir),
        initial_cash=args.initial_cash,
    )


def cmd_replay(args) -> int:
    print(PAPER_ONLY_BANNER)
    config = _build_config(args)
    store = TrackStore(config.store_dir, config.account)
    days = trading_day_sequence(date.today(), args.days)
    feed = SyntheticFeed.build(days, seed=args.seed, instrument=config.instrument)
    run_id = f"exp-{args.days}d-{args.seed}-{date.today():%Y%m%d}"
    print(f"replaying {args.days} session(s) starting {days[0]} (seed={args.seed}) ...")
    engine = replay_experiment(
        config=config,
        store=store,
        feed=feed,
        days=days,
        verbose=args.verbose,
        failpoints=set(args.failpoint or []),
        run_id=run_id,
    )
    report = engine.report()
    print(f"final state: leg={engine.leg.value} eod={engine.eod_status}")
    print(f"  cash={engine.portfolio.cash:g} realized={engine.portfolio.realized_pnl:g}")
    print(f"  fingerprint={report['fingerprint']}")
    print(f"  reports under {store.reports_dir}")
    return 0


def cmd_status(args) -> int:
    store = TrackStore(Path(args.store_dir), args.account)
    days = sorted(p.name.split(".")[-2] for p in store.reports_dir.glob(f"{args.account}.*.json"))
    if not days:
        print("no experiment reports yet")
        return 2
    print(f"experiment account: {args.account}")
    print(f"reported days: {len(days)}")
    for day in days:
        report = store.load_report(date.fromisoformat(day))
        if report is None:
            continue
        acct = report["accounting"]
        print(
            f"  {day}: net={acct['net_pnl']} cash={acct['cash_end']} "
            f"reconciled={acct['reconciled']} fp={report['fingerprint'][:12]}"
        )
    return 0


def cmd_report(args) -> int:
    store = TrackStore(Path(args.store_dir), args.account)
    day = None
    if args.day is not None:
        day = date.fromisoformat(args.day)
    if day is None:
        days = sorted(p.name.split(".")[-2] for p in store.reports_dir.glob(f"{args.account}.*.json"))
        if not days:
            print("no experiment reports yet")
            return 2
        day = date.fromisoformat(days[-1])
    report = store.load_report(day)
    if report is None:
        print(f"no report for {day.isoformat()}")
        return 2
    print(stable_dumps(report))
    return 0


def cmd_compare(args) -> int:
    """Run the experiment and the frozen MA(5,21) baseline over the *same*
    synthetic sessions, then print a reporting-level comparison. No winner is
    declared (``declared_winner: null``)."""
    print(PAPER_ONLY_BANNER)
    config = _build_config(args)
    days = trading_day_sequence(date.today(), args.days)
    feed = SyntheticFeed.build(days, seed=args.seed, instrument=config.instrument)
    print(f"baseline (frozen MA(5,21), unmodified paper-track) over {len(days)} day(s) ...")

    from fno_ai_paper_trading.paper_track.engine import TrackConfig

    baseline_dir = Path(args.store_dir) / "_baseline"
    baseline_store = TrackStore(baseline_dir, "nifty_5m_daily")
    baseline_config = TrackConfig(
        account=baseline_store.account,
        store_dir=baseline_store.store_dir,
        initial_cash=config.initial_cash,
        interval="5m",
    )
    base_engine = run_sessions(
        config=baseline_config,
        store=baseline_store,
        feed=feed,
        days=days,
        verbose=False,
    )
    base_metrics = baseline_metrics_from_engine(base_engine)

    print(f"experiment ({EXPERIMENT_ID}) over the same {len(days)} day(s) ...")
    store = TrackStore(config.store_dir, config.account)
    run_id = f"exp-{args.days}d-{args.seed}-{date.today():%Y%m%d}-cmp"
    engine = replay_experiment(
        config=config,
        store=store,
        feed=feed,
        days=days,
        verbose=False,
        run_id=run_id,
        baseline_metrics=base_metrics,
    )
    report = engine.report()
    table = report["baseline_comparison"]
    print(stable_dumps(table))
    print(f"experiment fingerprint={report['fingerprint']}")
    print(f"baseline fingerprint={base_metrics['fingerprint']}")
    print("NOTE: no winner is declared; drawdowns use different granularity (see table).")
    return 0


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--account", default="15m_directional_options")
    parser.add_argument("--store-dir", default="data/experiments/15m_directional_options")
    parser.add_argument("--initial-cash", type=int, default=100000)
    parser.add_argument("--seed", type=int, default=20260921)
    parser.add_argument("--days", type=int, default=3)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--failpoint", action="append", default=None)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python scripts/run_15m_directional_experiment.py",
        description="15M_DIRECTIONAL_OPTIONS_EXPERIMENT - isolated, paper-only, deterministic.",
    )
    _add_common(parser)
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("replay", help="paper replay over synthetic sessions, then report")
    sub.add_parser("status", help="observer: summarize persisted reports")
    report_parser = sub.add_parser("report", help="observer: print one persisted report (JSON)")
    report_parser.add_argument("--day", default=None, help="ISO trading day (default: latest)")
    sub.add_parser("compare", help="experiment vs frozen MA(5,21) baseline on same bars")
    args = parser.parse_args(argv)
    command = args.command or "replay"
    if command == "replay":
        return cmd_replay(args)
    if command == "status":
        return cmd_status(args)
    if command == "report":
        return cmd_report(args)
    if command == "compare":
        return cmd_compare(args)
    parser.error(f"unknown command {command!r}")
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())