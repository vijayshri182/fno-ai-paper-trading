"""Tests for Phase 7.1 AI decision-support contracts (offline only)."""
from __future__ import annotations

import ast
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from fno_ai_paper_trading.ai import AIDecision, DecisionContext, HoldDecisionSupport
from fno_ai_paper_trading.models.enums import InstrumentType, Signal
from fno_ai_paper_trading.models.instruments import Instrument


def _instrument() -> Instrument:
    return Instrument(
        symbol="NIFTY_INDEX",
        instrument_type=InstrumentType.INDEX,
        underlying_symbol="NIFTY",
    )


def _context() -> DecisionContext:
    return DecisionContext(
        instrument=_instrument(),
        timestamp=datetime(2026, 9, 11, 10, 0),
        data_reference="dataset:demo#abc123",
        features={"close": Decimal("25000"), "fast_ma": Decimal("24990"), "bars": 22},
    )


def test_hold_baseline_returns_a_structured_advisory_decision() -> None:
    context = _context()
    decision = HoldDecisionSupport().recommend(context)

    assert decision.action is Signal.HOLD
    assert decision.confidence == Decimal("1")
    assert decision.features_considered == context.features
    assert decision.market_regime == "unknown"
    assert decision.model_name == "deterministic-hold-baseline"
    assert decision.timestamp == context.timestamp
    assert decision.data_reference == context.data_reference


def test_decision_rejects_unreliable_confidence_and_float_features() -> None:
    with pytest.raises(ValueError, match="between 0 and 1"):
        AIDecision(
            action=Signal.BUY,
            confidence=Decimal("1.01"),
            rationale="test",
            features_considered={},
            market_regime="trend",
            model_name="test",
            model_version="1",
            timestamp=datetime(2026, 9, 11),
            data_reference="test-data",
        )
    with pytest.raises(TypeError, match="floats are not supported"):
        DecisionContext(
            instrument=_instrument(),
            timestamp=datetime(2026, 9, 11),
            data_reference="test-data",
            features={"close": 25000.0},
        )


def test_feature_mappings_are_immutable_after_construction() -> None:
    source = {"close": Decimal("25000")}
    context = DecisionContext(
        instrument=_instrument(),
        timestamp=datetime(2026, 9, 11),
        data_reference="test-data",
        features=source,
    )
    source["close"] = Decimal("1")

    assert context.features["close"] == Decimal("25000")
    with pytest.raises(TypeError):
        context.features["new"] = Decimal("1")  # type: ignore[index]


def test_ai_contract_package_has_no_execution_dependencies() -> None:
    ai_dir = Path(__file__).parents[1] / "src" / "fno_ai_paper_trading" / "ai"
    forbidden = {"broker", "portfolio", "risk", "services", "persistence"}
    imports: set[str] = set()
    for path in ai_dir.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imports.add(node.module.split(".")[2] if node.module.startswith("fno_ai_paper_trading.") else node.module.split(".")[0])
            elif isinstance(node, ast.Import):
                imports.update(alias.name.split(".")[0] for alias in node.names)

    assert not imports & forbidden
