"""Research design and preregistration protocol (Phase 12).

A frozen :class:`ResearchProtocol` records the research question, hypothesis,
strategy specification status, candidate definition, features, entry/exit
definitions, contract-selection and gate rules, fill model and costs, evaluation
metrics, period splits, exclusions, minimum samples, stopping criteria and
known limitations. ``freeze`` pins a SHA-256 fingerprint that changes whenever
the protocol changes — a protocol must be frozen before any evaluation runs and
may not be altered after inspecting protected OOS results.

If no explicit, deterministic options strategy has been approved, the status is
``STRATEGY_SPECIFICATION_REQUIRED`` and the simulation harness is never
presented as a strategy.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Mapping

from fno_ai_paper_trading.paper_track.store import stable_dumps

PROTOCOL_SCHEMA_VERSION = "1"
STRATEGY_SPECIFICATION_REQUIRED = "STRATEGY_SPECIFICATION_REQUIRED"


@dataclass(frozen=True)
class ResearchProtocol:
    """The frozen research protocol (all free-text fields are prose notes)."""

    research_question: str
    hypothesis: str
    strategy_specification_status: str = STRATEGY_SPECIFICATION_REQUIRED
    candidate_strategy: str | None = None
    input_features: tuple[str, ...] = ()
    entry_event: str | None = None
    exit_event: str | None = None
    contract_selection_rules: str | None = None
    trade_quality_gates: str | None = None
    risk_gates: str | None = None
    fill_model: str | None = None
    fill_model_version: str | None = None
    transaction_costs: str | None = None
    evaluation_metrics: tuple[str, ...] = ()
    development_period: str | None = None
    validation_period: str | None = None
    protected_oos_period: str | None = None
    exclusion_rules: str | None = None
    minimum_sample_requirements: str | None = None
    stopping_criteria: str | None = None
    known_limitations: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "protocol_schema_version": PROTOCOL_SCHEMA_VERSION,
            "research_question": self.research_question,
            "hypothesis": self.hypothesis,
            "strategy_specification_status": self.strategy_specification_status,
            "candidate_strategy": self.candidate_strategy,
            "input_features": list(self.input_features),
            "entry_event": self.entry_event,
            "exit_event": self.exit_event,
            "contract_selection_rules": self.contract_selection_rules,
            "trade_quality_gates": self.trade_quality_gates,
            "risk_gates": self.risk_gates,
            "fill_model": self.fill_model,
            "fill_model_version": self.fill_model_version,
            "transaction_costs": self.transaction_costs,
            "evaluation_metrics": list(self.evaluation_metrics),
            "development_period": self.development_period,
            "validation_period": self.validation_period,
            "protected_oos_period": self.protected_oos_period,
            "exclusion_rules": self.exclusion_rules,
            "minimum_sample_requirements": self.minimum_sample_requirements,
            "stopping_criteria": self.stopping_criteria,
            "known_limitations": list(self.known_limitations),
        }


def protocol_fingerprint(protocol: ResearchProtocol) -> str:
    """SHA-256 over the stable-JSON form of the protocol."""
    return hashlib.sha256(stable_dumps(protocol.to_dict()).encode("utf-8")).hexdigest()


def freeze(protocol: ResearchProtocol) -> str:
    """Pin a protocol and return its fingerprint (freeze before evaluation)."""
    return protocol_fingerprint(protocol)


def default_protocol() -> ResearchProtocol:
    """The Phase 12 default protocol recording the current blocker honestly.

    No explicit deterministic options strategy has been approved and no
    historical options dataset exists, so the status is
    ``STRATEGY_SPECIFICATION_REQUIRED`` and the protected-OOS period is
    recorded as unavailable to evaluate.
    """
    return ResearchProtocol(
        research_question=(
            "Can a deterministic options pipeline (regime -> selection -> quality -> "
            "risk -> paper simulation) be evaluated leakage-safely on historical data?"
        ),
        hypothesis=(
            "Not testable without a validated historical options dataset; no edge is "
            "claimed and nothing is promoted."
        ),
        strategy_specification_status=STRATEGY_SPECIFICATION_REQUIRED,
        candidate_strategy=None,
        input_features=(
            "nifty_50_5m",
            "market_regime_report",
            "validated_option_chain_snapshot",
        ),
        entry_event="EntryEvent (Phase 8-10 results + snapshot + quantity)",
        exit_event="ExitEvent (Phase 8-10 results)",
        contract_selection_rules="OptionContractSelector (Phase 8), frozen before evaluation",
        trade_quality_gates="TradeQualityEngine (Phase 9), frozen before evaluation",
        risk_gates="OptionsRiskEngine (Phase 10), frozen before evaluation",
        fill_model="options-paper-fill-1 (Phase 11); bid/ask observed, else last price",
        fill_model_version="options-paper-fill-1",
        transaction_costs="commission on premium value via PaperBroker (Phase 11)",
        evaluation_metrics=(
            "eligible_opportunities",
            "rejected_by_reason",
            "executed_trades",
            "win_rate",
            "gross/net pnl",
            "profit_factor",
            "max_drawdown",
            "holding_duration",
            "capital_utilization",
        ),
        development_period=None,
        validation_period=None,
        protected_oos_period=None,
        exclusion_rules="no future data, no chain/snapshot reuse outside its timestamp",
        minimum_sample_requirements="unset; blocking on dataset availability",
        stopping_criteria="stop at the historical-data capability gate unless a validated dataset exists",
        known_limitations=(
            "no verified historical options-chain dataset (gate UNAVAILABLE)",
            "no explicit approved options strategy (STRATEGY_SPECIFICATION_REQUIRED)",
            "fixture-only evaluation is not market evidence",
        ),
    )


def from_dict(payload: Mapping[str, object]) -> ResearchProtocol:
    """Rebuild a protocol from its stable dict form (round-trip for pinning)."""
    return ResearchProtocol(
        research_question=str(payload["research_question"]),
        hypothesis=str(payload["hypothesis"]),
        strategy_specification_status=str(
            payload.get("strategy_specification_status", STRATEGY_SPECIFICATION_REQUIRED)
        ),
        candidate_strategy=str(payload["candidate_strategy"]) if payload.get("candidate_strategy") else None,
        input_features=tuple(str(x) for x in payload.get("input_features", [])),
        entry_event=str(payload["entry_event"]) if payload.get("entry_event") else None,
        exit_event=str(payload["exit_event"]) if payload.get("exit_event") else None,
        contract_selection_rules=str(payload["contract_selection_rules"]) if payload.get("contract_selection_rules") else None,
        trade_quality_gates=str(payload["trade_quality_gates"]) if payload.get("trade_quality_gates") else None,
        risk_gates=str(payload["risk_gates"]) if payload.get("risk_gates") else None,
        fill_model=str(payload["fill_model"]) if payload.get("fill_model") else None,
        fill_model_version=str(payload["fill_model_version"]) if payload.get("fill_model_version") else None,
        transaction_costs=str(payload["transaction_costs"]) if payload.get("transaction_costs") else None,
        evaluation_metrics=tuple(str(x) for x in payload.get("evaluation_metrics", [])),
        development_period=str(payload["development_period"]) if payload.get("development_period") else None,
        validation_period=str(payload["validation_period"]) if payload.get("validation_period") else None,
        protected_oos_period=str(payload["protected_oos_period"]) if payload.get("protected_oos_period") else None,
        exclusion_rules=str(payload["exclusion_rules"]) if payload.get("exclusion_rules") else None,
        minimum_sample_requirements=str(payload["minimum_sample_requirements"]) if payload.get("minimum_sample_requirements") else None,
        stopping_criteria=str(payload["stopping_criteria"]) if payload.get("stopping_criteria") else None,
        known_limitations=tuple(str(x) for x in payload.get("known_limitations", [])),
    )


__all__ = [
    "PROTOCOL_SCHEMA_VERSION",
    "STRATEGY_SPECIFICATION_REQUIRED",
    "ResearchProtocol",
    "protocol_fingerprint",
    "freeze",
    "default_protocol",
    "from_dict",
]