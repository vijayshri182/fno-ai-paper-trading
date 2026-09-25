"""Filesystem lock preventing overlapping collector runs.

Two competing scheduler ticks (or a cron tick racing a manual ``--once``) would
race on the same manifest and upstox account; the lock serialises them. It is a
cross-process advisory lock built on atomic ``O_CREAT|O_EXCL`` file creation:

* acquire writes ``{pid, hostname, created_at, run_id}`` to ``<root>/.collector.lock``;
* an already-present lock that is *stale* (older than ``stale_after``) is taken
  over after a single retry, so a crashed process does not wedge the server;
* release removes the file best-effort.

Pure stdlib (no Redis/Postgres/Celery dependency). Scheduler overlap and
restart tests exercise this module directly.
"""
from __future__ import annotations

import json
import os
import socket
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Iterator

from fno_ai_paper_trading.fresh_oos.errors import LockedError

DEFAULT_LOCK_NAME = ".collector.lock"
DEFAULT_STALE_AFTER_SECONDS = 15 * 60.0  # a run that outlives 15 minutes is stale


class CollectorLock:
    """Cross-process run lock for the fresh-OOS collector."""

    def __init__(
        self,
        root: str | Path,
        *,
        stale_after: float = DEFAULT_STALE_AFTER_SECONDS,
        now_fn: Callable[[], datetime] = datetime.now,
        lock_name: str = DEFAULT_LOCK_NAME,
    ) -> None:
        self.path = Path(root) / lock_name
        self.stale_after = float(stale_after)
        self._now_fn = now_fn
        self._held = False

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    def acquire(self, run_id: str = "") -> None:
        """Try to take the lock; raise :class:`LockedError` when busy/fresh."""
        if self._held:
            raise LockedError("collector lock already held by this process")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        now = self._now_fn()
        payload = {
            "pid": os.getpid(),
            "hostname": socket.gethostname() or "unknown",
            "created_at": now.isoformat(timespec="seconds"),
            "run_id": run_id or "",
        }
        try:
            self._create_exclusive(payload)
        except FileExistsError:
            if self._is_stale():
                try:
                    self.path.unlink()
                except FileNotFoundError:
                    pass
                try:
                    self._create_exclusive(payload)
                except FileExistsError as exc:
                    raise LockedError(
                        "another fresh-OOS collector run holds the lock (recent)"
                    ) from exc
            else:
                raise LockedError(
                    "another fresh-OOS collector run holds the lock "
                    f"(pid/run recorded at {self.path})"
                )
        self._held = True

    def release(self) -> None:
        if not self._held:
            return
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        finally:
            self._held = False

    @contextmanager
    def locked(self, run_id: str = "") -> Iterator[None]:
        self.acquire(run_id=run_id)
        try:
            yield
        finally:
            self.release()

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    def _create_exclusive(self, payload: dict[str, object]) -> None:
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        fd = os.open(str(self.path), flags, 0o644)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, sort_keys=True)
        except BaseException:
            try:
                self.path.unlink()
            except OSError:
                pass
            raise

    def _is_stale(self) -> bool:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return True
        try:
            created = datetime.fromisoformat(str(payload.get("created_at", "")))
        except (TypeError, ValueError):
            return True
        age = self._now_fn() - created
        return age > timedelta(seconds=self.stale_after)