"""Local persistence of normalized OHLCV bars to CSV + a JSON metadata manifest.

Research datasets can be downloaded once from a provider and re-used offline and
deterministically. Saving produces two files:

* ``<name>.csv`` — one canonical OHLCV bar per row (ISO-8601 naive timestamps,
  ``str(Decimal)`` values), sorted chronologically;
* ``<name>.meta.json`` — a metadata manifest holding the provider, interval,
  normalized instrument, date range, row count, checkpoint time and a
  deterministic ``data_hash`` (SHA-256 over the canonical CSV content).

``data_hash`` lets research code detect a changed dataset (identical bytes must
hash identically) without a database. Timestamps are stored without a timezone
(naive IST), matching the rest of the domain models and the providers.
"""
from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime
from datetime import timezone as _utc
from decimal import Decimal
from pathlib import Path
from typing import Any

from fno_ai_paper_trading.data.intervals import canonical_interval
from fno_ai_paper_trading.models.enums import InstrumentType
from fno_ai_paper_trading.models.instruments import Instrument
from fno_ai_paper_trading.models.market import MarketPrice

SCHEMA_VERSION = "1"

_CSV_COLUMNS = ("timestamp", "open", "high", "low", "close", "volume", "open_interest")
_META_SUFFIX = ".meta.json"

# Characters not safe in a file name.
_UNSAFE = frozenset('\\/:*?"<>|')


def _safe_name(value: str) -> str:
    return "".join("_" if ch in _UNSAFE or ch.isspace() else ch for ch in value)


@dataclass(frozen=True)
class StoredDataset:
    """A dataset loaded from or saved to disk."""

    bars: list[MarketPrice]
    metadata: dict[str, Any]
    path: Path

    @property
    def data_hash(self) -> str:
        return self.metadata["data_hash"]

    def instrument(self) -> Instrument:
        return _instrument_from_meta(self.metadata["instrument"])


def save_dataset(
    bars: list[MarketPrice],
    *,
    instrument: Instrument,
    provider: str,
    interval: str,
    directory: str | Path = "datasets",
    name: str | None = None,
    downloaded_at: datetime | None = None,
    timezone: str = "Asia/Kolkata",
) -> StoredDataset:
    """Write ``bars`` (plus metadata) into ``directory`` and return a descriptor.

    ``bars`` must be chronologically ordered with unique timestamps — silently
    re-sorting or de-duplicating would silently change research inputs, so a
    mis-ordered input raises :class:`ValueError` instead.
    """
    if not bars:
        raise ValueError("save_dataset requires at least one bar")
    for previous, current in zip(bars, bars[1:]):
        if current.timestamp <= previous.timestamp:
            raise ValueError("bars must be chronologically ordered with unique timestamps")

    token = canonical_interval(interval)
    if token is None:
        raise ValueError(f"unknown interval {interval!r}")
    interval = token

    if not provider.strip():
        raise ValueError("provider must not be empty")

    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)

    start_date = bars[0].timestamp.date()
    end_date = bars[-1].timestamp.date()
    if name is None:
        name = f"{_safe_name(provider)}_{_safe_name(instrument.symbol)}_{interval}_{start_date:%Y%m%d}_{end_date:%Y%m%d}"
    name = _safe_name(name)

    csv_path = directory / f"{name}.csv"
    meta_path = directory / f"{name}{_META_SUFFIX}"

    content = _canonical_csv(bars)
    csv_path.write_text(content, encoding="utf-8")
    data_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()

    metadata = {
        "schema_version": SCHEMA_VERSION,
        "provider": provider.strip(),
        "interval": interval,
        "instrument": _instrument_to_meta(instrument),
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "num_bars": len(bars),
        "downloaded_at": (downloaded_at or datetime.now(_utc.utc)).isoformat(timespec="seconds") + "Z",
        "timezone": timezone,
        "data_hash": data_hash,
    }
    meta_path.write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return StoredDataset(bars=list(bars), metadata=metadata, path=csv_path)


def load_dataset(path: str | Path) -> StoredDataset:
    """Load a dataset from its CSV file (sidecar metadata is required)."""
    csv_path = Path(path)
    meta_path = csv_path.with_name(csv_path.stem + _META_SUFFIX)
    if not meta_path.exists():
        raise FileNotFoundError(
            f"dataset metadata {meta_path} is missing; re-save the dataset with save_dataset()"
        )

    with meta_path.open("r", encoding="utf-8") as handle:
        metadata = json.load(handle)
    instrument = _instrument_from_meta(metadata["instrument"])
    interval = metadata.get("interval", "day")

    bars: list[MarketPrice] = []
    with csv_path.open("r", newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None or list(reader.fieldnames) != list(_CSV_COLUMNS):
            raise ValueError(
                f"unexpected CSV columns in {csv_path}; expected {list(_CSV_COLUMNS)}"
            )
        for row in reader:
            timestamp = datetime.fromisoformat(row["timestamp"])
            open_interest = row["open_interest"]
            bars.append(
                MarketPrice(
                    instrument=instrument,
                    timestamp=timestamp,
                    open=Decimal(row["open"]),
                    high=Decimal(row["high"]),
                    low=Decimal(row["low"]),
                    close=Decimal(row["close"]),
                    volume=int(row["volume"]),
                    open_interest=int(open_interest) if open_interest not in ("", None) else None,
                )
            )

    actual_hash = hashlib.sha256(_canonical_csv(bars).encode("utf-8")).hexdigest()
    expected_hash = metadata.get("data_hash")
    if expected_hash and actual_hash != expected_hash:
        raise ValueError(
            f"data_hash mismatch for {csv_path} (file was modified after saving)"
        )
    return StoredDataset(bars=bars, metadata=metadata, path=csv_path)


def dataset_hash(bars: list[MarketPrice]) -> str:
    """Deterministic SHA-256 over the canonical CSV rows of ``bars``."""
    return hashlib.sha256(_canonical_csv(bars).encode("utf-8")).hexdigest()


def _canonical_csv(bars: list[MarketPrice]) -> str:
    lines = [",".join(_CSV_COLUMNS)]
    for bar in bars:
        oi = "" if bar.open_interest is None else str(bar.open_interest)
        lines.append(
            ",".join(
                (
                    bar.timestamp.isoformat(),
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


def _instrument_to_meta(instrument: Instrument) -> dict[str, Any]:
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


def _instrument_from_meta(meta: dict[str, Any]) -> Instrument:
    expiry = date.fromisoformat(meta["expiry"]) if meta.get("expiry") else None
    strike = Decimal(meta["strike"]) if meta.get("strike") is not None else None
    return Instrument(
        symbol=meta["symbol"],
        instrument_type=InstrumentType(meta["instrument_type"]),
        underlying_symbol=meta["underlying_symbol"],
        expiry=expiry,
        strike=strike,
        option_type=meta.get("option_type"),
        exchange=meta.get("exchange", "NSE"),
        exchange_token=meta.get("exchange_token"),
        lot_size=int(meta.get("lot_size", 1)),
        tick_size=Decimal(meta.get("tick_size", "0.05")),
        multiplier=int(meta.get("multiplier", 1)),
    )