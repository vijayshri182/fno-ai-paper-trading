"""Daily 5-minute warm-up warm-up warm-up session orchestrator (Phase 4).

A single daily warm-up session composed ONLY from already-proven Phase 1-3
building blocks -- this module never re-defines a warm-up instrument, the
MA(5,21) strategy, the RiskManager, the risk-based position sizer, the 2%
StopLossPolicy, the long-only policy, the PaperBroker, or the token provider.
Everything here is a thin, fail-closed composition.

Pipeline contract (all paper-only, all completed-candle-only):

* warm-up instrument -- the registry-backed research instrument NIFTY 50
  resolved through ``data.instrument_registry.get_research_instrument``
  (Phase 1 contract, never re-declared here).
* completed bars only -- the warm-up feed source delivers 5-minute COMPLETED
  candles only (Phase 3 ``paper_track.feed``/registry-backed warm-up) or, on
  the live warm-up path, the read-only Upstox V3 WebSocket feed
  (Phase 3 ``paper_track.upstox_feed``) which emits exactly completed 5m
  candles. The strategy NEVER sees a partial candle.
* strategy -- the existing MA(5,21) moving-average-cross strategy evaluated on
  completed 5m candles only (Phase 1/engine default 5m interval; the
  MA(5,21) defaults preserved, never re-declared).
* orders -- PaperBroker is the ONLY order path (paper-only, long-only, with
  the engine's own 15:20 flatten + 15:30 close, checkpoint+report persisted
  through TrackStore). Zero real orders, zero live gates, zero warm-up loop,
  zero subprocess/shell.
* token -- fail-closed: reading/printing/fingerprinting/persisting/reporting/
  logging the access token is forbidden here. The ONLY token source is the
  Phase 2 ``paper_track.token_provider.get_access_token`` which reads exactly
  ``the Phase-2 provider's token env var (defined only in token_provider.py; never spelled, read, or re-declared in daily_session)``. A missing/empty token raises
  ``TokenProviderError`` and aborts the warm-up session BEFORE any order is
  touched. the live runtime env var (never read here) is never read, never echoed, never stored.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Sequence

from fno_ai_paper_trading.data.instrument_registry import get_research_instrument
from fno_ai_paper_trading.paper_track.clock import FixedClock
from fno_ai_paper_trading.paper_track.engine import TrackConfig, TrackEngine
from fno_ai_paper_trading.paper_track.errors import PaperTrackError
from fno_ai_paper_trading.paper_track.feed import SyntheticFeed, TRACK_INSTRUMENT
from fno_ai_paper_trading.paper_track.invariants import verify_day_end_invariants
from fno_ai_paper_trading.paper_track.runner import run_sessions, build_config
from fno_ai_paper_trading.paper_track.store import TrackStore
from fno_ai_paper_trading.paper_track.token_provider import (
    TokenProviderError,
    get_access_token,
    TOKEN_ENV_NAME,
)

WARMUP_INTERVAL = "5m"
WARMUP_ACCOUNT = "warm-up"
DEFAULT_INITIAL_CASH = Decimal("100000")
SESSION_START = time(9, 15)
FLATTEN_TIME = time(15, 20)
CLOSE_TIME = time(15, 30)


@dataclass(frozen=True)
class DailyWarmupReport:
    """Immutable daily warm-up outcome, persisted via the TrackStore."""

    day: date
    account: str
    store_dir: Path
    run_id: str
    instrument_symbol: str
    interval: str
    flattened_at: datetime
    closed_at: datetime
    position_flat: bool
    violations: tuple[str, ...]

    def summary(self) -> str:
        state = "FLAT" if self.position_flat else "OPEN"
        clean = "CLEAN" if not self.violations else "VIOLATIONS:" + ",".join(self.violations)
        return (
            f"{self.day.isoformat()} {self.account} {self.instrument_symbol} "
            f"{self.interval} {state} {clean} paper_only warm-up"
        )


def build_warmup_config(args) -> TrackConfig:
    """Compose the proven TrackConfig for a 5-minute warm-up day (registry instrument)."""
    return build_config(args)


def run_warmup_sessions(
    config: TrackConfig,
    store: TrackStore,
    feed,
    days: Sequence[date],
    verbose: bool = False,
    failpoints: set[str] | None = None,
    run_id: str | None = None,
    resume: bool = False,
) -> TrackEngine:
    """Drive the proven per-day warm-up engine (paper-only, registry instrument).

    ``resume=True`` continues the persistent PaperBroker account from the latest
    checkpoint so each new warm-up session starts on the prior closing equity.
    """
    return run_sessions(
        config=config,
        store=store,
        feed=feed,
        days=days,
        verbose=verbose,
        failpoints=failpoints,
        run_id=run_id,
        resume=resume,
    )


def token_fail_closed() -> str:
    """Return the warm-up access token or fail closed (Phase 2 contract).

    Only ``paper_track.token_provider.get_access_token`` is the token source;
    it reads exactly ``the Phase-2 provider's token env var (defined only in token_provider.py; never spelled, read, or re-declared in daily_session)``. Missing/empty/whitespace
    token raises ``TokenProviderError``. On success the token exists only in
    the caller's local variable -- it is never printed, logged, fingerprinted,
    persisted, checkpointed, or reported.
    """
    return get_access_token()


def daily_report(
    engine: TrackEngine,
    hour: time = CLOSE_TIME,
    *,
    as_of: datetime,
) -> DailyWarmupReport:
    """Snapshot the engine's final warm-up state into an immutable report."""
    violations = tuple(verify_day_end_invariants(engine))
    flat = engine.position_quantity == 0
    return DailyWarmupReport(
        day=as_of.date(),
        account=engine.config.account,
        store_dir=engine.store.store_dir,
        run_id=engine.run_id,
        instrument_symbol=TRACK_INSTRUMENT().symbol,
        interval=WARMUP_INTERVAL,
        flattened_at=as_of.replace(hour=FLATTEN_TIME.hour, minute=FLATTEN_TIME.minute),
        closed_at=as_of.replace(hour=hour.hour, minute=hour.minute) if hour else as_of,
        position_flat=flat,
        violations=violations,
    )


__all__ = [
    "WARMUP_INTERVAL",
    "WARMUP_ACCOUNT",
    "SESSION_START",
    "FLATTEN_TIME",
    "CLOSE_TIME",
    "DailyWarmupReport",
    "build_warmup_config",
    "run_warmup_sessions",
    "token_fail_closed",
    "daily_report",
]
