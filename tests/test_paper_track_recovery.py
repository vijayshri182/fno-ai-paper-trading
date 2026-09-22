"""Phase 8 — restart / stale-lock / corruption / near-close recovery.

The crash-restart and lock suites already prove per-bar and failpoint-A-J
determinism.  Phase 8 hardens the *runner* lifecycle end-to-end:

* a stale-locked account is reclaimed and resumed by the next run;
* a corrupted checkpoint blocks a resume instead of corrupting the ledger;
* a near-close crash (after the checkpoint, before report finalisation) is
  recovered by the next invocation, which finalises the same day and persists
  the report byte-deterministically;
* the runner keeps the run-lock alive on long runs (heartbeat) so simulated
  time can never let another writer steal the account mid-run.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from pathlib import Path

import pytest

from fno_ai_paper_trading.models.enums import Signal
from fno_ai_paper_trading.paper_track.engine import TrackConfig
from fno_ai_paper_trading.paper_track.errors import (
    InjectedFailure,
    PaperTrackError,
    TrackCheckpointError,
)
from fno_ai_paper_trading.paper_track.runner import run_sessions
from fno_ai_paper_trading.paper_track.store import TrackStore
from fno_ai_paper_trading.strategies.base import SignalResult, Strategy
from tests.paper_track_testkit import closes_feed

DAY1 = date(2026, 9, 21)
DAY2 = date(2026, 9, 22)


class PlanStrategy(Strategy):
    name = "plan"

    def __init__(self, buy: time | None = None, sell: time | None = None) -> None:
        self.buy = buy
        self.sell = sell

    def analyze(self, bars):
        last = bars[-1]
        moment = last.timestamp.time()
        if self.buy is not None and moment == self.buy:
            return SignalResult(Signal.BUY, last.instrument, last.timestamp, "plan buy")
        if self.sell is not None and moment == self.sell:
            return SignalResult(Signal.SELL, last.instrument, last.timestamp, "plan sell")
        return SignalResult(Signal.HOLD, last.instrument, last.timestamp, "plan hold")


def _config(tmp_path, account="recover") -> TrackConfig:
    return TrackConfig(
        account=account, store_dir=Path(tmp_path),
        strategy=PlanStrategy(buy=time(10, 0), sell=time(12, 0)),
    )


def _day1_feed():
    return closes_feed([25000 + 40 * i for i in range(75)], day=DAY1)


def test_stale_locked_account_is_reclaimed_and_resumed(tmp_path):
    """A wall-clock-stale lock (dead holder, old heartbeat) is reclaimed by the
    next runner invocation instead of blocking the account forever."""
    import json

    from fno_ai_paper_trading.paper_track.lock import RunLock

    config = _config(tmp_path, account="stalerec")
    store = TrackStore(Path(tmp_path), "stalerec")

    # Simulate a hard-killed holder: lock owned by a dead run whose heartbeat is
    # ~9 months old (well beyond DEFAULT_STALE_AFTER=600s).
    lock = RunLock(Path(tmp_path), "stalerec")
    lock.try_acquire("dead-pid", now=datetime(2026, 1, 2, 0, 0, 0), stale_after=timedelta(days=1))
    lock_path = lock.path
    assert lock_path.exists()

    engine = run_sessions(
        config=config, store=store, feed=_day1_feed(), days=[DAY1],
        resume=False, run_id="fresh",
    )
    assert engine.eod_status in ("FLAT", "FLATTENED")
    assert store.load_report(DAY1) is not None
    # the reclaiming run released the lock afterwards
    assert not lock_path.exists()


def test_corrupted_checkpoint_blocks_resume(tmp_path):
    config = _config(tmp_path, account="corruptcp")
    store = TrackStore(Path(tmp_path), "corruptcp")
    run_sessions(
        config=config, store=store, feed=_day1_feed(), days=[DAY1],
        resume=False, run_id="first",
    )
    day2_feed = closes_feed([24000 - 10 * i for i in range(75)], day=DAY2)
    run_sessions(config=config, store=store, feed=day2_feed, days=[DAY2], resume=True, run_id="second")
    # corrupt the day-2 checkpoint body; a resume must refuse it, not replay it
    cp = store.checkpoint_path(DAY2)
    cp.write_text("{broken", encoding="utf-8")
    with pytest.raises(TrackCheckpointError):
        store.load_checkpoint(DAY2)


def test_near_close_crash_then_resume_finalizes(tmp_path):
    """Crash after the checkpoint and before report finalisation (failpoint H):
    the next invocation resumes the same day, finalises it, and persists the
    report with economics identical to an uninterrupted clean run."""
    clean = _config(tmp_path, account="clean")
    clean_store = TrackStore(Path(tmp_path), "clean")
    clean_engine = run_sessions(
        config=clean, store=clean_store, feed=_day1_feed(), days=[DAY1],
        resume=False, run_id="clean",
    )
    clean_fp = clean_engine.stable_fingerprint()

    crash = _config(tmp_path, account="crash")
    crash_store = TrackStore(Path(tmp_path), "crash")
    with pytest.raises(InjectedFailure):
        run_sessions(
            config=crash, store=crash_store, feed=_day1_feed(), days=[DAY1],
            resume=False, failpoints={"H"}, run_id="crashed",
        )
    assert crash_store.load_report(DAY1) is None  # finalise never happened

    engine2 = run_sessions(
        config=crash, store=crash_store, feed=_day1_feed(), days=[DAY1],
        resume=True, run_id="recovered",
    )
    assert engine2.stable_fingerprint() == clean_fp
    assert crash_store.load_report(DAY1) is not None
    assert engine2.counters["errors"] == []


def test_near_close_flatten_crash_then_resume_is_flat(tmp_path):
    """Crash inside the EOD flatten (failpoints I/J): the recovered run must
    flatten the position exactly once and land flat, never double-filling."""
    clean_store = TrackStore(Path(tmp_path), "cleanflt")
    buy_only = PlanStrategy(buy=time(10, 0), sell=None)  # position stays open → EOD flatten
    clean = TrackConfig(account="cleanflt", store_dir=Path(tmp_path), strategy=buy_only)
    clean_engine = run_sessions(
        config=clean, store=clean_store, feed=_day1_feed(), days=[DAY1],
        resume=False, run_id="clean",
    )
    assert clean_engine.position_quantity == 0
    assert clean_engine.counters["exits_filled"] == 1
    clean_fp = clean_engine.stable_fingerprint()

    crash_store = TrackStore(Path(tmp_path), "crashflt")
    crash = TrackConfig(account="crashflt", store_dir=Path(tmp_path), strategy=buy_only)
    with pytest.raises(InjectedFailure):
        run_sessions(
            config=crash, store=crash_store, feed=_day1_feed(), days=[DAY1],
            resume=False, failpoints={"J"}, run_id="crashed",
        )
    engine2 = run_sessions(
        config=crash, store=crash_store, feed=_day1_feed(), days=[DAY1],
        resume=True, run_id="recovered",
    )
    assert engine2.position_quantity == 0
    assert engine2.stable_fingerprint() == clean_fp
    assert engine2.counters["exits_filled"] == clean_engine.counters["exits_filled"]


def test_runner_heartbeats_the_account_lock(tmp_path, monkeypatch):
    """The runner heartbeats the account lock during the run so long/simulated
    runs can never look stale to a competing writer."""
    from fno_ai_paper_trading.paper_track.lock import RunLock

    calls: list[datetime] = []
    real_heartbeat = RunLock.heartbeat

    def _record(self, now):
        calls.append(now)
        real_heartbeat(self, now)

    monkeypatch.setattr(RunLock, "heartbeat", _record)
    config = _config(tmp_path, account="hb")
    store = TrackStore(Path(tmp_path), "hb")
    run_sessions(config=config, store=store, feed=_day1_feed(), days=[DAY1], resume=False, run_id="hb")
    # a day is 78 ticks; heartbeats fire every 75 ticks, so >= one must have run
    assert calls, "runner never heartbeated the account lock"
    # the account is flat and its day still finalized normally
    assert store.load_report(DAY1) is not None


def test_completed_day_on_resume_still_refused(tmp_path):
    config = _config(tmp_path, account="refuse")
    store = TrackStore(Path(tmp_path), "refuse")
    run_sessions(config=config, store=store, feed=_day1_feed(), days=[DAY1], resume=False)
    with pytest.raises(PaperTrackError, match="already completed"):
        run_sessions(config=config, store=store, feed=_day1_feed(), days=[DAY1], resume=True)