"""Query/filter helpers for the experience store (WS 7.9).

These are deliberately simple, dependency-free filters over the in-memory record
index. They support the future consumers (WS 7.10 outcome analysis, WS 7.11
champion/challenger comparison, WS 7.13 feedback loop) and regime-aware slices
without coupling them to any storage implementation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Mapping, Sequence

from fno_ai_paper_trading.experience.enums import (
    DataQualityStatus,
    DecisionStatus,
    ExperienceSourceType,
    OutcomeKind,
)
from fno_ai_paper_trading.experience.records import ExperienceRecord
from fno_ai_paper_trading.models.enums import Signal


@dataclass(frozen=True)
class ExperienceQuery:
    """All optional; a record matches when every set filter matches."""

    instrument_symbol: str | None = None
    timeframe: str | None = None
    regime_label: str | None = None
    strategy_name: str | None = None
    strategy_version: str | None = None
    decision_signal: Signal | None = None
    decision_status: DecisionStatus | None = None
    data_quality: DataQualityStatus | None = None
    outcome: OutcomeKind | None = None
    source: ExperienceSourceType | None = None
    source_detail: str | None = None
    start_time: datetime | None = None
    end_time: datetime | None = None
    has_trade: bool | None = None
    has_advisory: bool | None = None
    record_status: str | None = None
    experience_ids: Sequence[str] = field(default_factory=tuple)


def apply_query(records: Sequence[ExperienceRecord], query: ExperienceQuery) -> list[ExperienceRecord]:
    """Return the records matching ``query``, sorted by decision timestamp."""
    matched: list[ExperienceRecord] = []
    for record in records:
        if not _matches(record, query):
            continue
        matched.append(record)
    matched.sort(key=lambda record: record.decision.decision_timestamp)
    return matched


def _matches(record: ExperienceRecord, q: ExperienceQuery) -> bool:
    decision = record.decision
    if q.experience_ids and record.experience_id not in set(q.experience_ids):
        return False
    if q.instrument_symbol and decision.instrument.symbol != q.instrument_symbol:
        return False
    if q.timeframe and decision.timeframe != q.timeframe:
        return False
    if q.regime_label and decision.regime_label != q.regime_label:
        return False
    if q.strategy_name and decision.strategy_name != q.strategy_name:
        return False
    if q.strategy_version and decision.strategy_version != q.strategy_version:
        return False
    if q.decision_signal and decision.signal is not q.decision_signal:
        return False
    if q.decision_status and decision.decision_status is not q.decision_status:
        return False
    if q.data_quality and decision.data_quality is not q.data_quality:
        return False
    if q.outcome is not None and (record.outcome is None or record.outcome.outcome is not q.outcome):
        return False
    if q.source and record.source is not q.source:
        return False
    if q.source_detail and record.source_detail != q.source_detail:
        return False
    if q.record_status and record.status != q.record_status:
        return False
    at = decision.decision_timestamp
    if q.start_time and at < q.start_time:
        return False
    if q.end_time and at > q.end_time:
        return False
    if q.has_trade is not None and (record.outcome is not None) != q.has_trade:
        return False
    if q.has_advisory is not None and (record.advisory is not None) != q.has_advisory:
        return False
    return True


_GROUP_KEYS: Mapping[str, Callable[[ExperienceRecord], str]] = {
    "outcome": lambda r: r.outcome.outcome.value if r.outcome is not None else "NO_TRADE",
    "regime_label": lambda r: r.decision.regime_label or "UNKNOWN",
    "strategy_name": lambda r: r.decision.strategy_name,
    "strategy_version": lambda r: r.decision.strategy_version,
    "decision_status": lambda r: r.decision.decision_status.value,
    "source": lambda r: r.source.value,
    "record_status": lambda r: r.status,
}


def count_by(records: Sequence[ExperienceRecord], key: str) -> dict[str, int]:
    """Group records by ``key`` and return per-group counts.

    Supported keys: ``outcome``, ``regime_label``, ``strategy_name``,
    ``strategy_version``, ``decision_status``, ``source``, ``record_status``.
    """
    if key not in _GROUP_KEYS:
        raise ValueError(f"unsupported grouping key {key!r}")
    extract = _GROUP_KEYS[key]
    grouped: dict[str, int] = {}
    for record in records:
        label = extract(record)
        grouped[label] = grouped.get(label, 0) + 1
    return dict(sorted(grouped.items()))