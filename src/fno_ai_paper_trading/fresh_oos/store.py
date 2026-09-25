"""Immutable, content-addressed per-day storage for fresh-OOS candles.

Layout under the store root (default ``data/fresh_oos``)::

    <root>/NIFTY_50_5m/YYYY-MM-DD/data.csv
    <root>/NIFTY_50_5m/YYYY-MM-DD/metadata.json
    <root>/NIFTY_50_5m/YYYY-MM-DD/sha256.txt

Writing is atomic per file and refuses to overwrite an accepted day:

1. the three artifacts are first written to ``.tmp-*`` scratch files, flushed
   and ``fsync``-ed;
2. they are then ``os.replace``-ed into place (atomic on the same filesystem);
3. any failure cleans up scratch files and leaves the target day untouched, so
   a crashed run never leaves a half-accepted day;
4. re-acquiring an existing day raises :class:`FileExistsError` -- immutability
   is enforced at the storage layer, not just in the collector.

The CSV format and the SHA-256 ``data_hash`` are byte-identical to the
established ``datasets/`` convention (``data.dataset_store``), so a day recorded
here is hash-comparable with an established single-day dataset. ``sha256.txt``
carries the same digest the metadata ``data_hash`` records.

This module also scans the established ``datasets/`` pool for single-day
NIFTY 50 5m datasets (e.g. the WS 7.26 sessions
``upstox_Nifty_50_5m_20260915_20260915``), so already-acquired days are reused
instead of re-collected.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, timedelta, timezone
from pathlib import Path
from typing import Any

from fno_ai_paper_trading.data.intervals import canonical_interval
from fno_ai_paper_trading.fresh_oos.errors import DataInvalidError
from fno_ai_paper_trading.fresh_oos.protocol import (
    FRESH_OOS_BOUNDARY,
    INTERVAL,
    SCHEMA_VERSION,
)
from fno_ai_paper_trading.models.instruments import Instrument
from fno_ai_paper_trading.models.market import MarketPrice

_KOLKATA = timezone(timedelta(hours=5, minutes=30))

_CSV_COLUMNS = ("timestamp", "open", "high", "low", "close", "volume", "open_interest")
_META_NAME = "metadata.json"
_HASH_NAME = "sha256.txt"
_CSV_NAME = "data.csv"

DEFAULT_STORE_ROOT = Path("data") / "fresh_oos"
DEFAULT_DATASETS_DIR = Path("datasets")


def canonical_csv(bars: list[MarketPrice]) -> str:
    """Deterministic canonical CSV (byte-identical to the dataset store).

    Timestamps are rendered as naive IST (the established ``datasets/``
    convention), so an IST-aware timestamp from the provider and the stored
    naive form hash identically.
    """
    lines = [",".join(_CSV_COLUMNS)]
    for bar in bars:
        oi = "" if bar.open_interest is None else str(bar.open_interest)
        timestamp = bar.timestamp
        if timestamp.tzinfo is not None:
            timestamp = timestamp.astimezone(_KOLKATA).replace(tzinfo=None)
        lines.append(
            ",".join(
                (
                    timestamp.isoformat(),
                    str(bar.open),
                    str(bar.high),
                    str(bar.low),
                    str(bar.close),
                    str(bar.volume),
                    oi,
                )
            )
        )
    return "\n".join(lines) + "\n"


def content_hash(bars: list[MarketPrice]) -> str:
    """Deterministic SHA-256 over the canonical CSV of ``bars``."""
    return hashlib.sha256(canonical_csv(bars).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class StoredDay:
    """One accepted day stored in the immutable area."""

    day: date
    directory: Path
    csv_path: Path
    data_hash: str
    num_bars: int

    def to_record(self) -> dict[str, object]:
        return {
            "day": self.day.isoformat(),
            "num_bars": self.num_bars,
            "data_hash": self.data_hash,
        }


@dataclass(frozen=True)
class EstablishedDay:
    """A single-day NIFTY 50 5m dataset already present in ``datasets/``."""

    day: date
    name: str
    csv_path: Path
    meta_path: Path
    data_hash: str
    verified: bool  # recomputed CSV hash matched the recorded meta data_hash


class FreshOosStore:
    """Immutable per-day store (scratch + fsync + atomic rename)."""

    def __init__(self, root: str | Path, namespace: str = "NIFTY_50_5m") -> None:
        self.root = Path(root)
        self.namespace = namespace
        self.base_dir = self.root / namespace

    # ------------------------------------------------------------------ #
    # Day paths
    # ------------------------------------------------------------------ #

    def day_dir(self, day: date) -> Path:
        return self.base_dir / day.isoformat()

    def _artifacts(self, day: date) -> tuple[Path, Path, Path]:
        day_dir = self.day_dir(day)
        return day_dir / _CSV_NAME, day_dir / _META_NAME, day_dir / _HASH_NAME

    # ------------------------------------------------------------------ #
    # Writing (atomic, refuse to overwrite)
    # ------------------------------------------------------------------ #

    def write_day(
        self,
        day: date,
        bars: list[MarketPrice],
        *,
        instrument: Instrument,
        data_hash: str,
        downloaded_at: str,
        timezone: str = "Asia/Kolkata",
    ) -> StoredDay:
        """Atomically persist a validated day. Never overwrites an existing day."""
        if not bars:
            raise ValueError("refusing to store an empty day")
        csv_path, meta_path, hash_path = self._artifacts(day)
        if csv_path.exists() or meta_path.exists() or hash_path.exists():
            raise FileExistsError(
                f"refusing to overwrite accepted day {day.isoformat()} at {csv_path.parent}"
            )

        content = canonical_csv(bars)
        if data_hash and content_hash(bars) != data_hash:
            raise ValueError("data_hash does not match the canonical CSV content")
        if not data_hash:
            data_hash = content_hash(bars)

        metadata = {
            "schema_version": SCHEMA_VERSION,
            "fresh_oos": True,
            "provider": "upstox",
            "interval": INTERVAL,
            "instrument": _instrument_to_meta(instrument),
            "start_date": day.isoformat(),
            "end_date": day.isoformat(),
            "num_bars": len(bars),
            "downloaded_at": downloaded_at,
            "timezone": timezone,
            "data_hash": data_hash,
        }
        meta_text = json.dumps(metadata, indent=2, sort_keys=True) + "\n"
        hash_text = data_hash + "\n"

        self.day_dir(day).mkdir(parents=True, exist_ok=True)
        scratch: list[tuple[Path, str]] = [
            (self._scratch(csv_path), content),
            (self._scratch(meta_path), meta_text),
            (self._scratch(hash_path), hash_text),
        ]
        try:
            for scratch_path, text in scratch:
                self._write_scratch(scratch_path, text)
            # Atomic visibility switch: each os.replace is atomic on the
            # filesystem; all three artifacts are fsynced before any is visible.
            for scratch_path, target_path in zip(
                [s for s, _ in scratch],
                [csv_path, meta_path, hash_path],
            ):
                scratch_path.replace(target_path)
        except OSError:
            for scratch_path, _ in scratch:
                if scratch_path.exists():
                    try:
                        scratch_path.unlink()
                    except OSError:
                        pass
            raise

        return StoredDay(
            day=day,
            directory=self.day_dir(day),
            csv_path=csv_path,
            data_hash=data_hash,
            num_bars=len(bars),
        )

    # ------------------------------------------------------------------ #
    # Reading / verification
    # ------------------------------------------------------------------ #

    def find(self, day: date) -> StoredDay | None:
        """Return the stored day, or ``None`` when absent.

        Recomputes the SHA-256 over ``data.csv`` and requires it to match both
        ``metadata.json.data_hash`` and ``sha256.txt``; any mismatch raises
        :class:`DataInvalidError` (partial/corrupt artifacts must not be
        silently accepted).
        """
        csv_path, meta_path, hash_path = self._artifacts(day)
        present = [p for p in (csv_path, meta_path, hash_path) if p.exists()]
        if not present:
            return None
        if len(present) != 3:
            raise DataInvalidError(
                f"incomplete stored artifacts for {day.isoformat()} at {csv_path.parent} "
                "(manual inspection required)"
            )
        meta = _read_json(meta_path)
        data_hash_field = str(meta.get("data_hash", "") or "")
        num_bars = int(meta.get("num_bars", 0))
        actual_hash = _hash_of_file(csv_path)
        if data_hash_field and actual_hash != data_hash_field:
            raise DataInvalidError(
                f"data.csv for {day.isoformat()} no longer matches its metadata data_hash"
            )
        recorded_hash = csv_path.parent.joinpath(_HASH_NAME).read_text(encoding="utf-8").strip()
        if recorded_hash and recorded_hash != actual_hash:
            raise DataInvalidError(
                f"sha256.txt for {day.isoformat()} does not match the content hash"
            )
        return StoredDay(
            day=day,
            directory=csv_path.parent,
            csv_path=csv_path,
            data_hash=actual_hash,
            num_bars=num_bars,
        )

    def iter_days(self) -> list[StoredDay]:
        """All stored days, sorted chronologically."""
        found: list[StoredDay] = []
        if not self.base_dir.exists():
            return found
        for day_dir in sorted(self.base_dir.iterdir()):
            if not day_dir.is_dir():
                continue
            try:
                day = date.fromisoformat(day_dir.name)
            except ValueError:
                continue
            if (day_dir / _CSV_NAME).exists():
                stored = self.find(day)
                if stored is not None:
                    found.append(stored)
        return found

    # ------------------------------------------------------------------ #
    # Scratch helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    def _scratch(target: Path) -> Path:
        return target.with_name(f".tmp-{target.name}")

    @staticmethod
    def _write_scratch(path: Path, text: str) -> None:
        """Write + flush + fsync a scratch file (crash-safe before rename)."""
        with path.open("w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            import os

            os.fsync(handle.fileno())


def _hash_of_file(path: Path) -> str:
    """SHA-256 over the canonical (``\\n``) view of a CSV file.

    ``dataset_store.save_dataset`` hashes the canonical ``\\n``-joined content
    but writes text without a ``newline=""`` argument, so on Windows the file
    bytes may hold CRLF. Hash the universal-newlines-decoded text instead of
    raw bytes so the digest is platform-independent and matches the recorded
    ``data_hash`` (this is exactly what ``load_dataset`` effectively compares).
    """
    text = path.read_text(encoding="utf-8")  # universal-newline decode: CRLF -> LF
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise DataInvalidError(f"unreadable metadata {path}") from exc
    if not isinstance(payload, dict):
        raise DataInvalidError(f"metadata {path} is not a JSON object")
    return payload


def _instrument_to_meta(instrument: Instrument) -> dict[str, object]:
    return {
        "symbol": instrument.symbol,
        "instrument_type": instrument.instrument_type.value,
        "underlying_symbol": instrument.underlying_symbol,
        "expiry": instrument.expiry.isoformat() if instrument.expiry else None,
        "strike": str(instrument.strike) if instrument.strike is not None else None,
        "option_type": instrument.option_type,
        "exchange": instrument.exchange,
        "exchange_token": instrument.exchange_token,
        "lot_size": instrument.lot_size,
        "tick_size": str(instrument.tick_size),
        "multiplier": instrument.multiplier,
    }


# --------------------------------------------------------------------------- #
# Established ``datasets/`` pool scan (WS 7.26-era sessions and future single-day
# files recorded via the established convention).
# --------------------------------------------------------------------------- #
_ESTABLISHED_META_SUFFIX = ".meta.json"


def scan_established_datasets(
    datasets_dir: str | Path,
    *,
    interval: str = INTERVAL,
    symbol: str = "Nifty 50",
    boundary: date = FRESH_OOS_BOUNDARY,
) -> dict[date, EstablishedDay]:
    """Scan ``datasets/*.meta.json`` for single-day NIFTY 50 5m datasets.

    A dataset qualifies when its interval is 5m, its start and end dates are the
    same day, its instrument symbol matches, and that day is strictly after the
    fresh-OOS boundary. A consumed-window day (e.g. 2026-09-10, saved inside the
    protected 2025-10-06..2026-09-11 window) is NOT reusable fresh coverage and
    is excluded. The CSV hash is recomputed and verified against the recorded
    ``data_hash`` (``verified``).
    """
    datasets_dir = Path(datasets_dir)
    result: dict[date, EstablishedDay] = {}
    if not datasets_dir.exists():
        return result
    token = canonical_interval(interval)
    for meta_path in sorted(datasets_dir.glob(f"*{_ESTABLISHED_META_SUFFIX}")):
        try:
            meta = _read_json(meta_path)
        except DataInvalidError:
            continue
        if str(meta.get("interval", "")) != token:
            continue
        start = str(meta.get("start_date", ""))
        end = str(meta.get("end_date", ""))
        if not start or start != end:
            continue
        instrument_meta = meta.get("instrument") or {}
        if str(instrument_meta.get("symbol", "")) != symbol:
            continue
        try:
            day = date.fromisoformat(start)
        except ValueError:
            continue
        if day <= boundary:
            continue  # consumed protected-window data is not fresh coverage
        name = meta_path.name[: -len(_ESTABLISHED_META_SUFFIX)]
        csv_path = meta_path.with_name(f"{name}.csv")
        if not csv_path.exists():
            continue
        recorded = str(meta.get("data_hash", "") or "")
        actual = _hash_of_file(csv_path)
        result[day] = EstablishedDay(
            day=day,
            name=name,
            csv_path=csv_path,
            meta_path=meta_path,
            data_hash=actual,
            verified=bool(recorded) and actual == recorded,
        )
    return result