"""Strategy registry and auto-discovery for the research & competition layer.

The registry is the single place that maps a ``strategy_id`` to its spec and
its signal provider.  It registers the frozen champion and every research
candidate (c1..c5) without modifying them.  All lookups are deterministic and
pure: ``create`` returns a fresh stateless provider callable for a spec; no
strategy object is shared or mutated.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Iterable, Mapping

from fno_ai_paper_trading.strategies.research_candidates import CANDIDATES
from fno_ai_paper_trading.strategies.spec import (
    BREAKOUT,
    MOMENTUM,
    REGIME_SWITCHING,
    TREND_FOLLOWING,
    StrategySpec,
    champion_spec,
    composite_spec,
    spec_for_candidate,
)

# Preregistered family assignment for the initial candidate portfolio.  The
# mechanism is what ships; families can be re-assigned only by editing this
# catalog, never at runtime.
_CANDIDATE_FAMILIES: Mapping[str, str] = {
    "c1_long_only_ma_cross": TREND_FOLLOWING,
    "c2_slow_long_only_ma_cross": TREND_FOLLOWING,
    "c3_momentum_gated_ma_cross": MOMENTUM,
    "c4_trend_gated_ma_cross": REGIME_SWITCHING,
    "c5_donchian_breakout": BREAKOUT,
}

#: Family for the enhanced multi-indicator composite strategy.
_COMPOSITE_FAMILY = TREND_FOLLOWING

Provider = Callable[..., list[Any]]


class StrategyRegistry:
    """Versioned catalog of strategy specs plus their providers.

    ``providers`` maps ``strategy_id`` -> a callable that produces signal
    results (the same providers the recorded evaluation used, so the registry
    never changes behavior of c1..c5 or the champion).
    """

    def __init__(self, specs: Iterable[StrategySpec] = (), providers: Mapping[str, Provider] | None = None) -> None:
        self._specs: dict[str, StrategySpec] = {}
        self._providers: dict[str, Provider] = dict(providers or {})
        for spec in specs:
            self._specs[spec.strategy_id] = spec

    # ---- construction ------------------------------------------------------
    @classmethod
    def build_default(cls) -> "StrategyRegistry":
        """Registry containing the champion + preregistered candidates."""
        registry = cls()
        registry.register(champion_spec(), provider=_champion_provider())
        registry.register(composite_spec(), provider=_composite_provider())
        for key, entry in CANDIDATES.items():
            family = _CANDIDATE_FAMILIES[key]
            provider = entry.get("provider")
            registry.register(
                spec_for_candidate(key, entry, family),
                provider=provider if callable(provider) else None,
            )
        return registry

    # ---- mutation ----------------------------------------------------------
    def register(self, spec: StrategySpec, provider: Provider | None = None) -> None:
        existing = self._specs.get(spec.strategy_id)
        if existing is not None and existing.configuration_version != spec.configuration_version:
            raise ValueError(
                f"strategy_id {spec.strategy_id!r} already registered with a different configuration"
            )
        if existing is not None:
            return
        self._specs[spec.strategy_id] = spec
        if provider is not None:
            self._providers[spec.strategy_id] = provider

    def unregister(self, strategy_id: str) -> None:
        self._specs.pop(strategy_id, None)
        self._providers.pop(strategy_id, None)

    # ---- queries -----------------------------------------------------------
    def get(self, strategy_id: str) -> StrategySpec:
        try:
            return self._specs[strategy_id]
        except KeyError as exc:
            raise KeyError(f"strategy {strategy_id!r} not registered") from exc

    def __contains__(self, strategy_id: str) -> bool:
        return strategy_id in self._specs

    def __len__(self) -> int:
        return len(self._specs)

    def specs(self) -> list[StrategySpec]:
        return sorted(self._specs.values(), key=lambda s: (s.family, s.strategy_id))

    def families(self) -> list[str]:
        return sorted({s.family for s in self._specs.values()})

    def family_specs(self, family: str) -> list[StrategySpec]:
        return sorted((s for s in self._specs.values() if s.family == family), key=lambda s: s.strategy_id)

    def provider(self, strategy_id: str) -> Provider | None:
        return self._providers.get(strategy_id)

    def create(self, strategy_id: str) -> Provider:
        """Return a fresh provider callable for a spec (never a shared object)."""
        provider = self.provider(strategy_id)
        if provider is None:
            raise KeyError(f"no provider registered for {strategy_id!r}")
        return provider

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "1",
            "catalog": {s.strategy_id: s.to_dict() for s in self.specs()},
            "families": {f: [s.strategy_id for s in self.family_specs(f)] for f in self.families()},
        }

    def catalog_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, default=str)


def _champion_provider() -> Provider:
    from fno_ai_paper_trading.strategies.moving_average_cross import MovingAverageCrossStrategy

    strategy = MovingAverageCrossStrategy(fast=5, slow=21)

    def provider(bars: Any, context: Any | None = None) -> list[Any]:
        return [strategy.analyze(bars)]

    return provider


def _composite_provider() -> Provider:
    from fno_ai_paper_trading.strategies.composite import MultiIndicatorStrategy

    strategy = MultiIndicatorStrategy(mode="trend")

    def provider(bars: Any, context: Any | None = None) -> list[Any]:
        return [strategy.analyze(bars)]

    return provider


def discover() -> StrategyRegistry:
    """Auto-discovery entry point; currently the default catalog."""
    return StrategyRegistry.build_default()


def load_catalog(payload: Mapping[str, Any]) -> StrategyRegistry:
    """Rebuild a registry from ``to_dict`` output (specs only, providers optional)."""
    specs = []
    for item in payload.get("catalog", {}).values():
        item = dict(item)
        item.pop("metadata", None)
        specs.append(StrategySpec(**{k: v for k, v in item.items() if k != "metadata"}))
    return StrategyRegistry(specs=specs)