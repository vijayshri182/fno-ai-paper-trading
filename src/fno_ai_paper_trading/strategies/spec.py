"""Declarative strategy contract for the Algorithm Research & Competition layer.

A strategy participates in research/paper trading through one object: a
:class:`StrategySpec`.  Specs are stateless, deterministic, fully
serializable and versioned.  They name a signal provider (how an entry/exit
decision is computed from a bar window), the family it competes in, its
version and its parameter fingerprint.

Nothing in this module executes trades, reads market data, or bypasses the
risk manager.  Research/paper code may only act on specs provided through
the registry (see ``strategies/registry.py``).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Mapping

# The research families.  ``Family`` is open-ended by design: new
# families are legal, but every family must contain at least one registered
# spec before it is ranked on the scoreboard.
TREND_FOLLOWING = "TREND_FOLLOWING"
MOMENTUM = "MOMENTUM"
BREAKOUT = "BREAKOUT"
PULLBACK = "PULLBACK"
MEAN_REVERSION = "MEAN_REVERSION"
VOLATILITY_REGIME = "VOLATILITY_REGIME"
MARKET_STRUCTURE = "MARKET_STRUCTURE"
MULTI_TIMEFRAME = "MULTI_TIMEFRAME"
REGIME_SWITCHING = "REGIME_SWITCHING"

ALL_FAMILIES: tuple[str, ...] = (
    TREND_FOLLOWING,
    MOMENTUM,
    BREAKOUT,
    PULLBACK,
    MEAN_REVERSION,
    VOLATILITY_REGIME,
    MARKET_STRUCTURE,
    MULTI_TIMEFRAME,
    REGIME_SWITCHING,
)


def is_family(value: str) -> bool:
    return value in ALL_FAMILIES


@dataclass(frozen=True)
class StrategySpec:
    """Immutable description of one registered strategy instance.

    ``strategy_id`` is unique and stable for the project; ``version`` is the
    strategy's own version; ``configuration_version`` fingerprints the exact
    parameter tuple so two versions of the same strategy never collide.
    """

    strategy_id: str
    family: str
    strategy_name: str
    version: str
    description: str = ""
    parameters: Mapping[str, Any] = field(default_factory=dict)
    configuration_version: str = ""
    entry_semantics: str = ""
    exit_semantics: str = ""
    supports_confidence: bool = False
    reproducibility: str = "deterministic and stateless"
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.strategy_id:
            raise ValueError("strategy_id is required")
        if not self.family:
            raise ValueError("family is required")
        if not self.strategy_name:
            raise ValueError("strategy_name is required")
        if not self.version:
            raise ValueError("version is required")
        if not is_family(self.family):
            raise ValueError(f"unknown family {self.family!r}")
        defaults = {"strategy_id", "family", "strategy_name", "version"}
        if not self.configuration_version:
            object.__setattr__(
                self,
                "configuration_version",
                configuration_hash(self.strategy_name, self.parameters),
            )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _json_dumps_sorted(value: Mapping[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def configuration_hash(strategy_name: str, parameters: Mapping[str, Any]) -> str:
    """Stable short fingerprint of a (strategy, parameters) combination."""
    payload = f"{strategy_name}::{_json_dumps_sorted(params_jsonable(parameters))}"
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]


def params_jsonable(parameters: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize parameter values to JSON-safe scalars/collections."""
    def _clean(value: Any) -> Any:
        if isinstance(value, Mapping):
            return {str(k): _clean(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [_clean(v) for v in value]
        if isinstance(value, (str, int, float, bool)):
            return value
        if value is None:
            return None
        return str(value)

    return {str(k): _clean(v) for k, v in parameters.items()}


def spec_for_candidate(
    candidate_key: str,
    entry: Mapping[str, Any],
    family: str,
    version: str = "2.0.0",
    supports_confidence: bool = False,
) -> StrategySpec:
    """Build a :class:`StrategySpec` from a ``research_candidates.CANDIDATES`` entry.

    Uses only the recorded ``strategy`` class name and ``params``; the class
    itself is never constructed here.  ``strategy_id`` is derived from the
    candidate key so artifacts stay traceable to the preregistered candidate.
    """
    strategy_cls = entry.get("strategy")
    strategy_name = getattr(strategy_cls, "name", None) or candidate_key
    params = dict(entry.get("params") or {})
    rationale = str(entry.get("rationale", ""))
    description = rationale or f"Research candidate {candidate_key}"
    return StrategySpec(
        strategy_id=candidate_key,
        family=family,
        strategy_name=strategy_name,
        version=version,
        description=description[:500],
        parameters=params_jsonable(params),
        entry_semantics="signal computed from a stateless provider over the decision window",
        exit_semantics="opposite signal (signal-based exit) or provider-defined stop",
        supports_confidence=supports_confidence,
        metadata={"source": "research_candidates.CANDIDATES", "candidate": candidate_key},
    )


def champion_spec(version: str = "1.0.0") -> StrategySpec:
    """The frozen MA(5,21) baseline champion, registered unmodified."""
    return StrategySpec(
        strategy_id="moving_average_cross",
        family=TREND_FOLLOWING,
        strategy_name="moving_average_cross",
        version=version,
        description=(
            "Frozen long/short MA(5,21) crossover baseline champion (algorithm "
            "v1-baseline-ma521). Kept behavior-identical while the competition "
            "layer is introduced; promotion requires the independent family "
            "competition + credible-positive-OOS discipline."
        ),
        parameters={"fast": 5, "slow": 21},
        entry_semantics="BUY when fast SMA crosses above slow; SELL on the opposite cross",
        exit_semantics="opposite signal; the strategy never holds past the next signal",
        supports_confidence=False,
        reproducibility="deterministic and stateless",
        metadata={"algorithm_version": "v1-baseline-ma521", "configuration_version": "v1-paper-defaults"},
    )


def composite_spec(version: str = "2.0.0") -> StrategySpec:
    """Enhanced multi-indicator composite (educational WS 7.18 addition).

    Trend-following preset: EMA(9/21) alignment + MACD + Supertrend + ADX
    filter + stochastic/OBV/VWAP confirmation with an RSI guard and an ATR
    stop suggestion. Registered in the neutral catalog so it can be ranked
    against the frozen champion under the existing competition discipline.
    """
    return StrategySpec(
        strategy_id="composite_multi_indicator",
        family=TREND_FOLLOWING,
        strategy_name="multi_indicator_composite",
        version=version,
        description=(
            "Multi-indicator composite (trend preset): combines EMA(9/21), MACD, "
            "Supertrend, ADX filter, stochastic, OBV, VWAP and Bollinger votes with "
            "an RSI guard and ATR-based stop distance. Educational framework only; "
            "promotion requires the same OOS discipline as any challenger."
        ),
        parameters={"mode": "trend", "warmup": 40, "entry_votes": 3, "adx_min": 20},
        entry_semantics="BUY when the trend-mode vote majority crosses the entry threshold with ADX confirmation",
        exit_semantics="opposite vote majority (signal-based exit)",
        supports_confidence=True,
        reproducibility="deterministic and stateless",
        metadata={"algorithm_version": "v2-composite-multi-indicator", "configuration_version": "v1-trend-preset"},
    )