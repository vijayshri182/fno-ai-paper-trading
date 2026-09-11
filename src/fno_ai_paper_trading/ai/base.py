"""Decision-support interface and a safe deterministic baseline."""
from __future__ import annotations

from abc import ABC, abstractmethod
from decimal import Decimal

from fno_ai_paper_trading.ai.decision import AIDecision, DecisionContext
from fno_ai_paper_trading.models.enums import Signal


class DecisionSupport(ABC):
    """Produces an advisory decision from validated context; never executes it."""

    model_name: str = "decision-support"
    model_version: str = "unknown"

    @abstractmethod
    def recommend(self, context: DecisionContext) -> AIDecision:
        """Return one structured, non-executable recommendation."""


class HoldDecisionSupport(DecisionSupport):
    """Offline baseline that recommends HOLD until a model is deliberately added."""

    model_name = "deterministic-hold-baseline"
    model_version = "1"

    def recommend(self, context: DecisionContext) -> AIDecision:
        return AIDecision(
            action=Signal.HOLD,
            confidence=Decimal("1"),
            rationale="Deterministic baseline: no model recommendation is configured.",
            features_considered=context.features,
            market_regime="unknown",
            model_name=self.model_name,
            model_version=self.model_version,
            timestamp=context.timestamp,
            data_reference=context.data_reference,
        )
