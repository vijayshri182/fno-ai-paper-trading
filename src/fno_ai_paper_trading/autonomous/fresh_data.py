"""Fresh, untouched, single-use out-of-sample (OOS) data pool for the loop.

Data recorded here is strictly fresh OOS: every bar is dated **after** the
lineage's already-consumed protected window (2025-10-06 .. 2026-09-11). The pool
only ever consumes metadata and coverage (timestamps/checksums) -- it never runs
the blocked candidate, never tunes, never ranks and never inspects strategy
state. A window is recorded as ``UNTOUCHED`` at acquisition, requires an explicit
``register`` before it may be referenced by research, and may be ``consume``d
exactly once. Collection, registration, inspection, validation and promotion are
kept as separate steps.

Safety: read-only research data bookkeeping. No orders, no live trading, no
promotion.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, Mapping

POOL_SCHEMA_VERSION = 1
STATUS_UNTOUCHED = "UNTOUCHED"
STATUS_CONSUMED = "CONSUMED"
_META_SUFFIX = ".meta.json"


@dataclass(frozen=True)
class FreshOosRequirement:
    """Minimum coverage a fresh OOS window must have before validation."""

    first_bar_after: date
    min_days: int = 20
    min_bars: int = 1500
    min_trades: int = 30
    contiguous: bool = True

    def __post_init__(self) -> None:
        if self.min_days <= 0:
            raise ValueError("min_days must be positive")
        if self.min_bars <= 0:
            raise ValueError("min_bars must be positive")
        if self.min_trades <= 0:
            raise ValueError("min_trades must be positive")

    def to_dict(self) -> dict[str, object]:
        return {
            "first_bar_after": self.first_bar_after.isoformat(),
            "min_days": self.min_days,
            "min_bars": self.min_bars,
            "min_trades": self.min_trades,
            "contiguous": self.contiguous,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "FreshOosRequirement":
        return cls(
            first_bar_after=date.fromisoformat(str(data["first_bar_after"])),
            min_days=int(data["min_days"]),
            min_bars=int(data["min_bars"]),
            min_trades=int(data["min_trades"]),
            contiguous=bool(data.get("contiguous", True)),
        )


@dataclass(frozen=True)
class FreshWindow:
    """One acquired fresh-OOS window (metadata only, never strategy output)."""

    name: str
    start: date
    end: date
    bars: int
    days: int
    data_hash: str
    status: str = STATUS_UNTOUCHED
    registered: bool = False
    first_used: str = ""

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("name is required")
        if self.start > self.end:
            raise ValueError("start must not be after end")
        if self.bars < 0 or self.days < 0:
            raise ValueError("bars/days must not be negative")
        if not self.data_hash.strip():
            raise ValueError("data_hash is required")
        if self.status not in (STATUS_UNTOUCHED, STATUS_CONSUMED):
            raise ValueError(f"unknown status {self.status!r}")
        if self.status == STATUS_CONSUMED and not self.first_used:
            raise ValueError("consumed window requires first_used")

    @property
    def untouched(self) -> bool:
        return self.status == STATUS_UNTOUCHED

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "bars": self.bars,
            "days": self.days,
            "data_hash": self.data_hash,
            "status": self.status,
            "registered": self.registered,
            "first_used": self.first_used,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "FreshWindow":
        return cls(
            name=str(data["name"]),
            start=date.fromisoformat(str(data["start"])),
            end=date.fromisoformat(str(data["end"])),
            bars=int(data["bars"]),
            days=int(data["days"]),
            data_hash=str(data["data_hash"]),
            status=str(data.get("status", STATUS_UNTOUCHED)),
            registered=bool(data.get("registered", False)),
            first_used=str(data.get("first_used", "")),
        )


@dataclass(frozen=True)
class FreshDataReadiness:
    """Whether the fresh pool already satisfies validation coverage."""

    ready: bool
    bars: int
    days: int
    missing_bars: int
    missing_days: int
    reasons: tuple[str, ...]

    def required_message(self) -> str:
        if self.ready:
            return "sufficient fresh untouched OOS window already available"
        return "; ".join(self.reasons)

    def ready_message(self) -> str:
        return self.required_message()

    def to_dict(self) -> dict[str, object]:
        return {
            "ready": self.ready,
            "bars": self.bars,
            "days": self.days,
            "missing_bars": self.missing_bars,
            "missing_days": self.missing_days,
            "reasons": list(self.reasons),
        }


class FreshDataPool:
    """Registry of acquired fresh windows with single-use enforcement."""

    def __init__(
        self,
        requirement: FreshOosRequirement,
        pool_path: str | Path | None = None,
    ) -> None:
        self.requirement = requirement
        self.pool_path = Path(pool_path) if pool_path else None
        self._windows: list[FreshWindow] = []

    # ------------------------------------------------------------------ #
    # Construction / persistence
    # ------------------------------------------------------------------ #

    @classmethod
    def from_datasets(
        cls,
        datasets_dir: str | Path,
        requirement: FreshOosRequirement,
        pool_path: str | Path | None = None,
    ) -> "FreshDataPool":
        """Acquire (collect) fresh windows from recorded dataset metadata."""
        pool = cls(requirement, pool_path=pool_path)
        dataset_dir = Path(datasets_dir)
        for meta_path in sorted(dataset_dir.glob(f"*{_META_SUFFIX}")):
            pool.add_from_meta(meta_path)
        return pool

    @classmethod
    def load(
        cls,
        pool_path: str | Path,
        requirement: FreshOosRequirement | None = None,
    ) -> "FreshDataPool":
        data = json.loads(Path(pool_path).read_text(encoding="utf-8"))
        if int(data.get("schema_version", 0)) != POOL_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported pool schema {data.get('schema_version')!r}"
            )
        req = (
            FreshOosRequirement.from_dict(data["requirement"])
            if requirement is None
            else requirement
        )
        pool = cls(req, pool_path=pool_path)
        for row in data.get("windows", []):
            pool._windows.append(FreshWindow.from_dict(row))
        return pool

    def save(self) -> None:
        if self.pool_path is None:
            raise ValueError("no pool_path configured")
        self.pool_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "schema_version": POOL_SCHEMA_VERSION,
            "requirement": self.requirement.to_dict(),
            "windows": [w.to_dict() for w in self._windows],
        }
        self.pool_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    def add_window(self, window: FreshWindow) -> FreshWindow:
        """Record a window from metadata (does not consume or register it)."""
        if any(w.name == window.name for w in self._windows):
            raise ValueError(f"window {window.name!r} already recorded")
        self._windows.append(window)
        return window

    def add_from_meta(self, meta_path: str | Path) -> FreshWindow | None:
        """Collect one dataset window if it is strictly fresh (metadata only)."""
        meta_path = Path(meta_path)
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        start = date.fromisoformat(str(meta["start_date"]))
        if start <= self.requirement.first_bar_after:
            return None
        end = date.fromisoformat(str(meta["end_date"]))
        name = meta_path.name[: -len(_META_SUFFIX)]
        data_hash = str(meta.get("data_hash", ""))
        bars = int(meta.get("num_bars", 0))
        csv_path = meta_path.with_name(meta_path.name[: -len(_META_SUFFIX)] + ".csv")
        days = _count_trading_days(csv_path) if csv_path.exists() else 0
        window = FreshWindow(
            name=name,
            start=start,
            end=end,
            bars=bars,
            days=days,
            data_hash=data_hash,
        )
        self.add_window(window)
        return window

    # ------------------------------------------------------------------ #
    # Window lifecycle
    # ------------------------------------------------------------------ #

    def register(self, name: str) -> FreshWindow:
        """Mark a collected window as available for a future single validation."""
        index = self._index_of(name)
        window = self._windows[index]
        if window.status == STATUS_CONSUMED:
            raise ValueError(f"window {name!r} already consumed")
        updated = FreshWindow(
            name=window.name,
            start=window.start,
            end=window.end,
            bars=window.bars,
            days=window.days,
            data_hash=window.data_hash,
            status=window.status,
            registered=True,
            first_used=window.first_used,
        )
        self._windows[index] = updated
        return updated

    def consume(self, name: str, now: datetime) -> FreshWindow:
        """Consume a registered window exactly once (single-use enforcement)."""
        index = self._index_of(name)
        window = self._windows[index]
        if window.status == STATUS_CONSUMED:
            raise ValueError(f"window {name!r} already consumed (single-use)")
        if not window.registered:
            raise ValueError(f"window {name!r} is not registered; inspect/use prohibited")
        updated = FreshWindow(
            name=window.name,
            start=window.start,
            end=window.end,
            bars=window.bars,
            days=window.days,
            data_hash=window.data_hash,
            status=STATUS_CONSUMED,
            registered=True,
            first_used=now.isoformat(timespec="seconds") + "Z",
        )
        self._windows[index] = updated
        return updated

    def get(self, name: str) -> FreshWindow | None:
        for window in self._windows:
            if window.name == name:
                return window
        return None

    @property
    def windows(self) -> tuple[FreshWindow, ...]:
        return tuple(self._windows)

    @property
    def untouched_windows(self) -> tuple[FreshWindow, ...]:
        return tuple(w for w in self._windows if w.untouched)

    @property
    def registered_windows(self) -> tuple[FreshWindow, ...]:
        return tuple(w for w in self._windows if w.registered)

    # ------------------------------------------------------------------ #
    # Readiness
    # ------------------------------------------------------------------ #

    def readiness(self) -> FreshDataReadiness:
        """Ready once a single untouched window meets coverage, else reasons."""
        candidates = self.untouched_windows
        best = max(candidates, key=lambda w: (w.days, w.bars)) if candidates else None
        reasons: list[str] = []
        if best is None:
            reasons.append("no fresh untouched OOS window collected")
        else:
            if best.days < self.requirement.min_days:
                reasons.append(
                    f"needs {self.requirement.min_days} trading days, has {best.days}"
                )
            if best.bars < self.requirement.min_bars:
                reasons.append(
                    f"needs {self.requirement.min_bars} bars, has {best.bars}"
                )
        if not self.registered_windows:
            reasons.append("no collected window is registered yet")
        if self.requirement.contiguous and best is not None and best.bars == 0:
            reasons.append("window is empty / non-contiguous data unavailable")
        ready = bool(
            best is not None
            and best.days >= self.requirement.min_days
            and best.bars >= self.requirement.min_bars
            and bool(self.registered_windows)
        )
        missing_days = 0
        missing_bars = 0
        if best is not None:
            missing_days = max(0, self.requirement.min_days - best.days)
            missing_bars = max(0, self.requirement.min_bars - best.bars)
        return FreshDataReadiness(
            ready=ready,
            bars=best.bars if best else 0,
            days=best.days if best else 0,
            missing_days=missing_days,
            missing_bars=missing_bars,
            reasons=tuple(reasons) or ("insufficient fresh evidence",),
        )

    def describe(self) -> str:
        read = self.readiness()
        summary = ", ".join(
            f"{w.name}: {w.bars} bars / {w.days} days / {w.status.lower()}"
            + (" registered" if w.registered else "")
            for w in self._windows
        )
        return f"fresh pool [{summary or 'empty'}] readiness={read.required_message()}"

    def _index_of(self, name: str) -> int:
        for index, window in enumerate(self._windows):
            if window.name == name:
                return index
        raise KeyError(f"window {name!r} not in pool")


def _count_trading_days(csv_path: Path) -> int:
    """Count distinct ISO date prefixes in the timestamp column (coverage only)."""
    days: set[str] = set()
    with csv_path.open("r", encoding="utf-8", newline="") as handle:
        next(handle, None)
        for line in handle:
            stamp = line.split(",", 1)[0]
            if stamp:
                days.add(stamp[:10])
    return len(days)