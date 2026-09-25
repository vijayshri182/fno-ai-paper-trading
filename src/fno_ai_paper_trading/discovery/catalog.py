"""Discovery catalog: immutable, versioned, fully-recorded algorithm candidates.

Every candidate in the deck is captured as a frozen :class:`CandidateDefinition`
whose ``definition_hash`` fingerprints candidate identity + family + provider
path + parameters + version + lineage. Definitions are persisted write-once so a
research result can always be traced back to the *exact* algorithm that produced
it ("PRESERVED — fully reconstructible") and never reconstructed by hand.

The control is the frozen model_0 (MA 5/21 crossover) — it is never modified and
always runs head-to-head against every challenger on the same window.

Perturbations are a small, bounded, explainable grid of knob changes used for
robustness — entry/exit thresholds, ATR multipliers, holding periods and
confirmation windows. This is not blind brute force: it is a bounded robustness
sweep over mechanisms that are already defined and explained.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Callable

from fno_ai_paper_trading.evaluation.fast_signal import moving_average_cross_signals
from fno_ai_paper_trading.strategies import discovery_candidates as dc
from fno_ai_paper_trading.strategies.base import SignalResult

GENERATION = "g0"
CONTROL_ID = "model_0.control"

_CANDIDATE_KEYS = ["d1_trend_ema", "d2_momentum", "d3_breakout_vol",
                   "d4_pullback", "d5_mean_reversion", "d6_structure"]

FAMILY_OF = {
    "d1_trend_ema": "TREND_FOLLOWING",
    "d2_momentum": "MOMENTUM",
    "d3_breakout_vol": "BREAKOUT",
    "d4_pullback": "PULLBACK",
    "d5_mean_reversion": "MEAN_REVERSION",
    "d6_structure": "MARKET_STRUCTURE",
}

NAME_OF = {
    "d1_trend_ema": "EMA stack + slope + ADX trend",
    "d2_momentum": "RSI + ROC + acceleration momentum",
    "d3_breakout_vol": "Donchian breakout + ATR-percentile range expansion",
    "d4_pullback": "EMA trend + band pullback + RSI recovery",
    "d5_mean_reversion": "Bollinger Z-score fade (volatility-gated)",
    "d6_structure": "Swing fractal + break of structure on expansion",
}

DESC_OF = {
    "d1_trend_ema": "Long/short aligned EMA stack (10/30/60) with positive slope and ADX >= 20 confirmation; ATR trailing stop; day flat at 15:15.",
    "d2_momentum": "Long/short on RSI above/below 50 with consistent ROC and acceleration; exit when momentum fades; day flat at 15:15.",
    "d3_breakout_vol": "Buy/sell a breakout above/below prior-ch 20-bar high/low only when today's range expands to >= 1.0x the avg 20-day range; exit on channel retreat or ATR trail; day flat at 15:10.",
    "d4_pullback": "In an EMA 10/30 trend, enter on a pullback into RSI-recovery zone after ADX confirmation; stop/target/max-hold exits; day flat at 15:15.",
    "d5_mean_reversion": "Fade a z <= -1.5 (RSI <= 30) or z >= +1.5 (RSI >= 70) band touch on the Bollinger 20/2.0 band, only in NORMAL volatility; exit on mean touch/RSI fade or stop; day flat at 15:15.",
    "d6_structure": "Trade breaks of the last confirmed 2-bar swing fractal on range expansion (>= 1.0x avg 20-day range); stop on ATR / structure; day flat at 15:10.",
}


def _json_default(value: object) -> str:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    raise TypeError(f"cannot serialize {type(value).__name__}")


def _canonical(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=_json_default)


def _hash_of(
    candidate_id: str,
    family: str,
    provider_path: str,
    params: dict[str, Any],
    version: str,
    parent: str | None,
    generation: str,
) -> str:
    canonical = _canonical({
        "candidate_id": candidate_id,
        "family": family,
        "provider_path": provider_path,
        "params": params,
        "version": version,
        "parent": parent,
        "generation": generation,
    })
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _params_of(key: str) -> dict[str, Any]:
    raw = asdict(dc.PARAMS[key]())
    return json.loads(json.dumps(raw, sort_keys=True, default=_json_default))


@dataclass(frozen=True)
class CandidateDefinition:
    """Immutable, fully-recorded definition of one competing algorithm."""

    candidate_id: str
    family: str
    name: str
    description: str
    provider_path: str
    params: dict[str, Any]
    version: str
    parent: str | None
    generation: str
    definition_hash: str
    control: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "family": self.family,
            "name": self.name,
            "description": self.description,
            "provider_path": self.provider_path,
            "params": self.params,
            "version": self.version,
            "parent": self.parent,
            "generation": self.generation,
            "definition_hash": self.definition_hash,
            "control": self.control,
        }


_CONTROL_PROVIDER = "fno_ai_paper_trading.evaluation.fast_signal.moving_average_cross_signals"


def build_control(version: str = "1.0") -> CandidateDefinition:
    params = {"fast": 5, "slow": 21}
    return CandidateDefinition(
        candidate_id=CONTROL_ID,
        family="TREND_FOLLOWING",
        name="MA 5/21 crossover (frozen model_0 control)",
        description="The frozen model_0 control algorithm. Never modified. Single-instrument MA crossover, day-local, day flat by force-exit.",
        provider_path=_CONTROL_PROVIDER,
        params=params,
        version=version,
        parent=None,
        generation="control-0",
        definition_hash=_hash_of(CONTROL_ID, "TREND_FOLLOWING", _CONTROL_PROVIDER,
                                 params, version, None, "control-0"),
        control=True,
    )


def build_deck(version: str = "1.0") -> list[CandidateDefinition]:
    definitions: list[CandidateDefinition] = [
        build_control(version=version)
    ]
    for key in _CANDIDATE_KEYS:
        provider_path = f"fno_ai_paper_trading.strategies.discovery_candidates.{dc.PROVIDERS[key].__name__}"
        params = _params_of(key)
        definitions.append(
            CandidateDefinition(
                candidate_id=key,
                family=FAMILY_OF[key],
                name=NAME_OF[key],
                description=DESC_OF[key],
                provider_path=provider_path,
                params=params,
                version=version,
                parent=None,
                generation=GENERATION,
                definition_hash=_hash_of(key, FAMILY_OF[key], provider_path,
                                         params, version, None, GENERATION),
            )
        )
    return definitions


# ---------------------------------------------------------------------------
# bounded robustness perturbation grid (per family, exact field names)
# ---------------------------------------------------------------------------

_PERTURBATIONS: dict[str, list[dict[str, Any]]] = {
    "d1_trend_ema": [
        {"fast": 8, "mid": 24, "slow": 48},
        {"fast": 12, "mid": 36, "slow": 72},
        {"adx_trend": 18.0},
        {"adx_trend": 22.0},
        {"stop_atr": 2.0},
        {"stop_atr": 3.0},
        {"adx_period": 10, "atr_period": 10},
        {"adx_period": 21, "atr_period": 21},
    ],
    "d2_momentum": [
        {"rsi_period": 10, "roc_period": 8, "accel_period": 4},
        {"rsi_period": 21, "roc_period": 14, "accel_period": 7},
        {"rsi_long_enter": 55.0, "rsi_short_enter": 45.0},
        {"rsi_long_enter": 45.0, "rsi_short_enter": 55.0},
        {"roc_period": 7},
        {"roc_period": 14},
        {"accel_period": 3},
        {"accel_period": 8},
    ],
    "d3_breakout_vol": [
        {"entry_channel": 15, "exit_channel": 8},
        {"entry_channel": 30, "exit_channel": 15},
        {"range_expansion": 0.9},
        {"range_expansion": 1.2},
        {"trail_atr": 1.5},
        {"trail_atr": 2.5},
        {"atr_period": 10},
        {"atr_period": 21},
    ],
    "d4_pullback": [
        {"fast": 8, "slow": 24},
        {"fast": 12, "slow": 36},
        {"adx_trend": 16.0},
        {"adx_trend": 20.0},
        {"rsi_recover_long": 42.0, "rsi_recover_short": 58.0},
        {"stop_atr": 1.2, "target_atr": 2.0},
        {"max_hold_bars": 8},
        {"max_hold_bars": 16},
    ],
    "d5_mean_reversion": [
        {"bb_period": 15, "bb_stdev": 1.5},
        {"bb_period": 30, "bb_stdev": 2.5},
        {"entry_z_long": -1.2, "entry_z_short": 1.2},
        {"entry_z_long": -2.0, "entry_z_short": 2.0},
        {"exit_z": 0.2},
        {"exit_z": 0.5},
        {"stop_atr": 1.5},
        {"stop_atr": 2.5},
    ],
    "d6_structure": [
        {"swing_bars": 3, "entry_channel": 20},
        {"swing_bars": 1, "entry_channel": 40},
        {"range_expansion": 0.9},
        {"range_expansion": 1.2},
        {"stop_atr": 1.2},
        {"stop_atr": 2.0},
        {"atr_period": 10},
        {"atr_period": 21},
    ],
}

CONTROL_PERTURBATIONS: list[dict[str, Any]] = []


def perturbations_for(defn: CandidateDefinition) -> list[CandidateDefinition]:
    """Bounded variants of a candidate (robustness grid), each fully recorded."""
    if defn.control:
        return []
    overrides = _PERTURBATIONS.get(defn.candidate_id, [])
    variants: list[CandidateDefinition] = []
    for i, override in enumerate(overrides):
        params = dict(defn.params)
        for key in ("entry_open_minute", "entry_cutoff_minute", "force_exit_minute",
                    "confidence_threshold"):
            params.setdefault(key, defn.params.get(key, 0))
        params.update(override)
        version = f"{defn.version}.p{i + 1}"
        variants.append(
            CandidateDefinition(
                candidate_id=defn.candidate_id,
                family=defn.family,
                name=defn.name,
                description=defn.description,
                provider_path=defn.provider_path,
                params=params,
                version=version,
                parent=defn.candidate_id,
                generation=defn.generation,
                definition_hash=_hash_of(defn.candidate_id, defn.family, defn.provider_path,
                                         params, version, defn.candidate_id, defn.generation),
            )
        )
    return variants


def variants_for(defn: CandidateDefinition) -> list[CandidateDefinition]:
    """Base definition followed by its perturbation variants (control alone when frozen)."""
    return [defn] + perturbations_for(defn)


# ---------------------------------------------------------------------------
# resolution
# ---------------------------------------------------------------------------

def resolve_signal_fn(defn: CandidateDefinition) -> Callable[[list, object], list[SignalResult]]:
    """Return ``fn(bars, params) -> list[SignalResult]`` for a definition.

    ``params`` is the already-merged parameter object; for the control it is the
    plain ``{"fast": 5, "slow": 21}`` dict consumed by the MA crossover.
    """
    if defn.control:
        base_params = dict(defn.params)

        def control_fn(bars, _params=None) -> list[SignalResult]:
            return moving_average_cross_signals(bars, fast=base_params["fast"], slow=base_params["slow"])

        return control_fn
    key = defn.candidate_id
    if key not in dc.PROVIDERS:
        raise KeyError(f"no discovery provider for {key!r}")
    provider = dc.PROVIDERS[key]

    def discovery_fn(bars, params) -> list[SignalResult]:
        return provider(bars, params)

    return discovery_fn


def build_params(defn: CandidateDefinition, overrides: dict[str, Any] | None = None):
    """Build the concrete parameter object for a candidate definition."""
    if defn.control:
        return dict(defn.params)
    params_payload = dict(defn.params)
    if overrides:
        params_payload.update(overrides)
    key = defn.candidate_id
    if key not in dc.PARAMS:
        raise KeyError(f"no discovery params for {key!r}")
    return dc.PARAMS[key](**params_payload)


def persist_definition(defn: CandidateDefinition, reports_dir) -> None:
    """Write the immutable definition JSON (write-once, never overwritten)."""
    import pathlib

    path = pathlib.Path(reports_dir) / "candidates" / f"{defn.candidate_id}.{defn.version}.definition.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
        if existing.get("definition_hash") != defn.definition_hash:
            raise ValueError(
                f"definition collides on disk for {defn.candidate_id}@{defn.version}: "
                f"existing hash {existing.get('definition_hash')} != {defn.definition_hash}"
            )
        return
    path.write_text(_canonical(defn.to_dict()), encoding="utf-8")