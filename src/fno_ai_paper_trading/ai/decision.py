"""Immutable, deterministic-boundary data models for AI decision support."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from types import MappingProxyType
from typing import Mapping, TypeAlias

from fno_ai_paper_trading.models.enums import Signal
from fno_ai_paper_trading.models.instruments import Instrument

FeatureValue: TypeAlias = Decimal | int | str


def _frozen_features(features: Mapping[str, FeatureValue]) -> Mapping[str, FeatureValue]:
    """Validate and freeze feature values at the decision-support boundary."""
    copied: dict[str, FeatureValue] = {}
    for name, value in features.items():
        if not isinstance(name, str) or not name.strip():
            raise ValueError("feature names must be non-empty strings")
        if isinstance(value, Decimal):
            if not value.is_finite():
                raise ValueError(f"feature {name!r} must be finite")
        elif isinstance(value, bool) or not isinstance(value, (int, str)):
            raise TypeError(
                f"feature {name!r} must be a Decimal, int, or str; floats are not supported"
            )
        copied[name] = value
    return MappingProxyType(copied)


def _required_text(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


@dataclass(frozen=True)
class DecisionContext:
    """The validated data reference supplied to a decision-support implementation.

    It intentionally contains only normalized market context and provenance. A
    decision-support implementation receives no broker, portfolio, risk-manager,
    or execution capability.
    """

    instrument: Instrument
    timestamp: datetime
    data_reference: str
    features: Mapping[str, FeatureValue]

    def __post_init__(self) -> None:
        if not isinstance(self.instrument, Instrument):
            raise TypeError("instrument must be an Instrument")
        if not isinstance(self.timestamp, datetime):
            raise TypeError("timestamp must be a datetime")
        object.__setattr__(self, "data_reference", _required_text(self.data_reference, "data_reference"))
        object.__setattr__(self, "features", _frozen_features(self.features))


@dataclass(frozen=True)
class AIDecision:
    """A structured recommendation produced by an AI or deterministic model.

    ``action`` is advisory only. This data model has no path to submit an order;
    any future execution integration must independently invoke deterministic risk,
    sizing, stop-loss, and paper-broker controls.
    """

    action: Signal
    confidence: Decimal
    rationale: str
    features_considered: Mapping[str, FeatureValue]
    market_regime: str
    model_name: str
    model_version: str
    timestamp: datetime
    data_reference: str

    def __post_init__(self) -> None:
        if not isinstance(self.action, Signal):
            raise TypeError("action must be a Signal")
        if not isinstance(self.confidence, Decimal):
            raise TypeError("confidence must be a Decimal")
        if not self.confidence.is_finite() or not Decimal("0") <= self.confidence <= Decimal("1"):
            raise ValueError("confidence must be finite and between 0 and 1")
        if not isinstance(self.timestamp, datetime):
            raise TypeError("timestamp must be a datetime")
        object.__setattr__(self, "rationale", _required_text(self.rationale, "rationale"))
        object.__setattr__(self, "market_regime", _required_text(self.market_regime, "market_regime"))
        object.__setattr__(self, "model_name", _required_text(self.model_name, "model_name"))
        object.__setattr__(self, "model_version", _required_text(self.model_version, "model_version"))
        object.__setattr__(self, "data_reference", _required_text(self.data_reference, "data_reference"))
        object.__setattr__(self, "features_considered", _frozen_features(self.features_considered))
