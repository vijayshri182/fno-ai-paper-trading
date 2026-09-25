"""Reader for the fresh-OOS pool that re-verifies every shipped guarantee.

The pool loader is a hardened input path for the single-use validation job:

* reads the collector manifest at ``<root>/fresh_oos_manifest.json``;
* considers only accepted statuses (``ACQUIRED`` / ``NOOP`` / ``NOOP_ESTABLISHED``);
* enforces the fresh boundary strictly after 2026-09-11 again (defence in depth);
* recomputes the SHA-256 over each stored canonical CSV and reconciles it with
  the metadata ``data_hash`` and ``sha256.txt``;
* re-runs the 5-minute session structure/order validation per day;
* assembles one chronologically-ordered bar stream.

Any mismatch is a refusal with an explicit machine-readable reason.  Nothing is
repaired, filtered or fabricated.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from fno_ai_paper_trading.data.instrument_registry import get_research_instrument
from fno_ai_paper_trading.fresh_oos.protocol import (
    EXPECTED_BARS_PER_FULL_DAY,
    FRESH_OOS_BOUNDARY,
    SESSION_FIRST_TIME,
)
from fno_ai_paper_trading.models.market import MarketPrice
from fno_ai_paper_trading.fresh_oos_validation.protocol import ACCEPTED_POOL_STATUSES

_CSV_COLUMNS = ("timestamp", "open", "high", "low", "close", "volume", "open_interest")
_INSTRUMENT = get_research_instrument("NIFTY 50")


@dataclass(frozen=True)
class DayRecord:
    """One accepted pool day with its re-verified provenance."""

    day: date
    status: str
    num_bars: int
    data_hash: str
    source: str
    stored_path: Path
    bars_valid: bool


@dataclass(frozen=True)
class PoolReport:
    """Snapshot of the consumable fresh pool after full verification."""

    days: tuple[DayRecord, ...]
    bars: tuple[MarketPrice, ...]
    schema_version: str

    @property
    def num_days(self) -> int:
        return len(self.days)

    @property
    def num_bars_count(self) -> int:
        return len(self.bars)

    @property
    def ordered(self) -> bool:
        stamps = [bar.timestamp for bar in self.bars]
        return all(a < b for a, b in zip(stamps, stamps[1:]))

    def per_day_hashes(self) -> dict[str, str]:
        return {rec.day.isoformat(): rec.data_hash for rec in self.days}

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "days": [rec.day.isoformat() for rec in self.days],
            "bars": self.num_bars_count,
            "per_day_hashes": self.per_day_hashes(),
        }


class PoolViolationError(Exception):
    """A re-verification failure that must block validation (never repaired)."""


def load_pool(root: Path, *, datasets_dir: Path | None = None) -> PoolReport:
    """Load and re-verify the consumable fresh pool under ``root``.

    Raises :class:`PoolViolationError` on the first hard refusal with a stable
    message; returns a :class:`PoolReport` only when every day passes.
    """
    root = Path(root)
    datasets_dir = Path(datasets_dir) if datasets_dir is not None else root.parent.parent / "datasets"
    manifest_path = root / "fresh_oos_manifest.json"
    if not manifest_path.is_file():
        raise PoolViolationError(f"manifest missing: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    schema_version = str(manifest.get("schema_version", ""))
    pool = manifest.get("pool") or {}
    if not isinstance(pool, dict) or not pool:
        raise PoolViolationError(f"manifest at {manifest_path} has no consumable pool")

    records: list[DayRecord] = []
    all_bars: list[MarketPrice] = []
    for day_text, entry in sorted(pool.items()):
        day = date.fromisoformat(str(day_text))
        if day <= FRESH_OOS_BOUNDARY:
            raise PoolViolationError(
                f"pool day {day.isoformat()} is on/before the fresh boundary "
                f"{FRESH_OOS_BOUNDARY.isoformat()}; contaminated pool"
            )
        status = str(entry.get("status", ""))
        if status not in ACCEPTED_POOL_STATUSES:
            continue
        expected_bars = int(entry.get("num_bars", 0) or 0)
        source = str(entry.get("source") or entry.get("src") or "")
        csv_path = _resolve_csv(root, datasets_dir, day, status, source)
        data_hash = _lf_hash(csv_path)
        bars = _parse_csv(csv_path, day)
        if len(bars) != expected_bars:
            raise PoolViolationError(
                f"{day.isoformat()}: manifest declares {expected_bars} bars but store "
                f"holds {len(bars)}"
            )
        if len(bars) < EXPECTED_BARS_PER_FULL_DAY:
            raise PoolViolationError(
                f"{day.isoformat()}: incomplete session ({len(bars)} bars < "
                f"{EXPECTED_BARS_PER_FULL_DAY}); incomplete days never enter validation"
            )
        _reconcile_hash(root, day, csv_path, data_hash)
        records.append(
            DayRecord(
                day=day,
                status=status,
                num_bars=len(bars),
                data_hash=data_hash,
                source=source,
                stored_path=csv_path,
                bars_valid=True,
            )
        )
        all_bars.extend(bars)

    if not records:
        raise PoolViolationError("no accepted pool days to validate")

    stamps = [bar.timestamp for bar in all_bars]
    if any(a >= b for a, b in zip(stamps, stamps[1:])):
        raise PoolViolationError("pool bar stream is not strictly chronologically ordered")
    return PoolReport(days=tuple(records), bars=tuple(all_bars), schema_version=schema_version)


def _resolve_csv(root: Path, datasets_dir: Path, day: date, status: str, source: str) -> Path:
    store_path = root / "NIFTY_50_5m" / day.isoformat() / "data.csv"
    if store_path.is_file():
        return store_path
    if status == "NOOP_ESTABLISHED":
        compact = day.strftime("%Y%m%d")
        candidate = datasets_dir / f"upstox_Nifty_50_5m_{compact}_{compact}.csv"
        if candidate.is_file():
            return candidate
    if source:
        candidate = Path(source)
        if candidate.is_file():
            return candidate
        rel = Path(source.replace("\\", "/"))
        if rel.is_file():
            return rel
    raise PoolViolationError(
        f"{day.isoformat()}: no stored artifact found (status={status}, source={source!r})"
    )


def _reconcile_hash(root: Path, day: date, csv_path: Path, data_hash: str) -> None:
    day_dir = csv_path.parent
    meta_path = root / "NIFTY_50_5m" / day.isoformat() / "metadata.json"
    if meta_path.is_file():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        recorded = str(meta.get("data_hash", "")).lower()
        if recorded and recorded != data_hash:
            raise PoolViolationError(
                f"{day.isoformat()}: stored CSV hash {data_hash} does not match metadata "
                f"data_hash {recorded}"
            )
        if int(meta.get("num_bars", -1)) != -1 and int(meta["num_bars"]) != expected_of(day):
            raise PoolViolationError(
                f"{day.isoformat()}: metadata num_bars {meta['num_bars']} inconsistent"
            )
    hash_path = root / "NIFTY_50_5m" / day.isoformat() / "sha256.txt"
    if hash_path.is_file():
        recorded_hash = hash_path.read_text(encoding="utf-8").strip().lower()
        if recorded_hash and recorded_hash != data_hash:
            raise PoolViolationError(
                f"{day.isoformat()}: stored CSV hash {data_hash} does not match sha256.txt "
                f"{recorded_hash}"
            )


def _lf_hash(path: Path) -> str:
    """SHA-256 over the canonical LF view of a CSV file.

    Mirrors ``fresh_oos.store._hash_of_file``: the collector records the
    digest of the LF-joined canonical text, and on Windows the written bytes
    may hold CRLF, so the digest must be computed over the universal-newline
    decoded text to be platform-independent and byte-comparable.
    """
    text = path.read_text(encoding="utf-8")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def expected_of(day: date) -> int:
    return EXPECTED_BARS_PER_FULL_DAY


def _parse_csv(path: Path, day: date) -> list[MarketPrice]:
    """Parse a canonical single-day CSV into a validated, ordered bar list."""
    from fno_ai_paper_trading.fresh_oos.validation import validate_5m_day

    rows: list[MarketPrice] = []
    with path.open(encoding="utf-8") as handle:
        header = handle.readline().strip()
        if header.split(",") != list(_CSV_COLUMNS):
            raise PoolViolationError(f"{day.isoformat()}: unexpected CSV header {header!r}")
        for line_number, line in enumerate(handle, start=2):
            line = line.strip()
            if not line:
                continue
            parts = line.split(",")
            if len(parts) != len(_CSV_COLUMNS):
                raise PoolViolationError(
                    f"{day.isoformat()}: malformed row at line {line_number}"
                )
            timestamp = datetime.fromisoformat(parts[0])
            rows.append(
                MarketPrice(
                    instrument=_INSTRUMENT,
                    timestamp=timestamp,
                    open=Decimal(parts[1]),
                    high=Decimal(parts[2]),
                    low=Decimal(parts[3]),
                    close=Decimal(parts[4]),
                    volume=int(parts[5]) if parts[5] else 0,
                    open_interest=int(parts[6]) if parts[6] else None,
                )
            )
    result = validate_5m_day(rows, day, boundary=FRESH_OOS_BOUNDARY)
    if not result.ok:
        raise PoolViolationError(
            f"{day.isoformat()}: structure validation failed ({result.codes})"
        )
    return rows