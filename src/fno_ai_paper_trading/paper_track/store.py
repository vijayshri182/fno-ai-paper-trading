"""Honest persistence for the Daily Paper Trading Track.

Checkpoints, reports and the run manifest are JSON files written atomically
(temp file + ``os.replace``) and guarded by a SHA-256 sidecar so a torn or
altered file is detected before it is ever trusted. Decimal/datetime/enum model
fields are serialized as primitive strings/ISO values; deserialization rebuilds
real domain objects. Byte-stable dumps (sorted keys) make hash verification and
deterministic-rerun comparisons possible.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from fno_ai_paper_trading.models.enums import InstrumentType, OrderSide, OrderStatus, OrderType
from fno_ai_paper_trading.models.instruments import Instrument
from fno_ai_paper_trading.models.market import MarketPrice
from fno_ai_paper_trading.models.order import Fill, Order
from fno_ai_paper_trading.models.position import Position, Trade
from fno_ai_paper_trading.paper_track.errors import TrackCheckpointError

__all__ = ["TrackStore", "stable_dumps", "hash_bytes"]

_SCHEMA_VERSION = 1


def _dec(value: Any) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))


def _dt(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    return datetime.fromisoformat(value)


# --------------------------------------------------------------------------- #
# Model codecs (primitives in, domain objects out)
# --------------------------------------------------------------------------- #

def instrument_to_dict(instrument: Instrument) -> dict:
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


def instrument_from_dict(data: dict) -> Instrument:
    return Instrument(
        symbol=data["symbol"],
        instrument_type=InstrumentType(data["instrument_type"]),
        underlying_symbol=data["underlying_symbol"],
        expiry=date.fromisoformat(data["expiry"]) if data.get("expiry") else None,
        strike=_dec(data.get("strike")),
        option_type=data.get("option_type"),
        exchange=data.get("exchange", "NSE"),
        exchange_token=data.get("exchange_token"),
        lot_size=data.get("lot_size", 1),
        tick_size=_dec(data.get("tick_size", "0.05")),
        multiplier=data.get("multiplier", 1),
    )


def order_to_dict(order: Order) -> dict:
    return {
        "instrument": instrument_to_dict(order.instrument),
        "side": order.side.value,
        "quantity": order.quantity,
        "order_type": order.order_type.value,
        "status": order.status.value,
        "created_at": order.created_at.isoformat(),
        "submitted_at": order.submitted_at.isoformat() if order.submitted_at else None,
        "filled_at": order.filled_at.isoformat() if order.filled_at else None,
        "filled_quantity": order.filled_quantity,
        "average_fill_price": str(order.average_fill_price),
        "order_id": order.order_id,
        "rejection_reason": order.rejection_reason,
    }


def order_from_dict(data: dict) -> Order:
    return Order(
        instrument=instrument_from_dict(data["instrument"]),
        side=OrderSide(data["side"]),
        quantity=data["quantity"],
        order_type=OrderType(data["order_type"]),
        status=OrderStatus(data["status"]),
        created_at=datetime.fromisoformat(data["created_at"]),
        submitted_at=_dt(data.get("submitted_at")),
        filled_at=_dt(data.get("filled_at")),
        filled_quantity=data.get("filled_quantity", 0),
        average_fill_price=_dec(data.get("average_fill_price", "0")) or Decimal("0"),
        order_id=data.get("order_id"),
        rejection_reason=data.get("rejection_reason"),
    )


def fill_to_dict(fill: Fill) -> dict:
    return {
        "order_id": fill.order_id,
        "instrument": instrument_to_dict(fill.instrument),
        "side": fill.side.value,
        "quantity": fill.quantity,
        "price": str(fill.price),
        "commission": str(fill.commission),
        "filled_at": fill.filled_at.isoformat(),
    }


def fill_from_dict(data: dict) -> Fill:
    return Fill(
        order_id=data["order_id"],
        instrument=instrument_from_dict(data["instrument"]),
        side=OrderSide(data["side"]),
        quantity=data["quantity"],
        price=_dec(data["price"]) or Decimal("0"),
        commission=_dec(data["commission"]) or Decimal("0"),
        filled_at=datetime.fromisoformat(data["filled_at"]),
    )


def position_to_dict(position: Position) -> dict:
    return {
        "instrument": instrument_to_dict(position.instrument),
        "quantity": position.quantity,
        "average_entry_price": str(position.average_entry_price),
        "realized_pnl": str(position.realized_pnl),
        "opened_at": position.opened_at.isoformat(),
    }


def position_from_dict(data: dict) -> Position:
    return Position(
        instrument=instrument_from_dict(data["instrument"]),
        quantity=data["quantity"],
        average_entry_price=_dec(data["average_entry_price"]) or Decimal("0"),
        realized_pnl=_dec(data.get("realized_pnl", "0")) or Decimal("0"),
        opened_at=datetime.fromisoformat(data["opened_at"]),
    )


def trade_to_dict(trade: Trade) -> dict:
    return {
        "trade_id": trade.trade_id,
        "instrument": instrument_to_dict(trade.instrument),
        "side": trade.side.value,
        "quantity": trade.quantity,
        "price": str(trade.price),
        "commission": str(trade.commission),
        "executed_at": trade.executed_at.isoformat(),
        "realized_pnl": str(trade.realized_pnl),
    }


def trade_from_dict(data: dict) -> Trade:
    return Trade(
        trade_id=data["trade_id"],
        instrument=instrument_from_dict(data["instrument"]),
        side=OrderSide(data["side"]),
        quantity=data["quantity"],
        price=_dec(data["price"]) or Decimal("0"),
        commission=_dec(data["commission"]) or Decimal("0"),
        executed_at=datetime.fromisoformat(data["executed_at"]),
        realized_pnl=_dec(data.get("realized_pnl", "0")) or Decimal("0"),
    )


def market_price_to_dict(bar: MarketPrice) -> dict:
    return {
        "instrument": instrument_to_dict(bar.instrument),
        "timestamp": bar.timestamp.isoformat(),
        "open": str(bar.open),
        "high": str(bar.high),
        "low": str(bar.low),
        "close": str(bar.close),
        "volume": bar.volume,
        "open_interest": bar.open_interest,
    }


def market_price_from_dict(data: dict) -> MarketPrice:
    return MarketPrice(
        instrument=instrument_from_dict(data["instrument"]),
        timestamp=datetime.fromisoformat(data["timestamp"]),
        open=_dec(data["open"]) or Decimal("0"),
        high=_dec(data["high"]) or Decimal("0"),
        low=_dec(data["low"]) or Decimal("0"),
        close=_dec(data["close"]) or Decimal("0"),
        volume=data.get("volume", 0),
        open_interest=data.get("open_interest"),
    )


# --------------------------------------------------------------------------- #
# Stable JSON
# --------------------------------------------------------------------------- #

def stable_dumps(payload: dict) -> str:
    """Deterministic JSON string (sorted keys, compact separators, LF only)."""
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    )


def hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------------- #
# Atomic storage
# --------------------------------------------------------------------------- #

class TrackStore:
    """File layout + atomic, hashed persistence for one account."""

    def __init__(self, store_dir: Path, account: str) -> None:
        self.store_dir = Path(store_dir)
        self.account = account
        self.checkpoints_dir = self.store_dir / "checkpoints"
        self.reports_dir = self.store_dir / "reports"
        self.locks_dir = self.store_dir / "locks"
        self.manifest_path = self.store_dir / "manifest.json"

    def checkpoint_path(self, day: date) -> Path:
        return self.checkpoints_dir / f"{self.account}.{day.isoformat()}.json"

    def report_path(self, day: date) -> Path:
        return self.reports_dir / f"{self.account}.{day.isoformat()}.json"

    def _sha256_path(self, path: Path) -> Path:
        return path.with_name(path.name + ".sha256")

    def write_hash(self, path: Path) -> None:
        digest = hash_bytes(path.read_bytes())
        self._sha256_path(path).write_text(digest, encoding="ascii")

    def write_atomic(self, path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        text = stable_dumps(payload)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
        self.write_hash(path)

    def read_verified(self, path: Path) -> dict | None:
        if not path.exists():
            return None
        sha = self._sha256_path(path)
        if not sha.exists():
            raise TrackCheckpointError(f"missing sha256 sidecar for {path.name}")
        actual = hash_bytes(path.read_bytes())
        expected = sha.read_text(encoding="ascii").strip()
        if actual != expected:
            raise TrackCheckpointError(
                f"sha256 mismatch for {path.name}: expected {expected}, got {actual}"
            )
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise TrackCheckpointError(f"corrupt json in {path.name}: {exc}") from exc
        if not isinstance(payload, dict):
            raise TrackCheckpointError(f"unexpected payload shape in {path.name}")
        return payload

    # -- checkpoint ------------------------------------------------

    def save_checkpoint(self, day: date, payload: dict) -> None:
        self.write_atomic(self.checkpoint_path(day), payload)

    def load_checkpoint(self, day: date) -> dict | None:
        return self.read_verified(self.checkpoint_path(day))

    # -- report ----------------------------------------------------

    def save_report(self, day: date, payload: dict) -> None:
        self.write_atomic(self.report_path(day), payload)

    def load_report(self, day: date) -> dict | None:
        return self.read_verified(self.report_path(day))

    # -- manifest --------------------------------------------------

    def manifest(self) -> dict:
        if not self.manifest_path.exists():
            return {"schema_version": _SCHEMA_VERSION, "runs": {}}
        return self.read_verified(self.manifest_path)

    def update_run(self, run_id: str, **fields: Any) -> None:
        manifest = self.manifest()
        entry = manifest.setdefault("runs", {}).setdefault(run_id, {})
        entry.update(fields)
        entry.setdefault("schema_version", _SCHEMA_VERSION)
        self.write_atomic(self.manifest_path, manifest)

    def list_runs(self) -> dict[str, dict]:
        return self.manifest().get("runs", {})

    def hashes(self) -> dict[str, str]:
        """sha256 digests of every persisted artifact (labelled by relative path)."""
        out: dict[str, str] = {}
        for folder in ("checkpoints", "reports"):
            root = self.store_dir / folder
            if not root.exists():
                continue
            for file in sorted(root.glob(f"{self.account}.*.json")):
                sha = self._sha256_path(file)
                key = f"{folder}/{file.name}"
                if sha.exists():
                    out[key] = sha.read_text(encoding="ascii").strip()
        return out