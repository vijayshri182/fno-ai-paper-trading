"""Durable JSONL-backed persistence for experience records (WS 7.9).

The store is append-only and idempotent:

* every record is written as one canonical JSON line in ``experiences.jsonl``;
* submitting a record whose ``experience_id`` already exists is a no-op
  (``duplicate=True``), which makes replay/merge safe to run repeatedly;
* a ``meta.json`` manifest records the schema version, name and created time —
  loading a store with an unsupported schema version (or a corrupted log line)
  raises rather than silently discarding evidence;
* records are immutable; a corrected outcome is a *new* record, never an edit.

The domain layer (:mod:`fno_ai_paper_trading.experience`) never depends on this
module, so the records can be stored by any future backend without changing the
evidence contract.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Sequence

from fno_ai_paper_trading.experience.enums import (
    AdvisoryUsage,
    DataQualityStatus,
    DecisionStatus,
    ExperienceSourceType,
    OutcomeKind,
)
from fno_ai_paper_trading.experience.queries import ExperienceQuery, apply_query
from fno_ai_paper_trading.experience.records import (
    EXPERIENCE_SCHEMA_VERSION,
    AdvisoryEvidence,
    DecisionContext,
    ExperienceRecord,
    TradeOutcome,
)
from fno_ai_paper_trading.models.enums import OrderSide, Signal
from fno_ai_paper_trading.models.instruments import Instrument

#: Default directory for experience stores (git-ignored alongside paper_state).
DEFAULT_EXPERIENCE_DIR = "experience_store"

_LOG_SUFFIX = ".jsonl"
_META_SUFFIX = ".meta.json"


def _safe_name(value: str) -> str:
    unsafe = frozenset('\\/:*?"<>|')
    return "".join("_" if ch in unsafe or ch.isspace() else ch for ch in value)


@dataclass(frozen=True)
class AppendResult:
    """Outcome of a single append."""

    record: ExperienceRecord
    duplicate: bool = False


@dataclass(frozen=True)
class MergeResult:
    """Outcome of a bulk merge."""

    appended: int
    duplicates: int

    @property
    def total(self) -> int:
        return self.appended + self.duplicates


class ExperienceStore:
    """Append-only, idempotent, file-backed experience-record store.

    ``directory=None`` keeps the store in memory only (used by offline tests and
    replay pipelines that persist elsewhere). With a directory, every append
    immediately rewrites the JSONL log so no explicit checkpoint is needed.
    """

    def __init__(
        self,
        directory: str | Path | None = None,
        name: str = "default",
    ) -> None:
        self.name = _safe_name(str(name)) or "default"
        self._records: dict[str, ExperienceRecord] = {}
        self._created_at: str = _now_iso()
        self._directory: Path | None = None
        if directory is not None:
            self.load(directory, name=self.name)

    # ------------------------------------------------------------------ #
    # Write path
    # ------------------------------------------------------------------ #

    def append(self, record: ExperienceRecord) -> AppendResult:
        """Add ``record`` unless its ``experience_id`` already exists.

        The single legal in-place transition is ``pending_outcome`` →
        ``complete`` (the outcome for an already-recorded executed decision was
        finalized). Everything else is idempotent: re-appending an identical
        record, or any update to an already-``complete`` record, is a no-op
        reported via ``duplicate=True``.
        """
        if not isinstance(record, ExperienceRecord):
            raise TypeError("experience store only accepts ExperienceRecord")
        existing = self._records.get(record.experience_id)
        if existing is None:
            self._records[record.experience_id] = record
            self._flush()
            return AppendResult(record=record, duplicate=False)
        if existing.status == "pending_outcome" and record.status == "complete":
            self._records[record.experience_id] = record
            self._flush()
            return AppendResult(record=record, duplicate=False)
        return AppendResult(record=existing, duplicate=True)

    def merge(self, records: Sequence[ExperienceRecord]) -> MergeResult:
        """Add many records idempotently (re-running a replay is safe)."""
        appended = 0
        duplicates = 0
        for record in records:
            result = self.append(record)
            if result.duplicate:
                duplicates += 1
            else:
                appended += 1
        return MergeResult(appended=appended, duplicates=duplicates)

    # ------------------------------------------------------------------ #
    # Read path
    # ------------------------------------------------------------------ #

    def get(self, experience_id: str) -> ExperienceRecord | None:
        return self._records.get(experience_id)

    def all(self) -> list[ExperienceRecord]:
        """All records in insertion order (stable)."""
        return list(self._records.values())

    def query(self, query: ExperienceQuery) -> list[ExperienceRecord]:
        return apply_query(self.all(), query)

    def find(
        self,
        *,
        instrument_symbol: str | None = None,
        regime_label: str | None = None,
        strategy_name: str | None = None,
        strategy_version: str | None = None,
        outcome: OutcomeKind | None = None,
        source: ExperienceSourceType | None = None,
        decision_signal: Signal | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        has_trade: bool | None = None,
    ) -> list[ExperienceRecord]:
        """Convenience keyword filter over the record index."""
        return self.query(
            ExperienceQuery(
                instrument_symbol=instrument_symbol,
                regime_label=regime_label,
                strategy_name=strategy_name,
                strategy_version=strategy_version,
                outcome=outcome,
                source=source,
                decision_signal=decision_signal,
                start_time=start_time,
                end_time=end_time,
                has_trade=has_trade,
            )
        )

    @property
    def ids(self) -> list[str]:
        return list(self._records.keys())

    @property
    def count(self) -> int:
        return len(self._records)

    # ------------------------------------------------------------------ #
    # Persistence
    # ------------------------------------------------------------------ #

    def load(self, directory: str | Path = DEFAULT_EXPERIENCE_DIR, name: str = "default") -> None:
        """Load a store from ``directory`` (creating an empty store if absent)."""
        directory = Path(directory)
        safe_name = _safe_name(str(name)) or "default"
        log_path = directory / f"{safe_name}{_LOG_SUFFIX}"
        meta_path = directory / f"{safe_name}{_META_SUFFIX}"

        records: dict[str, ExperienceRecord] = {}
        if log_path.is_file():
            if not meta_path.is_file():
                raise ValueError(
                    f"experience store metadata {meta_path} is missing; "
                    "refusing to load an unattached log"
                )
            with meta_path.open("r", encoding="utf-8") as handle:
                metadata = json.load(handle)
            if metadata.get("schema_version") != EXPERIENCE_SCHEMA_VERSION:
                raise ValueError(
                    f"unsupported experience store schema version "
                    f"{metadata.get('schema_version')!r}; expected "
                    f"{EXPERIENCE_SCHEMA_VERSION!r}"
                )
            records = self._read_log(log_path)

        self.name = safe_name
        self._records = records
        self._created_at = _file_meta_created_at(meta_path)
        self._directory = directory

    def save(self) -> None:
        """Explicit checkpoint (appends already auto-save)."""
        if self._directory is None:
            raise ValueError("cannot save an in-memory store (no directory)")
        self._flush()

    @property
    def directory(self) -> Path | None:
        return self._directory

    def _flush(self) -> None:
        if self._directory is None:
            return
        self._directory.mkdir(parents=True, exist_ok=True)
        log_path = self._directory / f"{self.name}{_LOG_SUFFIX}"
        meta_path = self._directory / f"{self.name}{_META_SUFFIX}"
        with log_path.open("w", encoding="utf-8") as handle:
            for record in self._records.values():
                handle.write(
                    json.dumps(experience_to_dict(record), sort_keys=True) + "\n"
                )
        metadata = {
            "schema_version": EXPERIENCE_SCHEMA_VERSION,
            "name": self.name,
            "count": self.count,
            "created_at": self._created_at,
            "saved_at": _now_iso(),
        }
        meta_path.write_text(
            json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

    @staticmethod
    def _read_log(log_path: Path) -> dict[str, ExperienceRecord]:
        records: dict[str, ExperienceRecord] = {}
        with log_path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    payload = json.loads(line)
                    record = experience_from_dict(payload)
                except (ValueError, TypeError, KeyError) as exc:
                    raise ValueError(
                        f"corrupt experience log at {log_path} line {line_number}: {exc}"
                    ) from exc
                existing = records.get(record.experience_id)
                if existing is not None:
                    raise ValueError(
                        f"duplicate experience_id {record.experience_id!r} in log "
                        f"{log_path} (line {line_number}) — evidence log is not append-only"
                    )
                records[record.experience_id] = record
        return records


def _file_meta_created_at(meta_path: Path) -> str:
    if meta_path.is_file():
        try:
            with meta_path.open("r", encoding="utf-8") as handle:
                metadata = json.load(handle)
            return str(metadata.get("created_at", _now_iso()))
        except (ValueError, OSError):
            return _now_iso()
    return _now_iso()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds") + "Z"


# --------------------------------------------------------------------------- #
# Deterministic serialization
# --------------------------------------------------------------------------- #


def experience_to_dict(record: ExperienceRecord) -> dict[str, Any]:
    """Canonical, JSON-serializable form of a record (money/time as strings)."""
    decision = record.decision
    outcome = record.outcome
    advisory = record.advisory
    return {
        "schema_version": EXPERIENCE_SCHEMA_VERSION,
        "experience_id": record.experience_id,
        "status": record.status,
        "recorded_at": record.recorded_at.isoformat(),
        "source": record.source.value,
        "source_detail": record.source_detail,
        "decision": {
            "decision_timestamp": decision.decision_timestamp.isoformat(),
            "instrument": _instrument_to_dict(decision.instrument),
            "timeframe": decision.timeframe,
            "signal": decision.signal.value,
            "confidence": str(decision.confidence),
            "strategy_name": decision.strategy_name,
            "strategy_version": decision.strategy_version,
            "data_reference": decision.data_reference,
            "feature_version": decision.feature_version,
            "decision_status": decision.decision_status.value,
            "data_quality": decision.data_quality.value,
            "regime_label": decision.regime_label,
            "features": _features_to_dict(decision.features),
            "metadata": dict(decision.metadata),
        },
        "outcome": _outcome_to_dict(outcome) if outcome is not None else None,
        "advisory": _advisory_to_dict(advisory) if advisory is not None else None,
    }


def experience_from_dict(payload: dict[str, Any]) -> ExperienceRecord:
    """Rebuild a record from its canonical dict (raises on any mismatch)."""
    if payload.get("schema_version") != EXPERIENCE_SCHEMA_VERSION:
        raise ValueError(
            f"unsupported experience schema version {payload.get('schema_version')!r}"
        )
    required_id = str(payload["experience_id"])
    decision_raw = payload["decision"]

    features = _features_from_dict(decision_raw.get("features", []))
    decision = DecisionContext(
        decision_timestamp=_date(decision_raw["decision_timestamp"], "decision_timestamp"),
        instrument=_instrument_from_dict(decision_raw["instrument"]),
        timeframe=str(decision_raw["timeframe"]),
        signal=Signal(decision_raw["signal"]),
        confidence=_decimal(decision_raw["confidence"], "confidence"),
        strategy_name=str(decision_raw["strategy_name"]),
        strategy_version=str(decision_raw["strategy_version"]),
        data_reference=str(decision_raw["data_reference"]),
        features=features,
        feature_version=str(decision_raw["feature_version"]),
        decision_status=DecisionStatus(decision_raw["decision_status"]),
        data_quality=DataQualityStatus(decision_raw["data_quality"]),
        regime_label=decision_raw.get("regime_label"),
        metadata=dict(decision_raw.get("metadata", {})),
    )
    outcome_raw = payload.get("outcome")
    advisory_raw = payload.get("advisory")

    return ExperienceRecord(
        experience_id=required_id,
        decision=decision,
        outcome=_outcome_from_dict(outcome_raw) if outcome_raw is not None else None,
        advisory=_advisory_from_dict(advisory_raw) if advisory_raw is not None else None,
        source=ExperienceSourceType(payload["source"]),
        source_detail=payload.get("source_detail"),
        recorded_at=_date(payload["recorded_at"], "recorded_at"),
        schema_version=EXPERIENCE_SCHEMA_VERSION,
    )


# --------------------------------------------------------------------------- #
# Serialization sub-blocks (shared with the session store's model fields)
# --------------------------------------------------------------------------- #


def _outcome_to_dict(outcome: TradeOutcome) -> dict[str, Any]:
    return {
        "trade_id": outcome.trade_id,
        "side": outcome.side.value,
        "entry_price": str(outcome.entry_price),
        "entry_timestamp": outcome.entry_timestamp.isoformat(),
        "exit_price": str(outcome.exit_price),
        "exit_timestamp": outcome.exit_timestamp.isoformat(),
        "quantity": outcome.quantity,
        "realized_pnl": str(outcome.realized_pnl),
        "total_costs": str(outcome.total_costs),
        "stop_loss_price": str(outcome.stop_loss_price) if outcome.stop_loss_price is not None else None,
        "outcome": outcome.outcome.value,
    }


def _outcome_from_dict(data: dict[str, Any]) -> TradeOutcome:
    stop_loss = data.get("stop_loss_price")
    return TradeOutcome(
        trade_id=str(data["trade_id"]),
        side=OrderSide(data["side"]),
        entry_price=_decimal(data["entry_price"], "entry_price"),
        entry_timestamp=_date(data["entry_timestamp"], "entry_timestamp"),
        exit_price=_decimal(data["exit_price"], "exit_price"),
        exit_timestamp=_date(data["exit_timestamp"], "exit_timestamp"),
        quantity=_int(data["quantity"], "quantity"),
        realized_pnl=_decimal(data["realized_pnl"], "realized_pnl"),
        total_costs=_decimal(data["total_costs"], "total_costs"),
        stop_loss_price=_decimal(stop_loss, "stop_loss_price") if stop_loss is not None else None,
        outcome=OutcomeKind(data["outcome"]) if "outcome" in data else None,
    )


def _advisory_to_dict(advisory: AdvisoryEvidence) -> dict[str, Any]:
    return {
        "present": advisory.present,
        "usage": advisory.usage.value,
        "action": advisory.action.value if advisory.action is not None else None,
        "confidence": str(advisory.confidence) if advisory.confidence is not None else None,
        "rationale": advisory.rationale,
        "model_name": advisory.model_name,
        "model_version": advisory.model_version,
        "advisory_only": advisory.advisory_only,
    }


def _advisory_from_dict(data: dict[str, Any]) -> AdvisoryEvidence:
    action = data.get("action")
    confidence = data.get("confidence")
    return AdvisoryEvidence(
        present=bool(data["present"]),
        usage=AdvisoryUsage(data["usage"]),
        action=Signal(action) if action is not None else None,
        confidence=_decimal(confidence, "confidence") if confidence is not None else None,
        rationale=data.get("rationale"),
        model_name=data.get("model_name"),
        model_version=data.get("model_version"),
        advisory_only=bool(data.get("advisory_only", True)),
    )


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
    from fno_ai_paper_trading.models.enums import InstrumentType

    expiry = _date(data["expiry"], "expiry") if data.get("expiry") else None
    strike = data.get("strike")
    return Instrument(
        symbol=data["symbol"],
        instrument_type=InstrumentType(data["instrument_type"]),
        underlying_symbol=data["underlying_symbol"],
        expiry=expiry,
        strike=_decimal(strike, "strike") if strike is not None else None,
        option_type=data.get("option_type"),
        exchange=data.get("exchange", "NSE"),
        exchange_token=data.get("exchange_token"),
        lot_size=_int(data.get("lot_size", 1), "lot_size"),
        tick_size=_decimal(data.get("tick_size", "0.05"), "tick_size"),
        multiplier=_int(data.get("multiplier", 1), "multiplier"),
    )


def _features_to_dict(features: Any) -> list[Any]:
    """Type-tagged, key-sorted feature serialization (preserves Decimal/int/str)."""
    entries = []
    for key in sorted(features):
        value = features[key]
        if isinstance(value, Decimal):
            entries.append({"key": key, "type": "D", "value": str(value)})
        elif isinstance(value, bool) or not isinstance(value, (int, str)):
            raise TypeError(f"feature {key!r} must be a Decimal, int, or str")
        elif isinstance(value, int):
            entries.append({"key": key, "type": "I", "value": value})
        else:
            entries.append({"key": key, "type": "S", "value": value})
    return entries


def _features_from_dict(entries: Any) -> dict[str, Any]:
    features: dict[str, Any] = {}
    if not isinstance(entries, list):
        raise ValueError("features must be a list of {key,type,value} entries")
    for entry in entries:
        key = str(entry["key"])
        kind = str(entry["type"])
        value = entry["value"]
        if kind == "D":
            features[key] = _decimal(value, f"feature {key}")
        elif kind == "I":
            features[key] = _int(value, f"feature {key}")
        elif kind == "S":
            if not isinstance(value, str):
                raise ValueError(f"feature {key} must be a string")
            features[key] = value
        else:
            raise ValueError(f"unknown feature type {kind!r} for {key!r}")
    return features


def _decimal(value: Any, name: str) -> Any:
    from decimal import Decimal, InvalidOperation

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


def _date(value: Any, name: str) -> datetime:
    try:
        return datetime.fromisoformat(str(value))
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO datetime") from exc