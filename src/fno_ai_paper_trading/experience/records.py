"""Durable experience-store domain records (WS 7.9).

These frozen records are the *evidence contract* for the adaptive-learning loop:

* :class:`DecisionContext` — exactly the information available at decision time
  (no outcome fields, no future data, by construction — see the
  ``decision_payload()`` guarantee).
* :class:`TradeOutcome` — the realized paper-trade result, written only after
  the position is closed.
* :class:`AdvisoryEvidence` — the AI/advisory recommendation and whether the
  deterministic path accepted it. ``advisory_only`` is hard-wired to ``True``;
  this module has no path to submit an order.

IDs are deterministic and stable: the same decision identity + occurrence always
produce the same ``experience_id``, which makes appends idempotent.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from types import MappingProxyType
from typing import Mapping

from fno_ai_paper_trading.ai.decision import FeatureValue
from fno_ai_paper_trading.experience.classification import classify_outcome, holding_seconds
from fno_ai_paper_trading.experience.enums import (
    AdvisoryUsage,
    DataQualityStatus,
    DecisionStatus,
    ExperienceSourceType,
    OutcomeKind,
)
from fno_ai_paper_trading.models.enums import OrderSide, Signal
from fno_ai_paper_trading.models.instruments import Instrument
from fno_ai_paper_trading.utils.functions import (
    non_negative_decimal,
    non_negative_int,
    positive_decimal,
    positive_int,
)

EXPERIENCE_SCHEMA_VERSION = "1"

_ID_PREFIX = "exp_"
_ID_LENGTH = 16

#: Values permitted in a decision-time feature snapshot (mirrors the AI boundary).
FeatureValueType = FeatureValue


def _frozen_features(features: Mapping[str, FeatureValue]) -> Mapping[str, FeatureValue]:
    """Validate and freeze a decision-time feature snapshot."""
    copied: dict[str, FeatureValue] = {}
    for name, value in features.items():
        if not isinstance(name, str) or not name.strip():
            raise ValueError("feature names must be non-empty strings")
        if isinstance(value, Decimal):
            if not value.is_finite():
                raise ValueError(f"feature {name!r} must be finite")
        elif isinstance(value, bool) or not isinstance(value, (int, str)):
            raise TypeError(f"feature {name!r} must be a Decimal, int, or str")
        copied[name] = value
    return MappingProxyType(copied)


def _frozen_strings(values: Mapping[str, str]) -> Mapping[str, str]:
    copied: dict[str, str] = {}
    for name, value in values.items():
        if not isinstance(name, str) or not name.strip():
            raise ValueError("metadata names must be non-empty strings")
        if not isinstance(value, str):
            raise TypeError(f"metadata {name!r} must be a string")
        copied[name] = value
    return MappingProxyType(copied)


def _required_text(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _required_datetime(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime")
    return value


def make_experience_id(identity: str, occurrence: int = 0) -> str:
    """Deterministic, stable experience ID from a decision identity.

    The same ``identity`` and ``occurrence`` always produce the same ID, so
    re-appending identical evidence is detected and ignored by the store.
    """
    non_negative_int(occurrence, "occurrence")
    digest = hashlib.sha256(f"{identity}|{occurrence}".encode("utf-8")).hexdigest()
    return f"{_ID_PREFIX}{digest[:_ID_LENGTH]}"


def decision_identity(decision: DecisionContext) -> str:
    """Deterministic identity string for a decision-time context.

    Derived from the full decision payload (timestamp, instrument, signal,
    confidence, strategy, features, regime, data reference, data quality) via a
    SHA-256 digest. It *excludes* outcome fields by construction, so a record
    can be upgraded from ``pending_outcome`` to ``complete`` without changing
    its identity.
    """
    payload = json.dumps(decision.decision_payload(), sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class DecisionContext:
    """The validated evidence available *at decision time* only.

    Deliberately contains no outcome fields, no post-decision bars, and no
    positions — these are attached later as :class:`TradeOutcome`. This is the
    no-look-ahead boundary: anything the future learning loop reasons about was
    genuinely available when the decision was made.
    """

    decision_timestamp: datetime
    instrument: Instrument
    timeframe: str
    signal: Signal
    confidence: Decimal
    strategy_name: str
    strategy_version: str
    data_reference: str
    features: Mapping[str, FeatureValue]
    feature_version: str
    decision_status: DecisionStatus
    data_quality: DataQualityStatus
    regime_label: str | None = None
    metadata: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "decision_timestamp", _required_datetime(self.decision_timestamp, "decision_timestamp"))
        if not isinstance(self.instrument, Instrument):
            raise TypeError("instrument must be an Instrument")
        object.__setattr__(self, "timeframe", _required_text(self.timeframe, "timeframe"))
        if not isinstance(self.signal, Signal):
            raise TypeError("signal must be a Signal")
        confidence = _bound_confidence(self.confidence)
        object.__setattr__(self, "confidence", confidence)
        object.__setattr__(self, "strategy_name", _required_text(self.strategy_name, "strategy_name"))
        object.__setattr__(self, "strategy_version", _required_text(self.strategy_version, "strategy_version"))
        object.__setattr__(self, "data_reference", _required_text(self.data_reference, "data_reference"))
        object.__setattr__(self, "features", _frozen_features(self.features))
        object.__setattr__(self, "feature_version", _required_text(self.feature_version, "feature_version"))
        if not isinstance(self.decision_status, DecisionStatus):
            raise TypeError("decision_status must be a DecisionStatus")
        if not isinstance(self.data_quality, DataQualityStatus):
            raise TypeError("data_quality must be a DataQualityStatus")
        if self.regime_label is not None:
            object.__setattr__(self, "regime_label", _required_text(self.regime_label, "regime_label"))
        object.__setattr__(self, "metadata", _frozen_strings(self.metadata))

    @property
    def identity(self) -> str:
        return decision_identity(self)

    def decision_payload(self) -> dict[str, object]:
        """Decision-time-only serialization — never contains trade/outcome data."""
        return {
            "decision_timestamp": self.decision_timestamp.isoformat(),
            "instrument": self.instrument.symbol,
            "timeframe": self.timeframe,
            "signal": self.signal.value,
            "confidence": str(self.confidence),
            "strategy_name": self.strategy_name,
            "strategy_version": self.strategy_version,
            "data_reference": self.data_reference,
            "feature_version": self.feature_version,
            "decision_status": self.decision_status.value,
            "data_quality": self.data_quality.value,
            "regime_label": self.regime_label,
            "features": {key: str(value) for key, value in self.features.items()},
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class TradeOutcome:
    """Realized paper-trade evidence, written after the position is closed."""

    trade_id: str
    side: OrderSide
    entry_price: Decimal
    entry_timestamp: datetime
    exit_price: Decimal
    exit_timestamp: datetime
    quantity: int
    realized_pnl: Decimal
    total_costs: Decimal
    outcome: OutcomeKind | None = None
    stop_loss_price: Decimal | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "trade_id", _required_text(self.trade_id, "trade_id"))
        if not isinstance(self.side, OrderSide):
            raise TypeError("side must be an OrderSide")
        object.__setattr__(self, "entry_price", positive_decimal(self.entry_price, "entry_price"))
        object.__setattr__(self, "entry_timestamp", _required_datetime(self.entry_timestamp, "entry_timestamp"))
        object.__setattr__(self, "exit_price", positive_decimal(self.exit_price, "exit_price"))
        object.__setattr__(self, "exit_timestamp", _required_datetime(self.exit_timestamp, "exit_timestamp"))
        object.__setattr__(self, "quantity", positive_int(self.quantity, "quantity"))
        realized = self.realized_pnl
        if not isinstance(realized, Decimal) or not realized.is_finite():
            raise ValueError("realized_pnl must be a finite Decimal")
        object.__setattr__(self, "realized_pnl", realized)
        object.__setattr__(self, "total_costs", non_negative_decimal(self.total_costs, "total_costs"))
        computed = self.outcome if self.outcome is not None else classify_outcome(realized)
        if not isinstance(computed, OutcomeKind):
            raise TypeError("outcome must be an OutcomeKind")
        if computed in (OutcomeKind.PENDING, OutcomeKind.FLAT):
            raise ValueError("a completed trade outcome cannot be PENDING or FLAT")
        object.__setattr__(self, "outcome", computed)
        if self.stop_loss_price is not None:
            object.__setattr__(self, "stop_loss_price", positive_decimal(self.stop_loss_price, "stop_loss_price"))
        if not (self.entry_timestamp.tzinfo is None) == (self.exit_timestamp.tzinfo is None):
            raise ValueError("entry and exit timestamps must both be aware or both be naive")
        if self.exit_timestamp < self.entry_timestamp:
            raise ValueError("exit timestamp must not be before entry timestamp")

    @property
    def holding_seconds(self) -> int:
        return holding_seconds(self.entry_timestamp, self.exit_timestamp)


@dataclass(frozen=True)
class AdvisoryEvidence:
    """AI/advisory recommendation evidence. Advisory and non-executable only."""

    present: bool
    usage: AdvisoryUsage
    action: Signal | None = None
    confidence: Decimal | None = None
    rationale: str | None = None
    model_name: str | None = None
    model_version: str | None = None
    advisory_only: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.present, bool):
            raise TypeError("present must be a bool")
        if not isinstance(self.usage, AdvisoryUsage):
            raise TypeError("usage must be an AdvisoryUsage")
        if not self.advisory_only:
            raise ValueError("advisory evidence must always be advisory_only=True")
        if self.action is not None and not isinstance(self.action, Signal):
            raise TypeError("action must be a Signal when set")
        if self.confidence is not None:
            object.__setattr__(self, "confidence", _bound_confidence(self.confidence))
        for name, value in (
            ("rationale", self.rationale),
            ("model_name", self.model_name),
            ("model_version", self.model_version),
        ):
            if value is not None:
                object.__setattr__(self, name, _required_text(value, name))


@dataclass(frozen=True)
class ExperienceRecord:
    """A durable, immutable experience record (decision + outcome + advisory).

    ``status`` is derived, not arbitrary:
    * ``complete`` — a full round-trip outcome is attached.
    * ``pending_outcome`` — the decision was executed but the outcome is not yet
      known (e.g. position still open at snapshot time).
    * ``no_trade`` — no trade resulted (HOLD/rejected/skipped decision).

    Records are append-only: a corrected outcome is a *new* record, never an
    in-place mutation.
    """

    experience_id: str
    decision: DecisionContext
    source: ExperienceSourceType
    recorded_at: datetime
    outcome: TradeOutcome | None = None
    advisory: AdvisoryEvidence | None = None
    source_detail: str | None = None
    schema_version: str = EXPERIENCE_SCHEMA_VERSION
    status: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "experience_id", _required_text(self.experience_id, "experience_id"))
        if not isinstance(self.decision, DecisionContext):
            raise TypeError("decision must be a DecisionContext")
        if self.outcome is not None and not isinstance(self.outcome, TradeOutcome):
            raise TypeError("outcome must be a TradeOutcome when set")
        if self.advisory is not None and not isinstance(self.advisory, AdvisoryEvidence):
            raise TypeError("advisory must be an AdvisoryEvidence when set")
        if not isinstance(self.source, ExperienceSourceType):
            raise TypeError("source must be an ExperienceSourceType")
        object.__setattr__(self, "recorded_at", _required_datetime(self.recorded_at, "recorded_at"))
        if self.source_detail is not None:
            object.__setattr__(self, "source_detail", _required_text(self.source_detail, "source_detail"))
        if self.schema_version != EXPERIENCE_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported experience schema version {self.schema_version!r}; "
                f"expected {EXPERIENCE_SCHEMA_VERSION!r}"
            )
        if self.outcome is not None:
            derived = "complete"
        elif self.decision.decision_status is DecisionStatus.EXECUTED:
            derived = "pending_outcome"
        else:
            derived = "no_trade"
        object.__setattr__(self, "status", derived)

    def with_outcome(self, outcome: TradeOutcome, *, recorded_at: datetime | None = None) -> "ExperienceRecord":
        """A *new* immutable record with the outcome attached (original unchanged)."""
        return ExperienceRecord(
            experience_id=self.experience_id,
            decision=self.decision,
            outcome=outcome,
            advisory=self.advisory,
            source=self.source,
            source_detail=self.source_detail,
            recorded_at=recorded_at or self.recorded_at,
            schema_version=self.schema_version,
        )


def _bound_confidence(value: Decimal) -> Decimal:
    if not isinstance(value, Decimal):
        try:
            value = Decimal(str(value))
        except Exception as exc:  # noqa: BLE001 - normalized into a ValueError
            raise TypeError("confidence must be a Decimal") from exc
    if not value.is_finite() or not Decimal("0") <= value <= Decimal("1"):
        raise ValueError("confidence must be finite and between 0 and 1")
    return value