"""Tests for the strategy plugin contract (strategies/spec.py)."""
from __future__ import annotations

import pytest

from fno_ai_paper_trading.strategies.spec import (
    ALL_FAMILIES,
    BREAKOUT,
    MOMENTUM,
    REGIME_SWITCHING,
    TREND_FOLLOWING,
    StrategySpec,
    champion_spec,
    configuration_hash,
    is_family,
    spec_for_candidate,
)


def test_family_namespace():
    assert TREND_FOLLOWING in ALL_FAMILIES
    assert MOMENTUM in ALL_FAMILIES
    assert REGIME_SWITCHING in ALL_FAMILIES
    assert BREAKOUT in ALL_FAMILIES
    assert len(ALL_FAMILIES) == 7
    assert is_family("MEAN_REVERSION")
    assert not is_family("unknown")


def test_spec_requires_core_fields():
    with pytest.raises(ValueError):
        StrategySpec(strategy_id="", family=TREND_FOLLOWING, strategy_name="x", version="1")
    with pytest.raises(ValueError):
        StrategySpec(strategy_id="x", family="NOPE", strategy_name="x", version="1")
    with pytest.raises(ValueError):
        StrategySpec(strategy_id="x", family=TREND_FOLLOWING, strategy_name="", version="1")


def test_spec_computes_configuration_version():
    a = StrategySpec(strategy_id="x", family=TREND_FOLLOWING, strategy_name="x", version="1", parameters={"fast": 5})
    b = StrategySpec(strategy_id="x", family=TREND_FOLLOWING, strategy_name="x", version="1", parameters={"fast": 6})
    assert a.configuration_version == configuration_hash("x", {"fast": 5})
    assert a.configuration_version != b.configuration_version


def test_configuration_hash_deterministic_and_ordered():
    assert configuration_hash("s", {"a": 1, "b": 2}) == configuration_hash("s", {"b": 2, "a": 1})
    assert configuration_hash("s", {"a": 1}) != configuration_hash("s", {"a": 2})


def test_spec_json_serializable():
    spec = champion_spec()
    payload = spec.to_dict()
    assert payload["strategy_id"] == "moving_average_cross"
    assert payload["family"] == TREND_FOLLOWING
    assert payload["parameters"] == {"fast": 5, "slow": 21}
    assert payload["supports_confidence"] is False
    assert isinstance(payload["configuration_version"], str)


def test_champion_spec_frozen_metadata():
    spec = champion_spec()
    assert spec.version == "1.0.0"
    assert spec.metadata["algorithm_version"] == "v1-baseline-ma521"


def test_spec_for_candidate_uses_recorded_metadata():
    class FakeStrategy:
        name = "long_only_ma_cross"

    entry = {
        "provider": lambda bars: [],
        "params": {"fast": 5, "slow": 21},
        "strategy": FakeStrategy,
        "rationale": "recorded rationale here",
    }
    spec = spec_for_candidate("c1_long_only_ma_cross", entry, TREND_FOLLOWING)
    assert spec.strategy_id == "c1_long_only_ma_cross"
    assert spec.strategy_name == "long_only_ma_cross"
    assert spec.family == TREND_FOLLOWING
    assert spec.parameters == {"fast": 5, "slow": 21}
    assert "recorded rationale" in spec.description