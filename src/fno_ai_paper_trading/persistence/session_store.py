"""Local persistence of paper-session state to JSON + a JSON metadata manifest.

A running ``PaperSession`` can be checkpointed and, after a restart, restored
into a fresh runtime. Saving produces two files per snapshot:

* ``<name>.json`` — a canonical JSON payload holding the full session state
  (instrument, interval, warm-up gate, portfolio, broker ledger, consumed
  candles, entry candles and session counters);
* ``<name>.meta.json`` — a metadata manifest with the schema version, save time,
  identity fields and a deterministic ``state_hash`` (SHA-256 over the canonical
  payload JSON content).

``state_hash`` detects a modified/corrupted payload without a database:
identical payloads must hash identically, so the hash is computed over the
re-serialized canonical form (``json.dumps(indent=2, sort_keys=True)``). All
timestamps are stored without a timezone (naive IST), matching the rest of the
domain models and the providers. All money values are stored as ``str(Decimal)``
losslessly and restored exactly.

Persistence is pure: this module contains **no** accounting, **no** stop
arithmetic, and **no** ``backtest.*`` imports. Restoring a snapshot never re-runs
``Portfolio.apply_fill``; it only deserializes previously recorded results, so
``Portfolio.apply_fill`` remains the sole accounting path.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime
from datetime import timezone as _utc
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from fno_ai_paper_trading.models.enums import InstrumentType, OrderSide, OrderStatus, OrderType
from fno_ai_paper_trading.models.instruments import Instrument
from fno_ai_paper_trading.models.order import Fill, Order
from fno_ai_paper_trading.models.position import Position, Trade
from fno_ai_paper_trading.portfolio.portfolio import Portfolio

SCHEMA_VERSION = "1"

#: Default snapshot directory (matches ``PaperSettings.paper_state_dir``).
DEFAULT_STATE_DIR = "paper_state"

_META_SUFFIX = ".meta.json"

# Characters not safe in a file name.
_UNSAFE = frozenset('\\/:*?"<>|')


def _safe_name(value: str) -> str:
    return "".join("_" if ch in _UNSAFE or ch.isspace() else ch for ch in value)


@dataclass(frozen=True)
class SessionSnapshot:
    """A point-in-time, self-contained capture of a running paper session.

    Created by :meth:`PaperSession.snapshot`, exchanged through
    :func:`save_session` / :func:`load_session` and applied to a fresh session by
    :meth:`PaperSession.restore`. Mutable collections are detached copies at
    capture time, so the snapshot is stable regardless of later session activity.
    """

    instrument: Instrument
    interval_token: str
    interval_minutes: int
    warmup_bars: int
    quantity: int
    portfolio: Portfolio
    orders: tuple[Order, ...]
    fills: tuple[Fill, ...]
    consumed_timestamps: tuple[datetime, ...]
    entry_candles: tuple[tuple[str, datetime], ...]
    orders_submitted: int
    fills_count: int
    trades_count: int
    rejections: int
    skips: int


@dataclass(frozen=True)
class StoredSession:
    """A session snapshot loaded from or saved to disk."""

    snapshot: SessionSnapshot
    metadata: dict[str, Any]
    path: Path

    @property
    def state_hash(self) -> str:
        return self.metadata["state_hash"]


def save_session(
    snapshot: SessionSnapshot,
    *,
    directory: str | Path = DEFAULT_STATE_DIR,
    name: str | None = None,
    saved_at: datetime | None = None,
) -> StoredSession:
    """Write ``snapshot`` into ``directory`` and return a descriptor.

    The payload is serialized canonically (``json.dumps(indent=2,
    sort_keys=True)`` + trailing newline) so identical snapshots produce
    byte-identical files and therefore identical ``state_hash`` values.
    ``name`` is sanitized for file-system use; when omitted it defaults to
    ``paper_{symbol}_{interval}``.
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)

    safe_symbol = _safe_name(snapshot.instrument.symbol)
    if name is None:
        name = f"paper_{safe_symbol}_{snapshot.interval_token}"
    name = _safe_name(name)
    if not name.strip():
        raise ValueError("session state name must not be empty")

    payload_path = directory / f"{name}.json"
    meta_path = directory / f"{name}{_META_SUFFIX}"

    payload = _snapshot_to_payload(snapshot)
    content = _canonical_json(payload)
    payload_path.write_text(content, encoding="utf-8")
    state_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()

    metadata = {
        "schema_version": SCHEMA_VERSION,
        "name": name,
        "instrument": snapshot.instrument.symbol,
        "instrument_token": snapshot.instrument.exchange_token,
        "interval": snapshot.interval_token,
        "warmup_bars": snapshot.warmup_bars,
        "saved_at": (saved_at or datetime.now(_utc.utc)).isoformat(timespec="seconds") + "Z",
        "num_orders": len(snapshot.orders),
        "num_fills": len(snapshot.fills),
        "num_trades": len(snapshot.portfolio.trade_history),
        "num_positions": len(snapshot.portfolio.open_positions()),
        "state_hash": state_hash,
    }
    meta_path.write_text(_canonical_json(metadata), encoding="utf-8")
    return StoredSession(snapshot=snapshot, metadata=metadata, path=payload_path)


def load_session(payload_path: str | Path) -> StoredSession:
    """Load a session snapshot from its payload file (sidecar metadata required)."""
    payload_path = Path(payload_path)
    meta_path = payload_path.with_suffix(_META_SUFFIX)
    if not meta_path.exists():
        raise FileNotFoundError(
            f"session metadata {meta_path} is missing; re-save the session with save_session()"
        )
    with meta_path.open("r", encoding="utf-8") as handle:
        metadata = json.load(handle)
    if metadata.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(
            f"unsupported session schema version {metadata.get('schema_version')!r}; "
            f"expected {SCHEMA_VERSION!r}"
        )

    with payload_path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(
            f"unsupported payload schema version {payload.get('schema_version')!r}; "
            f"expected {SCHEMA_VERSION!r}"
        )

    actual_hash = hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()
    expected_hash = metadata.get("state_hash")
    if expected_hash and actual_hash != expected_hash:
        raise ValueError(
            f"state_hash mismatch for {payload_path} "
            "(file was modified after saving; refusing to restore)"
        )

    return StoredSession(
        snapshot=_payload_to_snapshot(payload),
        metadata=metadata,
        path=payload_path,
    )


def _snapshot_to_payload(snapshot: SessionSnapshot) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "instrument": _instrument_to_dict(snapshot.instrument),
        "interval_token": snapshot.interval_token,
        "interval_minutes": snapshot.interval_minutes,
        "warmup_bars": snapshot.warmup_bars,
        "quantity": snapshot.quantity,
        "portfolio": {
            "cash": str(snapshot.portfolio.cash),
            "realized_pnl": str(snapshot.portfolio.realized_pnl),
            "initial_cash": str(snapshot.portfolio.initial_cash),
            "long_only": snapshot.portfolio.long_only,
            "positions": [
                _position_to_dict(position)
                for position in snapshot.portfolio.positions.values()
                if not position.is_flat
            ],
            "trade_history": [_trade_to_dict(trade) for trade in snapshot.portfolio.trade_history],
        },
        "orders": [_order_to_dict(order) for order in snapshot.orders],
        "fills": [_fill_to_dict(fill) for fill in snapshot.fills],
        "consumed_timestamps": sorted(
            timestamp.isoformat() for timestamp in snapshot.consumed_timestamps
        ),
        "entry_candles": [
            [symbol, timestamp.isoformat()]
            for symbol, timestamp in sorted(snapshot.entry_candles)
        ],
        "counters": {
            "orders_submitted": snapshot.orders_submitted,
            "fills_count": snapshot.fills_count,
            "trades_count": snapshot.trades_count,
            "rejections": snapshot.rejections,
            "skips": snapshot.skips,
        },
    }


def _payload_to_snapshot(payload: dict[str, Any]) -> SessionSnapshot:
    portfolio_dict = payload["portfolio"]
    positions = {
        position_dict["instrument"]["symbol"]: _position_from_dict(position_dict)
        for position_dict in portfolio_dict.get("positions", [])
    }
    trade_history = [_trade_from_dict(trade) for trade in portfolio_dict.get("trade_history", [])]
    portfolio = Portfolio(
        _money(portfolio_dict["cash"], "cash"),
        positions=positions,
        trade_history=trade_history,
        realized_pnl=_money(portfolio_dict["realized_pnl"], "realized_pnl"),
        long_only=bool(portfolio_dict.get("long_only", False)),
    )
    portfolio.initial_cash = _money(portfolio_dict["initial_cash"], "initial_cash")

    return SessionSnapshot(
        instrument=_instrument_from_dict(payload["instrument"]),
        interval_token=payload["interval_token"],
        interval_minutes=_int(payload["interval_minutes"], "interval_minutes"),
        warmup_bars=_int(payload["warmup_bars"], "warmup_bars"),
        quantity=_int(payload["quantity"], "quantity"),
        portfolio=portfolio,
        orders=tuple(_order_from_dict(order) for order in payload.get("orders", [])),
        fills=tuple(_fill_from_dict(fill) for fill in payload.get("fills", [])),
        consumed_timestamps=tuple(
            datetime.fromisoformat(value) for value in payload.get("consumed_timestamps", [])
        ),
        entry_candles=tuple(
            (symbol, datetime.fromisoformat(timestamp))
            for symbol, timestamp in payload.get("entry_candles", [])
        ),
        orders_submitted=_int(payload["counters"]["orders_submitted"], "orders_submitted"),
        fills_count=_int(payload["counters"]["fills_count"], "fills_count"),
        trades_count=_int(payload["counters"]["trades_count"], "trades_count"),
        rejections=_int(payload["counters"]["rejections"], "rejections"),
        skips=_int(payload["counters"]["skips"], "skips"),
    )


def _canonical_json(value: dict[str, Any]) -> str:
    return json.dumps(value, indent=2, sort_keys=True) + "\n"


# --------------------------------------------------------------------------- #
# Model serialization
# --------------------------------------------------------------------------- #


def _instrument_to_dict(instrument: Instrument) -> dict[str, Any]:
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


def _instrument_from_dict(data: dict[str, Any]) -> Instrument:
    expiry = date.fromisoformat(data["expiry"]) if data.get("expiry") else None
    strike = _money(data["strike"], "strike") if data.get("strike") is not None else None
    return Instrument(
        symbol=data["symbol"],
        instrument_type=InstrumentType(data["instrument_type"]),
        underlying_symbol=data["underlying_symbol"],
        expiry=expiry,
        strike=strike,
        option_type=data.get("option_type"),
        exchange=data.get("exchange", "NSE"),
        exchange_token=data.get("exchange_token"),
        lot_size=_int(data.get("lot_size", 1), "lot_size"),
        tick_size=_money(data.get("tick_size", "0.05"), "tick_size"),
        multiplier=_int(data.get("multiplier", 1), "multiplier"),
    )


def _position_to_dict(position: Position) -> dict[str, Any]:
    return {
        "instrument": _instrument_to_dict(position.instrument),
        "quantity": position.quantity,
        "average_entry_price": str(position.average_entry_price),
        "realized_pnl": str(position.realized_pnl),
        "opened_at": position.opened_at.isoformat(),
    }


def _position_from_dict(data: dict[str, Any]) -> Position:
    position = Position(
        instrument=_instrument_from_dict(data["instrument"]),
        quantity=_int(data["quantity"], "position quantity"),
        average_entry_price=_money(data["average_entry_price"], "average_entry_price"),
        opened_at=datetime.fromisoformat(data["opened_at"]),
    )
    # realized_pnl on a live position accumulates partial-close P&L and may be
    # negative; Position.__post_init__ only accepts non-negative values, so the
    # recorded value is applied after construction (matching portfolio mutation).
    position.realized_pnl = _money(data["realized_pnl"], "position realized_pnl")
    return position


def _trade_to_dict(trade: Trade) -> dict[str, Any]:
    return {
        "trade_id": trade.trade_id,
        "instrument": _instrument_to_dict(trade.instrument),
        "side": trade.side.value,
        "quantity": trade.quantity,
        "price": str(trade.price),
        "commission": str(trade.commission),
        "executed_at": trade.executed_at.isoformat(),
        "realized_pnl": str(trade.realized_pnl),
    }


def _trade_from_dict(data: dict[str, Any]) -> Trade:
    return Trade(
        trade_id=data["trade_id"],
        instrument=_instrument_from_dict(data["instrument"]),
        side=OrderSide(data["side"]),
        quantity=_int(data["quantity"], "trade quantity"),
        price=_money(data["price"], "trade price"),
        commission=_money(data["commission"], "trade commission"),
        executed_at=datetime.fromisoformat(data["executed_at"]),
        realized_pnl=_money(data["realized_pnl"], "trade realized_pnl"),
    )


def _order_to_dict(order: Order) -> dict[str, Any]:
    return {
        "instrument": _instrument_to_dict(order.instrument),
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


def _order_from_dict(data: dict[str, Any]) -> Order:
    return Order(
        instrument=_instrument_from_dict(data["instrument"]),
        side=OrderSide(data["side"]),
        quantity=_int(data["quantity"], "order quantity"),
        order_type=OrderType(data["order_type"]),
        status=OrderStatus(data["status"]),
        created_at=datetime.fromisoformat(data["created_at"]),
        submitted_at=datetime.fromisoformat(data["submitted_at"]) if data.get("submitted_at") else None,
        filled_at=datetime.fromisoformat(data["filled_at"]) if data.get("filled_at") else None,
        filled_quantity=_int(data["filled_quantity"], "order filled_quantity"),
        average_fill_price=_money(data["average_fill_price"], "average_fill_price"),
        order_id=data.get("order_id"),
        rejection_reason=data.get("rejection_reason"),
    )


def _fill_to_dict(fill: Fill) -> dict[str, Any]:
    return {
        "order_id": fill.order_id,
        "instrument": _instrument_to_dict(fill.instrument),
        "side": fill.side.value,
        "quantity": fill.quantity,
        "price": str(fill.price),
        "commission": str(fill.commission),
        "filled_at": fill.filled_at.isoformat(),
    }


def _fill_from_dict(data: dict[str, Any]) -> Fill:
    return Fill(
        order_id=data["order_id"],
        instrument=_instrument_from_dict(data["instrument"]),
        side=OrderSide(data["side"]),
        quantity=_int(data["quantity"], "fill quantity"),
        price=_money(data["price"], "fill price"),
        commission=_money(data["commission"], "fill commission"),
        filled_at=datetime.fromisoformat(data["filled_at"]),
    )


# --------------------------------------------------------------------------- #
# Scalar coercion (all raise ValueError, matching the model layer)
# --------------------------------------------------------------------------- #


def _money(value: Any, name: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a decimal number") from exc
    if not result.is_finite():
        raise ValueError(f"{name} must be finite")
    return result


def _int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    return value