"""Phase-5 daily server seam -- one paper-only warm-up day (composition-only).

This module is the thin command seam of the daily warm-up.  It composes ONLY
the already-proven Phase-1..4 building blocks and never re-defines the
tracking instrument, the MA(5,21) strategy, the FNO risk manager, the
risk-based position sizer, the 2% stop-loss, the long-only policy, or the
PaperBroker.

Everything needed to run and report one warm-up day is delegated to the
Phase-4 daily orchestrator (``daily_session.run_warmup_sessions`` +
``daily_session.daily_report``): the 09:15 session start, the 15:20 flatten
and the 15:30 close live inside that orchestrator, together with every
invariant check, the checkpoint/report persistence through TrackStore, and
PaperBroker-only order filling.

Paper-only / fail-closed invariants (never weakened):

* the warm-up token comes ONLY from the Phase-2 provider seam
  (``daily_session.get_access_token``); it is never read, printed, logged,
  fingerprinted, persisted, checkpointed, or reported in this module;
* live market data is delivered ONLY through the Phase-3 read-only Upstox V3
  client (``paper_track.upstox_feed``) via the ``feed`` seam of
  ``run_warmup_sessions``; this module never opens a socket and never
  re-implements a feed transport;
* a completed warm-up day is refused before it can run twice -- the existing
  TrackStore report/checkpoint/lock layout is reused so no second persistence
  system exists;
* this seam stays isolated from the live order layer, from recurring jobs,
  and from live credentials, and it never enables automatic operation.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime
import pathlib
from decimal import Decimal
from typing import Any, Sequence

from fno_ai_paper_trading.paper_track import daily_session as warmup
from fno_ai_paper_trading.paper_track.errors import PaperTrackError
from fno_ai_paper_trading.paper_track.feed import trading_day_sequence
from fno_ai_paper_trading.paper_track.store import TrackStore

DAY_COMPLETED_MSG = "trading day already completed; refusing to run it twice"


@dataclasses.dataclass
class LiveDailyConfig:
    """Offline-config switch carrying the warm-up composition parameters.

    All fields default to the already-proven Phase-4 warm-up defaults so the
    live-daily command never re-invents the instrument, the account, or the
    strategy parameters.
    """

    account: str = warmup.WARMUP_ACCOUNT
    store_dir: pathlib.Path = pathlib.Path("data/paper_trading")
    interval: str = warmup.WARMUP_INTERVAL
    initial_cash: Decimal = warmup.DEFAULT_INITIAL_CASH
    seed: int = 20260921
    day: datetime.date | None = None
    verbose: bool = False


def build_live_config(args: Any) -> LiveDailyConfig:
    """Compose the warm-up configuration from parsed CLI arguments."""
    return LiveDailyConfig(
        account=getattr(args, "account", None) or warmup.WARMUP_ACCOUNT,
        store_dir=pathlib.Path(getattr(args, "store_dir", None) or "data/paper_trading"),
        interval=getattr(args, "interval", None) or warmup.WARMUP_INTERVAL,
        initial_cash=Decimal(
            str(getattr(args, "initial_cash", None) or warmup.DEFAULT_INITIAL_CASH)
        ),
        seed=int(getattr(args, "seed", None) or 20260921),
        day=getattr(args, "day", None),
        verbose=bool(getattr(args, "verbose", False)),
    )


def _warmup_config(config: LiveDailyConfig):
    """Build the Phase-4 warm-up ``TrackConfig`` without re-defining anything."""
    return warmup.build_warmup_config(
        argparse.Namespace(
            account=config.account,
            store_dir=str(config.store_dir),
            initial_cash=config.initial_cash,
            interval=config.interval,
            day=config.day,
            verbose=config.verbose,
        )
    )


def research_instrument():
    """Registry-backed NIFTY 50 research instrument (Phase-1 contract)."""
    return warmup.get_research_instrument("NIFTY 50")


def day_completed(store, day: datetime.date) -> bool:
    """True when ``day`` already produced a persisted warm-up report."""
    return store.load_report(day) is not None


def token_fail_closed() -> str:
    """Fail-closed warm-up token; ONLY the Phase-2 provider seam is used.

    A missing/empty/whitespace token raises ``TokenProviderError`` before any
    warm-up day can start; on success the token lives only in the caller's
    local variable and is never printed, logged, persisted, or reported.
    """
    return warmup.get_access_token()


def run_live_daily(
    *,
    config: LiveDailyConfig,
    store=None,
    feed=None,
    day: datetime.date,
    verbose: bool = False,
    failpoints: set[str] | None = None,
    run_id: str | None = None,
):
    """Drive exactly one warm-up day through the Phase-4 orchestrator.

    ``store`` and ``feed`` are injectable (tests replace both).  The default
    feed is the deterministic Phase-3 ``SyntheticFeed`` so the command stays
    fully offline; the Phase-3 live Upstox V3 source plugs in at this same
    ``feed`` seam.  A day whose report already exists is refused (once-per-day).

    The day resumes the persistent account (Phase 6): the latest checkpoint is
    restored so the new session starts on the prior closing equity instead of
    accidentally resetting the account capital.
    """
    store = store or TrackStore(config.store_dir, config.account)
    if day_completed(store, day):
        raise PaperTrackError(f"{day.isoformat()} {DAY_COMPLETED_MSG}")
    if feed is None:
        feed = warmup.SyntheticFeed.build(
            [day], seed=config.seed, instrument=warmup.TRACK_INSTRUMENT()
        )
    return warmup.run_warmup_sessions(
        config=_warmup_config(config),
        store=store,
        feed=feed,
        days=[day],
        verbose=verbose,
        failpoints=failpoints,
        run_id=run_id,
        resume=True,
    )


def _date_arg(value: str) -> datetime.date:
    return datetime.date.fromisoformat(value)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python scripts/run_paper_track.py live-daily",
        description="One paper-only warm-up day for the NSE 5-minute track.",
    )
    parser.add_argument("--account", default=warmup.WARMUP_ACCOUNT)
    parser.add_argument("--store-dir", default="data/paper_trading")
    parser.add_argument("--interval", default=warmup.WARMUP_INTERVAL)
    parser.add_argument("--initial-cash", type=Decimal, default=warmup.DEFAULT_INITIAL_CASH)
    parser.add_argument("--seed", type=int, default=20260921)
    parser.add_argument(
        "--day",
        type=_date_arg,
        default=None,
        help="NSE trading date (default: next trading day)",
    )
    parser.add_argument("--verbose", action="store_true")
    return parser


def cmd_live_daily(argv: Sequence[str] | None = None) -> int:
    """``live-daily`` CLI handler -- one paper-only warm-up day, PaperBroker-only."""
    args = _parser().parse_args(list(argv) if argv else [])
    config = build_live_config(args)
    store = TrackStore(config.store_dir, config.account)
    day = config.day or trading_day_sequence(datetime.date.today(), 1)[0]
    if day_completed(store, day):
        print(f"  {day.isoformat()} {DAY_COMPLETED_MSG}")
        return 4
    engine = run_live_daily(config=config, store=store, day=day, verbose=config.verbose)
    report = warmup.daily_report(
        engine,
        hour=warmup.CLOSE_TIME,
        as_of=datetime.datetime.combine(day, warmup.CLOSE_TIME),
    )
    print(f"  day           = {day.isoformat()}")
    print(f"  account       = {config.account}")
    print(f"  instrument    = {engine.symbol}")
    print(f"  position_flat = {report.position_flat}")
    print(f"  violations    = {','.join(report.violations) or 'none'}")
    print(f"  report        = {store.report_path(day)}")
    return 0


__all__ = [
    "DAY_COMPLETED_MSG",
    "LiveDailyConfig",
    "build_live_config",
    "cmd_live_daily",
    "day_completed",
    "research_instrument",
    "run_live_daily",
    "token_fail_closed",
]