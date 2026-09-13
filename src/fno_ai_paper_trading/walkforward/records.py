"""Machine-readable day-by-day algorithm evolution ledger (WS 7.18).

The ledger is the mandatory, append-only, chronological record of the
walk-forward engine: for EVERY historical trading day it preserves what
algorithm was used that morning (frozen before the day was evaluated), what
happened, what the system learned afterward, what modification it proposed, how
that modification fared in its future-only validation, whether it was promoted,
and exactly which algorithm was selected for the next trading day and why.

Rewritten history is impossible by construction: the ledger is appended one line
per day and is never edited. The JSONL artifact is the machine-readable daily
evolution ledger; the HTML/Markdown reports in :mod:`reports` render the
human-readable tables plus the algorithm version/change history and the
promotion/rejection history.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from typing import Any, Mapping

from fno_ai_paper_trading.walkforward.config import FRAMEWORK_VERSION

# Numeric map from the WS 7.18 requirement (1..21) to the serialized field name.
DAILY_EVOLUTION_FIELDS = (
    ("1", "day"),
    ("2", "algorithm_used"),
    ("3", "parent_algorithm"),
    ("4", "regime"),
    ("5", "signal"),
    ("6", "trades"),
    ("7", "pnl"),
    ("8", "transaction_costs"),
    ("9", "slippage"),
    ("10", "outcome"),
    ("11", "evidence"),
    ("12", "problem_identified"),
    ("13", "hypothesis_generated"),
    ("14", "challenger_generated"),
    ("15", "modification_proposed"),
    ("16", "evidence_supporting"),
    ("17", "validation_period_assigned"),
    ("18", "validation_status"),
    ("19", "promotion_decision"),
    ("20", "next_day_algorithm"),
    ("21", "why_next_day"),
)


def _day_datetime(day: date) -> str:
    return f"{day.isoformat()}T00:00:00"


def daily_field_label_order() -> list[dict[str, str]]:
    """The canonical 1..21 field order and labels (used by reports/dashboard)."""
    return [{"field": label, "label": label.replace("_", " ")} for _n, label in DAILY_EVOLUTION_FIELDS]


@dataclass(frozen=True)
class SignalOccurrence:
    """One champion signal during the day, mapped to F&O semantics."""

    timestamp: str
    side: str  # CALL | PUT | NO_TRADE

    def to_dict(self) -> dict[str, str]:
        return {"timestamp": self.timestamp, "side": self.side}


@dataclass(frozen=True)
class TradeDetail:
    """One completed round trip (opening fill -> closing fill)."""

    entry_time: str
    exit_time: str
    side: str  # CALL | PUT (direction of the opening fill)
    entry_price: str
    exit_price: str
    quantity: int
    realized_pnl: str
    commission: str
    costs: str
    entry_regime: str

    def to_dict(self) -> dict[str, object]:
        return {
            "entry_time": self.entry_time,
            "exit_time": self.exit_time,
            "side": self.side,
            "entry_price": self.entry_price,
            "exit_price": self.exit_price,
            "quantity": self.quantity,
            "realized_pnl": self.realized_pnl,
            "commission": self.commission,
            "costs": self.costs,
            "entry_regime": self.entry_regime,
        }


@dataclass(frozen=True)
class DailyEvolutionRecord:
    """One row of the day-by-day algorithm evolution ledger (21 fields)."""

    day: str
    algorithm_used: str
    parent_algorithm: str | None
    regime: str
    regime_features: Mapping[str, str]
    signal: str
    signal_occurrences: tuple[SignalOccurrence, ...]
    trades: tuple[TradeDetail, ...]
    pnl: str
    transaction_costs: str
    slippage: str
    outcome: str
    win_count: int
    loss_count: int
    open_position_count: int
    evidence: Mapping[str, object]
    problem_identified: Mapping[str, str]
    hypothesis_generated: list[Mapping[str, object]]
    challenger_generated: list[Mapping[str, object]]
    modification_proposed: list[Mapping[str, object]]
    evidence_supporting: Mapping[str, object]
    validation_period_assigned: list[Mapping[str, object]]
    validation_status: list[Mapping[str, object]]
    promotion_decision: list[Mapping[str, object]]
    next_day_algorithm: str
    why_next_day: str

    def to_dict(self) -> dict[str, object]:
        return {
            "1.day": self.day,
            "2.algorithm_used": self.algorithm_used,
            "3.parent_algorithm": self.parent_algorithm,
            "4.regime": self.regime,
            "4.regime_features": dict(self.regime_features),
            "5.signal": self.signal,
            "5.signal_occurrences": [o.to_dict() for o in self.signal_occurrences],
            "6.trades": [t.to_dict() for t in self.trades],
            "7.pnl": self.pnl,
            "8.transaction_costs": self.transaction_costs,
            "9.slippage": self.slippage,
            "10.outcome": self.outcome,
            "10.win_count": self.win_count,
            "10.loss_count": self.loss_count,
            "10.open_position_count": self.open_position_count,
            "11.evidence": dict(self.evidence),
            "12.problem_identified": dict(self.problem_identified),
            "13.hypothesis_generated": [dict(m) for m in self.hypothesis_generated],
            "14.challenger_generated": [dict(m) for m in self.challenger_generated],
            "15.modification_proposed": [dict(m) for m in self.modification_proposed],
            "16.evidence_supporting": dict(self.evidence_supporting),
            "17.validation_period_assigned": [dict(m) for m in self.validation_period_assigned],
            "18.validation_status": [dict(m) for m in self.validation_status],
            "19.promotion_decision": [dict(m) for m in self.promotion_decision],
            "20.next_day_algorithm": self.next_day_algorithm,
            "21.why_next_day": self.why_next_day,
        }

    @classmethod
    def required_field_keys(cls) -> frozenset[str]:
        """All serialized field names, used by tests that prove completeness."""
        extra = {
            "4.regime_features",
            "5.signal_occurrences",
            "10.win_count",
            "10.loss_count",
            "10.open_position_count",
        }
        return frozenset(f"{n}.{name}" for n, name in DAILY_EVOLUTION_FIELDS) | extra


class EvolutionLedger:
    """Append-only store of daily evolution records (JSONL or in-memory).

    ``path=None`` keeps the ledger fully in memory (used by tests and by
    consumes that persist artifacts themselves).
    """

    def __init__(self, path: object | None = None) -> None:
        self.path = None if path is None else path
        self._records: list[dict[str, object]] = []
        if self.path is not None:
            from pathlib import Path

            p = Path(self.path)
            if p.is_file():
                lines = p.read_text(encoding="utf-8").splitlines()
                for line in lines:
                    if line.strip():
                        self._records.append(json.loads(line))

    def append(self, record: DailyEvolutionRecord) -> None:
        payload = record.to_dict()
        self._records.append(payload)
        if self.path is not None:
            from pathlib import Path

            p = Path(self.path)
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, sort_keys=True) + "\n")

    @property
    def count(self) -> int:
        return len(self._records)

    def records(self) -> tuple[dict[str, object], ...]:
        return tuple(self._records)

    def read_records(self) -> tuple[dict[str, object], ...]:
        return self.records()


def make_baseline_version(*, day: date) -> dict[str, Any]:
    """Deterministic champion baseline entry (frozen MA(5,21))."""
    return {
        "version_id": "model_0",
        "strategy_name": "moving_average_cross",
        "strategy_params": {"fast": 5, "slow": 21},
        "feature_version": FRAMEWORK_VERSION,
        "description": "Frozen MA(5,21) reference champion (Phase 2 baseline)",
        "status": "ACTIVE",
        "promoted_at": _day_datetime(day),
        "evidence": {},
        "retired_at": "",
        "rollback_reason": "",
        "rollback_at": "",
    }


def make_promoted_version(
    *,
    version_id: str,
    day: date,
    strategy_name: str,
    strategy_params: Mapping[str, object],
    description: str,
    evidence: Mapping[str, object],
) -> dict[str, Any]:
    """Deterministic champion version minted when a challenger passes the gate."""
    return {
        "version_id": version_id,
        "strategy_name": strategy_name,
        "strategy_params": dict(strategy_params),
        "feature_version": FRAMEWORK_VERSION,
        "description": description,
        "status": "ACTIVE",
        "promoted_at": _day_datetime(day),
        "evidence": dict(evidence),
        "retired_at": "",
        "rollback_reason": "",
        "rollback_at": "",
    }


@dataclass(frozen=True)
class PromotionRecord:
    """One promotion/rejection decision, recorded for the promotion history."""

    challenger_id: str
    day: str
    decision: str  # PROMOTE | REJECT | INSUFFICIENT_EVIDENCE
    promoted_version: str | None
    reasons: tuple[str, ...]
    metrics: Mapping[str, object]

    def to_dict(self) -> dict[str, object]:
        return {
            "challenger_id": self.challenger_id,
            "day": self.day,
            "decision": self.decision,
            "promoted_version": self.promoted_version,
            "reasons": list(self.reasons),
            "metrics": dict(self.metrics),
        }