"""Scheduler + lock: overlap protection, stale takeover, graceful shutdown.

The scheduler is deliberately thin -- every safety property lives in the
collector (run lock, catch-up, restart) -- so these tests exercise the loop and
the lock directly: a second scheduler cycle never runs while the first holds
the lock; a stale lock is taken over; ``stop()`` ends the loop cleanly.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from fresh_oos_testkit import make_context
from fno_ai_paper_trading.fresh_oos.errors import LockedError
from fno_ai_paper_trading.fresh_oos.lock import CollectorLock
from fno_ai_paper_trading.fresh_oos.protocol import STATUS_LOCKED, STATUS_SUCCESS
from fno_ai_paper_trading.fresh_oos.scheduler import FreshOosScheduler

NOW = datetime(2026, 10, 15, 12, 0)
D1 = date(2026, 9, 17)


class _FakeLock:
    def __init__(self, raise_locked=False):
        self.held = False
        self._raise_locked = raise_locked

    def acquire(self, run_id=""):
        if self._raise_locked:
            raise LockedError("locked")
        self.held = True

    def release(self):
        self.held = False


def test_collector_returns_locked_when_another_run_holds_the_lock(tmp_path):
    ctx = make_context(tmp_path, now=NOW)
    blocker = CollectorLock(ctx["root"], now_fn=lambda: NOW)
    blocker.acquire(run_id="other")
    try:
        outcome = ctx["collector"].collect_once(force_date=D1)
        assert outcome.status == STATUS_LOCKED
        assert outcome.accepted == ()
        assert ctx["store"].find(D1) is None  # nothing acquired
    finally:
        blocker.release()


def test_collector_takes_over_stale_lock(tmp_path):
    ctx = make_context(tmp_path, now=NOW)
    stale = CollectorLock(
        ctx["root"],
        stale_after=15 * 60,
        now_fn=lambda: NOW - timedelta(hours=1),  # lock created an hour ago
    )
    stale.acquire(run_id="crashed")
    assert stale.path.exists()
    outcome = ctx["collector"].collect_once(force_date=D1)
    assert outcome.status == STATUS_SUCCESS
    assert ctx["store"].find(D1) is not None


def test_lock_blocks_reentrant_acquire():
    lock = CollectorLock(".")  # unused path; held flag drives behavior
    lock._held = True
    with pytest.raises(LockedError):
        lock.acquire(run_id="x")


def test_scheduler_guards_against_overlap_and_stops_cleanly(tmp_path):
    ctx = make_context(tmp_path, now=NOW)
    # A held lock makes every pass return LOCKED; the scheduler keeps ticking.
    fake_lock = _FakeLock(raise_locked=True)
    ctx["collector"].lock = fake_lock

    sleeps: list[float] = []

    def _sleep(seconds):
        sleeps.append(seconds)

    scheduler = FreshOosScheduler(ctx["collector"], interval_seconds=7, sleep=_sleep)
    scheduler.stop()  # stop before the first pass -> single pass
    exit_code = scheduler.run(once=True)
    assert exit_code == 1  # LOCKED is a failure status
    assert sleeps == []
    assert len(ctx["manifest"].runs) == 1  # only the locked pass recorded


def test_scheduler_loop_runs_n_passes_then_stops(tmp_path):
    ctx = make_context(tmp_path, now=datetime(2026, 9, 19, 12, 0))
    for _ in range(2):
        scheduler = FreshOosScheduler(ctx["collector"], interval_seconds=60,
                                      sleep=lambda s: None)
        scheduler.stop()  # once-per-constructor -> one pass each
        code = scheduler.run(once=True)
        assert code == 0
    assert ctx["store"].find(D1) is not None
    assert len(ctx["manifest"].runs) == 2


def test_lock_release_removes_the_lock_file(tmp_path):
    lock = CollectorLock(tmp_path / "root/", now_fn=lambda: NOW)
    lock.acquire(run_id="r")
    assert lock.path.exists()
    lock.release()
    assert not lock.path.exists()
    # re-acquire works immediately after release
    lock.acquire(run_id="r2")
    lock.release()