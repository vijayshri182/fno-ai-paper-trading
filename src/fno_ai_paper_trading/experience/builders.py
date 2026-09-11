"""Builders that turn existing domain objects into experience evidence (WS 7.9).

These are pure constructors — they create frozen records; they never submit an
order, never touch a portfolio, and never trigger execution. The AI advisory
builder hard-wires ``advisory_only=True`` and simply attaches the recommendation
produced elsewhere.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from types import MappingProxyType
from typing import Mapping

from fno_ai_paper_trading.ai.decision import AIDecision
from fno_ai_paper_trading.experience.enums import (
    AdvisoryUsage,
    DataQualityStatus,
    DecisionStatus,
    ExperienceSourceType,
    OutcomeKind,
)
from fno_ai_paper_trading.experience.records import (
    EXPERIENCE_SCHEMA_VERSION,
    AdvisoryEvidence,
    DecisionContext,
    ExperienceRecord,
    TradeOutcome,
    make_experience_id,
)
from fno_ai_paper_trading.models.enums import OrderSide, Signal
from fno_ai_paper_trading.models.instruments import Instrument
from fno_ai_paper_trading.models.position import Trade
from fno_ai_paper_trading.utils.functions import to_decimal


def build_decision(
    *,
    instrument: Instrument,
    decision_timestamp: datetime,
    timeframe: str,
    signal: Signal,
    confidence: Decimal | str | int,
    strategy_name: str,
    strategy_version: str,
    data_reference: str,
    features: Mapping[str, object] | None = None,
    feature_version: str = "1.0",
    decision_status: DecisionStatus = DecisionStatus.EXECUTED,
    data_quality: DataQualityStatus = DataQualityStatus.VALIDATED,
    regime_label: str | None = None,
    metadata: Mapping[str, str] | None = None,
) -> DecisionContext:
    """Construct a validated decision-time context."""
    return DecisionContext(
        decision_timestamp=decision_timestamp,
        instrument=instrument,
        timeframe=timeframe,
        signal=signal,
        confidence=to_decimal(confidence),
        strategy_name=strategy_name,
        strategy_version=strategy_version,
        data_reference=data_reference,
        features=_coerce_features(features or {}),
        feature_version=feature_version,
        decision_status=decision_status,
        data_quality=data_quality,
        regime_label=regime_label,
        metadata=MappingProxyType(dict(metadata or {})),
    )


def build_outcome(
    *,
    trade_id: str,
    side: OrderSide,
    entry_price: Decimal | str | int,
    entry_timestamp: datetime,
    exit_price: Decimal | str | int,
    exit_timestamp: datetime,
    quantity: int,
    realized_pnl: Decimal | str | int,
    total_costs: Decimal | str | int,
    stop_loss_price: Decimal | str | int | None = None,
    outcome: OutcomeKind | None = None,
) -> TradeOutcome:
    """Construct a completed trade outcome (all values become Decimal)."""
    return TradeOutcome(
        trade_id=trade_id,
        side=side,
        entry_price=to_decimal(entry_price),
        entry_timestamp=entry_timestamp,
        exit_price=to_decimal(exit_price),
        exit_timestamp=exit_timestamp,
        quantity=quantity,
        realized_pnl=to_decimal(realized_pnl),
        total_costs=to_decimal(total_costs),
        stop_loss_price=to_decimal(stop_loss_price) if stop_loss_price is not None else None,
        outcome=outcome,
    )


def build_outcome_from_round_trip(
    entry: Trade,
    exit_: Trade,
    *,
    realized_pnl: Decimal | None = None,
    stop_loss_price: Decimal | str | int | None = None,
) -> TradeOutcome:
    """Build a completed trade outcome from the portfolio's entry/exit trades.

    ``realized_pnl`` defaults to the ``realized_pnl`` recorded on the closing
    trade (the value the deterministic accounting path computed). When the two
    trades belong to different instruments the caller has a wiring bug — this is
    checked explicitly.
    """
    if entry.instrument.symbol != exit_.instrument.symbol:
        raise ValueError("entry and exit trades must reference the same instrument")
    net_pnl = realized_pnl if realized_pnl is not None else exit_.realized_pnl
    return TradeOutcome(
        trade_id=exit_.trade_id,
        side=entry.side,
        entry_price=entry.price,
        entry_timestamp=entry.executed_at,
        exit_price=exit_.price,
        exit_timestamp=exit_.executed_at,
        quantity=entry.quantity,
        realized_pnl=to_decimal(net_pnl),
        total_costs=entry.commission + exit_.commission,
        stop_loss_price=to_decimal(stop_loss_price) if stop_loss_price is not None else None,
    )


def build_advisory(
    ai_decision: AIDecision | None,
    usage: AdvisoryUsage = AdvisoryUsage.NONE,
) -> AdvisoryEvidence:
    """Attach AI/advisory evidence. Never executable; advisory_only is enforced.

    When ``ai_decision`` is ``None`` a ``present=False``, ``NONE`` usage evidence
    is produced so the record still shows "no advisory input" explicitly.
    """
    if ai_decision is None:
        return AdvisoryEvidence(present=False, usage=usage)
    return AdvisoryEvidence(
        present=True,
        usage=usage,
        action=ai_decision.action,
        confidence=ai_decision.confidence,
        rationale=ai_decision.rationale,
        model_name=ai_decision.model_name,
        model_version=ai_decision.model_version,
        advisory_only=True,
    )


def build_record(
    *,
    decision: DecisionContext,
    source: ExperienceSourceType,
    recorded_at: datetime,
    occurrence: int = 0,
    outcome: TradeOutcome | None = None,
    advisory: AdvisoryEvidence | None = None,
    source_detail: str | None = None,
    schema_version: str = EXPERIENCE_SCHEMA_VERSION,
) -> ExperienceRecord:
    """Assemble a durable experience record with a deterministic ID."""
    return ExperienceRecord(
        experience_id=make_experience_id(decision.identity, occurrence),
        decision=decision,
        outcome=outcome,
        advisory=advisory,
        source=source,
        source_detail=source_detail,
        recorded_at=recorded_at,
        schema_version=schema_version,
    )


def _coerce_features(features: Mapping[str, object]) -> Mapping[str, object]:
    """Feature values must serialize deterministically (frozen mapping)."""
    copied: dict[str, object] = {}
    for key, value in features.items():
        if isinstance(value, bool) or not isinstance(value, (Decimal, int, str)):
            raise TypeError(f"feature {key!r} must be a Decimal, int, or str")
        copied[key] = value
    return MappingProxyType(copied)