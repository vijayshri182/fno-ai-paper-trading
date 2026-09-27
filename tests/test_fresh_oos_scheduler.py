"""Scheduler + lock: overlap protection, stale takeover, graceful shutdown.

The scheduler is deliberately thin -- every safety property lives in the
collector (run lock, catch-up, restart) -- so these tests exercise the loop and
the lock directly: a second scheduler cycle never runs while the first holds
the lock; a stale lock is taken over; ``stop()`` ends the loop cleanly.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import pytest

from fresh_oos_testkit import make_context
from fno_ai_paper_trading.fresh_oos.errors import LockedError
from fno_ai_paper_trading.fresh_oos.lock import CollectorLock
from fno_ai_paper_trading.fresh_oos.protocol import (
    STATUS_DATA_INVALID,
    STATUS_LOCKED,
    STATUS_NETWORK_ERROR,
    STATUS_NO_NEW_DATA,
    STATUS_RATE_LIMITED,
    STATUS_SOURCE_ERROR,
    STATUS_SUCCESS,
    RunOutcome,
)
from fno_ai_paper_trading.fresh_oos.scheduler import FreshOosScheduler

NOW = datetime(2026, 10, 15, 12, 0)
D1 = date(2026, 9, 17)


def _outcome(status: str, run_id: str = "RUN-T") -> RunOutcome:
    return RunOutcome(
        run_id=run_id,
        started_at="2026-10-15T06:30:00Z",
        ended_at="2026-10-15T06:30:01Z",
        status=status,
        dates_attempted=(),
        accepted=(),
        noop=(),
        conflicts=(),
        errors={},
        message=f"stub {status}",
    )


class _StubCollector:
    def __init__(self, statuses_or_fn):
        self.calls = 0
        if callable(statuses_or_fn):
            self._next = statuses_or_fn
        else:
            self._sequence = list(statuses_or_fn)
            self._next = lambda: self._sequence.pop(0)

    def collect_once(self):
        self.calls += 1
        return self._next()


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


# --------------------------------------------------------------------------
# WP-13: bounded in-pass retry of transient source failures
# --------------------------------------------------------------------------
def test_transient_status_is_retried_then_recovers(tmp_path):
    sleeps: list[float] = []
    collector = _StubCollector([_outcome(STATUS_RATE_LIMITED), _outcome(STATUS_SUCCESS)])
    scheduler = FreshOosScheduler(
        collector, interval_seconds=99, sleep=sleeps.append, retry_delay=0.2
    )
    code = scheduler.run(once=True)
    assert code == 0
    assert collector.calls == 2
    assert sleeps == [0.2]


def test_transient_status_retries_are_bounded(tmp_path):
    sleeps: list[float] = []
    collector = _StubCollector(lambda: _outcome(STATUS_NETWORK_ERROR))
    scheduler = FreshOosScheduler(
        collector, interval_seconds=99, sleep=sleeps.append, retry_attempts=4,
        retry_delay=0.1, retry_backoff=2.0, retry_max_delay=1.0,
    )
    code = scheduler.run(once=True)
    assert code == 1
    assert collector.calls == 4  # exactly attempts calls, no infinite retry
    assert sleeps == [0.1, 0.2, 0.4]


def test_hard_statuses_are_never_retried(tmp_path):
    sleeps: list[float] = []
    collector = _StubCollector([_outcome(STATUS_DATA_INVALID), _outcome(STATUS_SUCCESS)])
    scheduler = FreshOosScheduler(collector, interval_seconds=99, sleep=sleeps.append)
    code = scheduler.run(once=True)
    assert code == 1
    assert collector.calls == 1  # data-integrity outcome is not transient
    assert sleeps == []


def test_locked_is_not_retried(tmp_path):
    collector = _StubCollector([_outcome(STATUS_LOCKED)])
    scheduler = FreshOosScheduler(collector, interval_seconds=99, sleep=lambda s: None)
    assert scheduler.run(once=True) == 1
    assert collector.calls == 1


# --------------------------------------------------------------------------
# WP-13: consecutive-failure escalation + run-status artifact
# --------------------------------------------------------------------------
def test_consecutive_failures_escalate_to_stalled(tmp_path):
    sleeps: list[float] = []
    collector = _StubCollector(lambda: _outcome(STATUS_SOURCE_ERROR))
    status_path = tmp_path / "scheduler_status.json"
    scheduler = FreshOosScheduler(
        collector,
        interval_seconds=99,
        sleep=sleeps.append,
        status_path=status_path,
        max_consecutive_failures=2,
        retry_attempts=1,
    )
    code = scheduler.run(once=False)
    assert code == 2  # STALLED
    assert collector.calls == 2  # exactly the cap, then the loop halts
    payload = json.loads(status_path.read_text(encoding="utf-8"))
    assert payload["status"] == "STALLED"
    assert payload["escalated"] is True
    assert payload["consecutive_failures"] == 2


def test_successful_pass_resets_consecutive_failures(tmp_path):
    fails = 0
    state = {"calls": 0}

    def _next():
        state["calls"] += 1
        fails = state["calls"]
        # two failures, then a clean no-new-data pass, then one more failure
        if fails <= 2:
            return _outcome(STATUS_NETWORK_ERROR)
        if fails == 3:
            return _outcome(STATUS_NO_NEW_DATA)
        return _outcome(STATUS_SOURCE_ERROR)

    collector = _StubCollector(_next)
    scheduler = FreshOosScheduler(
        collector, interval_seconds=99, sleep=lambda s: None, max_consecutive_failures=3,
        retry_attempts=1,
    )
    code = scheduler.run(once=False)
    assert code == 2  # stalled only after the RESET counter reaches the cap again
    assert state["calls"] == 6  # 2 fail, 1 ok (reset), 3 fail -> stalled
    assert scheduler._consecutive_failures == 3


def test_locked_pass_does_not_count_as_failure(tmp_path):
    state = {"calls": 0}

    def _next():
        state["calls"] += 1
        if state["calls"] == 1:
            return _outcome(STATUS_LOCKED)
        return _outcome(STATUS_SOURCE_ERROR)

    collector = _StubCollector(_next)
    scheduler = FreshOosScheduler(
        collector, interval_seconds=99, sleep=lambda s: None, max_consecutive_failures=3,
        retry_attempts=1,
    )
    code = scheduler.run(once=False)
    assert code == 2
    # 1 locked (not counted) + 3 source failures -> stalled at the cap
    assert state["calls"] == 4
    assert scheduler._consecutive_failures == 3


def test_status_artifact_records_pass_details(tmp_path):
    sleeps: list[float] = []
    collector = _StubCollector([_outcome(STATUS_SUCCESS)])
    status_path = tmp_path / "scheduler_status.json"
    scheduler = FreshOosScheduler(
        collector, interval_seconds=99, sleep=sleeps.append, status_path=status_path
    )
    assert scheduler.run(once=True) == 0
    payload = json.loads(status_path.read_text(encoding="utf-8"))
    assert payload["status"] == "OK"
    assert payload["pass_status"] == STATUS_SUCCESS
    assert payload["consecutive_failures"] == 0
    assert payload["escalated"] is False
    assert payload["schema_version"] == 1


def test_no_status_path_means_no_artifact(tmp_path):
    collector = _StubCollector([_outcome(STATUS_SUCCESS)])
    scheduler = FreshOosScheduler(collector, interval_seconds=99, sleep=lambda s: None)
    assert scheduler.run(once=True) == 0
    assert (tmp_path / "scheduler_status.json").exists() is False