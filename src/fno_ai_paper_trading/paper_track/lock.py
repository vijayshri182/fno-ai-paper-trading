"""Account run-lock for the Daily Paper Trading Track.

Exactly one engine may run against an account/day at a time. The lock is a JSON
file in ``<store>/locks/<account>.lock.json`` carrying ``{pid, run_id,
acquired_at, heartbeat_at}`` (naive NSE-IST). Liveness is timestamp-based, which
works across processes and survives a hard kill of the holder: if the holder
dies, the lock goes stale after ``stale_after`` and the next attempt may break
it. A live lock can never be stolen — this is what makes single-writer runtime
enforcement testable without platform-specific OS file locking.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from fno_ai_paper_trading.paper_track.errors import TrackLockError

__all__ = ["RunLock", "LockState"]

DEFAULT_STALE_AFTER = timedelta(seconds=600)


@dataclass(frozen=True)
class LockState:
    """Snapshot of the lock file for one account."""

    path: str
    acquired: bool
    owner_pid: int | None = None
    run_id: str | None = None
    acquired_at: datetime | None = None
    heartbeat_at: datetime | None = None
    stale: bool = False
    detail: str = ""

    def as_dict(self) -> dict:
        return {
            "path": self.path,
            "acquired": self.acquired,
            "owner_pid": self.owner_pid,
            "run_id": self.run_id,
            "acquired_at": self.acquired_at.isoformat() if self.acquired_at else None,
            "heartbeat_at": self.heartbeat_at.isoformat() if self.heartbeat_at else None,
            "stale": self.stale,
            "detail": self.detail,
        }


class RunLock:
    """Timestamp-based exclusive run lock for one account."""

    def __init__(self, store_dir: Path, account: str) -> None:
        self.store_dir = Path(store_dir)
        self.account = account
        self.lock_dir = self.store_dir / "locks"
        self.path = self.lock_dir / f"{account}.lock.json"
        self._held_here = False  # re-entrancy guard within this instance

    # -- maintenance ------------------------------------------------------

    def _read(self) -> LockState:
        if not self.path.exists():
            return LockState(path=str(self.path), acquired=False, detail="no lock file")
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise TrackLockError(f"cannot read lock file {self.path}")
        return LockState(
            path=str(self.path),
            acquired=True,
            owner_pid=payload.get("pid"),
            run_id=payload.get("run_id"),
            acquired_at=self._parse(payload.get("acquired_at")),
            heartbeat_at=self._parse(payload.get("heartbeat_at")),
        )

    @staticmethod
    def _parse(value) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.fromisoformat(value)
        except (TypeError, ValueError):
            return None

    def _write(self, *, now: datetime, pid: int, run_id: str, heartbeat_at: datetime) -> None:
        self.lock_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "pid": pid,
            "run_id": run_id,
            "acquired_at": now.isoformat(),
            "heartbeat_at": heartbeat_at.isoformat(),
        }
        tmp = self.path.with_suffix(".lock.tmp")
        tmp.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.path)

    # -- API --------------------------------------------------------------

    def try_acquire(
        self,
        run_id: str,
        now: datetime | None = None,
        stale_after: timedelta = DEFAULT_STALE_AFTER,
        pid: int | None = None,
    ) -> bool:
        """Try to take the lock; returns True when this caller owns it now."""
        if self._held_here:
            return False
        owner = self._read()
        if owner.acquired:
            heartbeat = owner.heartbeat_at or owner.acquired_at
            if heartbeat is not None and (now - heartbeat) > stale_after:
                # Holder is dead/gone: break the stale lock and take ownership.
                self._write(now=now, pid=pid or os.getpid(), run_id=run_id, heartbeat_at=now)
                self._held_here = True
                return True
            return False
        self._write(now=now, pid=pid or os.getpid(), run_id=run_id, heartbeat_at=now)
        self._held_here = True
        return True

    def heartbeat(self, now: datetime) -> None:
        """Refresh the heartbeat (call on a schedule while the run is alive)."""
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        payload["heartbeat_at"] = now.isoformat()
        tmp = self.path.with_suffix(".lock.tmp")
        tmp.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.path)

    def release(self) -> None:
        if not self._held_here:
            return
        try:
            if self.path.exists():
                self.path.unlink()
        finally:
            self._held_here = False

    def status(self, now: datetime | None = None, stale_after: timedelta = DEFAULT_STALE_AFTER) -> LockState:
        state = self._read()
        if state.acquired:
            heartbeat = state.heartbeat_at or state.acquired_at
            current = now if now is not None else datetime.now()
            stale = heartbeat is None or (current - heartbeat) > stale_after
            state = LockState(
                path=state.path,
                acquired=state.acquired,
                owner_pid=state.owner_pid,
                run_id=state.run_id,
                acquired_at=state.acquired_at,
                heartbeat_at=state.heartbeat_at,
                stale=stale,
                detail=state.detail,
            )
        return state

    def __enter__(self) -> "RunLock":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()