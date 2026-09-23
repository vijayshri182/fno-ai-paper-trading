"""Tests for the strategy registry (strategies/registry.py).

The registry must register the frozen champion and the c1..c5 candidates
and the multi-indicator composite challenger without modifying them, expose
the expected families, and round-trip through its JSON catalog.
"""
from __future__ import annotations

from fno_ai_paper_trading.strategies.registry import (
    StrategyRegistry,
    discover,
    load_catalog,
)
from fno_ai_paper_trading.strategies.spec import (
    BREAKOUT,
    MOMENTUM,
    REGIME_SWITCHING,
    TREND_FOLLOWING,
)


def test_default_registry_contents():
    registry = discover()
    assert "moving_average_cross" in registry
    for candidate in (
        "c1_long_only_ma_cross",
        "c2_slow_long_only_ma_cross",
        "c3_momentum_gated_ma_cross",
        "c4_trend_gated_ma_cross",
        "c5_donchian_breakout",
        "composite_multi_indicator",
    ):
        assert candidate in registry, candidate
    assert len(registry) == 7


def test_default_families():
    registry = discover()
    families = set(registry.families())
    assert TREND_FOLLOWING in families
    assert MOMENTUM in families
    assert REGIME_SWITCHING in families
    assert BREAKOUT in families
    assert registry.family_specs(TREND_FOLLOWING)[0].strategy_id == "c1_long_only_ma_cross"


def test_champion_unchanged():
    registry = discover()
    spec = registry.get("moving_average_cross")
    assert spec.family == TREND_FOLLOWING
    assert spec.parameters == {"fast": 5, "slow": 21}
    assert spec.version == "1.0.0"
    provider = registry.create("moving_average_cross")
    assert callable(provider)


def test_candidate_versions_uniform():
    registry = discover()
    candidates = registry.family_specs(TREND_FOLLOWING) + registry.family_specs(MOMENTUM) + registry.family_specs(BREAKOUT) + registry.family_specs(REGIME_SWITCHING)
    for spec in candidates:
        if spec.strategy_id == "moving_average_cross":
            continue
        assert spec.version == "2.0.0"


def test_register_rejects_configuration_clash():
    registry = StrategyRegistry()
    from fno_ai_paper_trading.strategies.spec import StrategySpec, TREND_FOLLOWING

    one = StrategySpec(strategy_id="s", family=TREND_FOLLOWING, strategy_name="s", version="1", parameters={"a": 1})
    two = StrategySpec(strategy_id="s", family=TREND_FOLLOWING, strategy_name="s", version="1", parameters={"a": 2})
    registry.register(one)
    try:
        registry.register(two)
    except ValueError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected ValueError on configuration clash")


def test_unregister():
    registry = StrategyRegistry()
    from fno_ai_paper_trading.strategies.spec import StrategySpec, TREND_FOLLOWING

    spec = StrategySpec(strategy_id="s", family=TREND_FOLLOWING, strategy_name="s", version="1")
    registry.register(spec)
    assert len(registry) == 1
    registry.unregister("s")
    assert len(registry) == 0
    assert "s" not in registry


def test_catalog_round_trip():
    registry = discover()
    payload = registry.to_dict()
    rebuilt = load_catalog(payload)
    assert set(rebuilt.families()) == set(registry.families())
    for spec in registry.specs():
        other = rebuilt.get(spec.strategy_id)
        assert other.configuration_version == spec.configuration_version


def test_create_raises_without_provider():
    registry = discover()
    registry.unregister("c1_long_only_ma_cross")
    from fno_ai_paper_trading.strategies.spec import BREAKOUT, StrategySpec

    bare = StrategySpec(strategy_id="bare", family=BREAKOUT, strategy_name="bare", version="1")
    registry.register(bare)
    try:
        registry.create("bare")
    except KeyError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected KeyError for spec without provider")