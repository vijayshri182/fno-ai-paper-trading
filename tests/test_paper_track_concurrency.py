"""Phase 5 — run-lock, single-writer enforcement and idempotency.

Two engines can never run the same account/day simultaneously (live lock can
not be stolen); a locked-but-dead holder's lock becomes stale and is broken;
and re-delivering the same data/ticks never double-trades.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from fno_ai_paper_trading.paper_track.clock import FixedClock
from fno_ai_paper_trading.paper_track.lock import RunLock
from tests.paper_track_testkit import DAY0, day_ticks, drive, make_engine


def _lock(tmp_path, account="acct") -> RunLock:
    return RunLock(tmp_path, account)


def test_single_writer_cannot_take_live_lock(tmp_path):
    lock = _lock(tmp_path)
    now = datetime(2026, 9, 21, 9, 14, 0)
    assert lock.try_acquire("run-a", now=now)
    # Second caller with a fresh object cannot acquire while held live.
    other = _lock(tmp_path)
    assert other.try_acquire("run-b", now=now + timedelta(minutes=1)) is False
    assert other.status(now=now + timedelta(minutes=1)).stale is False


def test_live_lock_can_be_freed_by_owner(tmp_path):
    lock = _lock(tmp_path)
    now = datetime(2026, 9, 21, 9, 14, 0)
    assert lock.try_acquire("run-a", now=now)
    lock.release()
    other = _lock(tmp_path)
    assert other.try_acquire("run-b", now=now + timedelta(minutes=1)) is True


def test_stale_lock_is_broken_and_reclaimed(tmp_path):
    lock = _lock(tmp_path)
    now = datetime(2026, 9, 21, 9, 14, 0)
    assert lock.try_acquire("dead-run", now=now)
    # Simulate a hard-killed holder: no heartbeat for > stale_after.
    lock.heartbeat(now + timedelta(minutes=1))  # holder was alive at +1m...
    # ...  but nothing since. A fresh caller after stale_after steals it.
    other = _lock(tmp_path)
    steal_at = now + timedelta(seconds=901)
    assert other.status(now=steal_at).stale is True
    assert other.try_acquire("fresh-run", now=steal_at) is True


def test_heartbeat_wards_off_staleness(tmp_path):
    lock = _lock(tmp_path)
    now = datetime(2026, 9, 21, 9, 14, 0)
    assert lock.try_acquire("run-a", now=now)
    other = _lock(tmp_path)
    # Heartbeats keep it live beyond the default stale window.
    heartbeat = now
    for _ in range(12):
        heartbeat += timedelta(minutes=5)
        lock.heartbeat(heartbeat)
        assert other.try_acquire("run-b", now=heartbeat) is False
        assert other.status(now=heartbeat).stale is False


def test_reentrancy_guard_within_one_owner(tmp_path):
    lock = _lock(tmp_path)
    now = datetime(2026, 9, 21, 9, 14, 0)
    assert lock.try_acquire("run-a", now=now)
    assert lock.try_acquire("run-a-again", now=now + timedelta(minutes=1)) is False


def test_empty_lock_file_is_unacquired(tmp_path):
    lock = _lock(tmp_path)
    state = lock.status(now=datetime(2026, 9, 21, 9, 14, 0))
    assert state.acquired is False


def test_same_run_twice_produces_identical_day(tmp_path):
    """Deterministic rerun: the same store/account/feed yields the same economics."""
    e1, _, _ = make_engine(tmp_path, days=[DAY0], run_id="r1")
    drive(e1, day_ticks(DAY0))
    e2, _, _ = make_engine(tmp_path, days=[DAY0], run_id="r2")
    drive(e2, day_ticks(DAY0))
    assert e1.stable_fingerprint() == e2.stable_fingerprint()
    assert [f.quantity for f in e1.broker.fills] == [f.quantity for f in e2.broker.fills]


def test_stepping_a_frozen_clock_twice_is_idempotent(tmp_path):
    engine, _, _ = make_engine(tmp_path, days=[DAY0])
    engine.clock.set(day_ticks(DAY0)[2])  # first processed bar tick (09:20)
    first = engine.step()
    before = (len(engine.broker.fills), len(engine.consumed))
    second = engine.step()  # same instant again
    after = (len(engine.broker.fills), len(engine.consumed))
    assert before == after
    assert second.status.value in ("IDLE", "SKIPPED", "DATA_SKIP", "ERROR")


def test_accounts_are_independent(tmp_path):
    a1, _, _ = make_engine(tmp_path, account="alpha", days=[DAY0])
    drive(a1, day_ticks(DAY0))
    b1, _, _ = make_engine(tmp_path, account="beta", days=[DAY0])
    assert b1.position_quantity == 0
    drive(b1, day_ticks(DAY0))
    assert len(a1.broker.fills) == len(b1.broker.fills)


def test_locked_engine_rejects_second_engine_same_day(tmp_path):
    """The runner's lock is part of runtime safety: a second engine cannot
    write to an account owned by a live run."""
    engine, store, _ = make_engine(tmp_path, days=[DAY0])
    engine.lock.try_acquire(engine.run_id, now=day_ticks(DAY0)[0])
    other, _, _ = make_engine(tmp_path, days=[DAY0], run_id="intruder")
    assert other.lock.try_acquire(other.run_id, now=day_ticks(DAY0)[1]) is False