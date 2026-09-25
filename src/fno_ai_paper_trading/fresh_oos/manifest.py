"""Fresh-OOS manifest: protocol, pool state, run log, readiness.

The manifest is the collector's single source of truth on disk (JSON under
``data/fresh_oos/fresh_oos_manifest.json``). It records:

* the protocol block (boundary, thresholds, single-use/tuning flags);
* one pool entry per trading date (acquisition status, source, SHA-256 hash);
* an append-only run log describing every collection pass;
* the deterministic readiness summary computed from accepted coverage.

Persistence is atomic (scratch + fsync + ``os.replace``), so a crashed process
never corrupts the manifest. Reads are tolerant: structural problems raise
:class:`DataInvalidError` instead of being silently ignored.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Mapping

from fno_ai_paper_trading.data.intervals import canonical_interval
from fno_ai_paper_trading.fresh_oos.errors import DataInvalidError
from fno_ai_paper_trading.fresh_oos.protocol import (
    FRESH_OOS_BOUNDARY,
    INTERVAL,
    MIN_BARS,
    MIN_TRADES,
    MIN_TRADING_DAYS,
    POOL_ACQUIRED,
    POOL_CONFLICT,
    POOL_NOOP_DEDICATED,
    POOL_NOOP_ESTABLISHED,
    POOL_VERIFY_INVALID,
    PROTOCOL_NAME,
    PROTOCOL_VERSION,
    ReadinessReport,
    RunOutcome,
    SCHEMA_VERSION,
    compute_readiness,
    utc_now_iso,
)

_ACCEPTED_POOL_STATUSES = frozenset({POOL_ACQUIRED, POOL_NOOP_DEDICATED, POOL_NOOP_ESTABLISHED})


@dataclass
class FreshOosManifest:
    """In-memory manifest model bound to one JSON path."""

    path: Path
    boundary: date = FRESH_OOS_BOUNDARY
    min_days: int = MIN_TRADING_DAYS
    min_bars: int = MIN_BARS
    min_trades: int = MIN_TRADES
    pool: dict[str, dict[str, object]] = field(default_factory=dict)
    runs: list[RunOutcome] = field(default_factory=list)
    protocol_version: int = PROTOCOL_VERSION
    generated_at: str = ""

    # ------------------------------------------------------------------ #
    # Construction / persistence
    # ------------------------------------------------------------------ #

    @classmethod
    def load(
        cls,
        path: str | Path,
        *,
        boundary: date = FRESH_OOS_BOUNDARY,
        min_days: int = MIN_TRADING_DAYS,
        min_bars: int = MIN_BARS,
        min_trades: int = MIN_TRADES,
    ) -> "FreshOosManifest":
        path = Path(path)
        manifest = cls(
            path=path,
            boundary=boundary,
            min_days=min_days,
            min_bars=min_bars,
            min_trades=min_trades,
        )
        if not path.exists():
            return manifest
        data = _read_json(path)
        if str(data.get("schema_version", "")) != SCHEMA_VERSION:
            raise DataInvalidError(f"unsupported fresh-OOS manifest schema {data.get('schema_version')!r}")
        protocol = data.get("protocol") or {}
        manifest.boundary = date.fromisoformat(str(protocol.get("boundary_exclusive", boundary)))
        manifest.min_days = int(protocol.get("min_trading_days", min_days))
        manifest.min_bars = int(protocol.get("min_bars", min_bars))
        manifest.min_trades = int(protocol.get("min_trades", min_trades))
        manifest.protocol_version = int(protocol.get("version", PROTOCOL_VERSION))
        manifest.pool = {
            str(key): dict(value) for key, value in (data.get("pool") or {}).items()
        }
        manifest.runs = [RunOutcome.from_dict(row) for row in (data.get("runs") or [])]
        manifest.generated_at = str(data.get("generated_at", ""))
        return manifest

    def save(self) -> None:
        """Atomically persist the manifest (scratch + fsync + replace)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": SCHEMA_VERSION,
            "generated_at": self.generated_at,
            "protocol": {
                "name": PROTOCOL_NAME,
                "version": self.protocol_version,
                "boundary_exclusive": self.boundary.isoformat(),
                "min_trading_days": self.min_days,
                "min_bars": self.min_bars,
                "min_trades": self.min_trades,
                "single_use_validation": True,
                "parameter_tuning_allowed": False,
                "interval": INTERVAL,
            },
            "pool": dict(sorted(self.pool.items())),
            "runs": [run.to_dict() for run in self.runs],
        }
        text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
        scratch = self.path.with_name(f".tmp-{self.path.name}")
        _atomic_write_text(scratch, self.path, text)

    # ------------------------------------------------------------------ #
    # Pool bookkeeping
    # ------------------------------------------------------------------ #

    def record_date(
        self,
        day: date,
        *,
        status: str,
        data_hash: str = "",
        num_bars: int = 0,
        source: str = "",
        error: str = "",
        updated_at: str = "",
    ) -> None:
        """Record (or refresh) one date's pool entry.

        An error for a date that is already accepted never erases the accepted
        record: immutability of the *fact* that the day was accepted is
        preserved, while the error appears in the run log.
        """
        key = day.isoformat()
        existing = self.pool.get(key)
        if existing is None or existing.get("status") not in _ACCEPTED_POOL_STATUSES or status in _ACCEPTED_POOL_STATUSES:
            entry: dict[str, object] = {
                "status": status,
                "data_hash": data_hash,
                "num_bars": num_bars,
                "source": source,
                "error": error,
                "updated_at": updated_at,
            }
            if existing is not None:
                entry["first_recorded_at"] = existing.get("first_recorded_at", updated_at)
            else:
                entry["first_recorded_at"] = updated_at
            self.pool[key] = entry

    def add_run(self, outcome: RunOutcome) -> None:
        """Append a run record (append-only run log)."""
        self.runs.append(outcome)

    # ------------------------------------------------------------------ #
    # Derived state
    # ------------------------------------------------------------------ #

    @property
    def accepted_dates(self) -> list[date]:
        dates = [
            date.fromisoformat(key)
            for key, entry in self.pool.items()
            if entry.get("status") in _ACCEPTED_POOL_STATUSES
        ]
        return sorted(dates)

    @property
    def accepted_bars(self) -> int:
        return sum(
            int(entry.get("num_bars", 0)) for entry in self.pool.values() if entry.get("status") in _ACCEPTED_POOL_STATUSES
        )

    def readiness(self, now: str = "") -> ReadinessReport:
        return compute_readiness(
            len(self.accepted_dates),
            self.accepted_bars,
            min_days=self.min_days,
            min_bars=self.min_bars,
            min_trades=self.min_trades,
        )

    def last_run(self) -> RunOutcome | None:
        return self.runs[-1] if self.runs else None

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION,
            "generated_at": self.generated_at,
            "protocol": {
                "name": PROTOCOL_NAME,
                "version": self.protocol_version,
                "boundary_exclusive": self.boundary.isoformat(),
                "min_trading_days": self.min_days,
                "min_bars": self.min_bars,
                "min_trades": self.min_trades,
                "single_use_validation": True,
                "parameter_tuning_allowed": False,
                "interval": INTERVAL,
            },
            "pool": dict(sorted(self.pool.items())),
            "runs": [run.to_dict() for run in self.runs],
        }


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise DataInvalidError(f"unreadable manifest {path}") from exc
    if not isinstance(payload, dict):
        raise DataInvalidError(f"manifest {path} is not a JSON object")
    return payload


def _atomic_write_text(scratch: Path, target: Path, text: str) -> None:
    import os

    with scratch.open("w", encoding="utf-8", newline="") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        scratch.replace(target)
    except OSError:
        if scratch.exists():
            try:
                scratch.unlink()
            except OSError:
                pass
        raise


def manifest_generated_at(now) -> str:
    return utc_now_iso(now)