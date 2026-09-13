"""Preregistered challenger hypotheses for the walk-forward engine (WS 7.18).

Anti-overfitting discipline: every challenger's STRATEGY PARAMETERS are frozen
constants defined here in the catalog -- they are never derived from the
evidence window. Evidence only decides WHETHER a preregistered hypothesis fires
(and thereby spawns a challenger with its fixed, documented parameter set).
No search, no tuning, no least-negative-winner selection, and the protected
out-of-sample period is never read.

All challengers are regime-filtered variants of the frozen MA(5,21) crossover
(the WS 7.11 challenger machinery): a regime-filtered challenger can only stay
flat longer than the baseline, it can never widen an open risk state, and it
never modifies the baseline strategy itself.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Callable, Mapping

from fno_ai_paper_trading.strategies import (
    RegimeFilteredMovingAverageCross,
    Strategy,
)

from fno_ai_paper_trading.walkforward.config import WalkForwardConfig

_ZERO = Decimal("0")


@dataclass(frozen=True)
class WindowSlice:
    """Outcome totals for one evidence bucket (a regime, or a regime + side)."""

    trades: int
    wins: int
    realized_pnl: Decimal

    def to_dict(self) -> dict[str, object]:
        return {
            "trades": self.trades,
            "wins": self.wins,
            "realized_pnl": str(self.realized_pnl),
        }


@dataclass(frozen=True)
class ChampionWindowStats:
    """Deterministic champion evidence for a trailing window, by regime/side."""

    by_regime_side: Mapping[tuple[str, str], WindowSlice]
    total: WindowSlice

    def slice(self, regime_label: str, side: str) -> WindowSlice:
        return self.by_regime_side.get(
            (regime_label, side), WindowSlice(0, 0, _ZERO)
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "by_regime_side": {
                f"{label}|{side}": slice_.to_dict()
                for (label, side), slice_ in sorted(self.by_regime_side.items())
            },
            "total": self.total.to_dict(),
        }


def _trend_of(label: str) -> str:
    """Reduce a regime label (e.g. ``up_normal``) to its trend token."""
    return label.upper().split("_")[0]


def _sum_slices(
    stats: ChampionWindowStats, trends: tuple[str, ...], sides: tuple[str, ...]
) -> WindowSlice:
    trades = 0
    wins = 0
    pnl = _ZERO
    for (label, side), chunk in stats.by_regime_side.items():
        if _trend_of(label) in trends and side in sides:
            trades += chunk.trades
            wins += chunk.wins
            pnl += chunk.realized_pnl
    return WindowSlice(trades, wins, pnl)


def _money(value: Decimal) -> str:
    return format(value, "f")


def _h1_predicate(
    stats: ChampionWindowStats,
    config: WalkForwardConfig,
    *,
    require_coverage: bool = False,
) -> tuple[bool, tuple[str, ...]]:
    """Suppress BUY in non-UP regimes: evidence must show non-UP BUYs lose."""
    non_up = _sum_slices(stats, ("DOWN", "SIDEWAYS"), ("BUY",))
    up = _sum_slices(stats, ("UP",), ("BUY",))
    minimum = config.min_hypothesis_regime_trades
    reasons: list[str] = []
    if non_up.trades < minimum:
        return False, (
            f"non-UP BUY trades {non_up.trades} below minimum {minimum}",
        )
    if not (up.realized_pnl > _ZERO and non_up.realized_pnl < _ZERO):
        reasons = [
            f"UP BUY net {_money(up.realized_pnl)} / non-UP BUY net "
            f"{_money(non_up.realized_pnl)} does not separate"
        ]
        if require_coverage:
            if not (up.realized_pnl >= -non_up.realized_pnl):
                reasons.append(
                    f"UP BUY net {_money(up.realized_pnl)} does not cover the "
                    f"non-UP BUY loss {_money(non_up.realized_pnl)}"
                )
        return False, tuple(reasons)
    coverage = ""
    if require_coverage and not (up.realized_pnl >= -non_up.realized_pnl):
        return False, (
            f"UP BUY net {_money(up.realized_pnl)} does not cover the non-UP "
            f"BUY loss {_money(non_up.realized_pnl)}",
        )
    if up.trades >= minimum and require_coverage:
        coverage = (
            f"; UP BUY net {_money(up.realized_pnl)} covers the non-UP BUY loss"
        )
    return True, (
        f"non-UP BUY trades lose {_money(non_up.realized_pnl)} over "
        f"{non_up.trades} trades while UP BUY wins {_money(up.realized_pnl)}"
        f"{coverage}",
    )


def _h2_predicate(
    stats: ChampionWindowStats,
    config: WalkForwardConfig,
    *,
    require_coverage: bool = False,
) -> tuple[bool, tuple[str, ...]]:
    """Suppress SELL in non-DOWN regimes: evidence must show non-DOWN SELLs lose."""
    non_down = _sum_slices(stats, ("UP", "SIDEWAYS"), ("SELL",))
    down = _sum_slices(stats, ("DOWN",), ("SELL",))
    minimum = config.min_hypothesis_regime_trades
    if non_down.trades < minimum:
        return False, (
            f"non-DOWN SELL trades {non_down.trades} below minimum {minimum}",
        )
    if not (down.realized_pnl > _ZERO and non_down.realized_pnl < _ZERO):
        return False, (
            f"DOWN SELL net {_money(down.realized_pnl)} / non-DOWN SELL net "
            f"{_money(non_down.realized_pnl)} does not separate",
        )
    coverage = ""
    if require_coverage and not (down.realized_pnl >= -non_down.realized_pnl):
        return False, (
            f"DOWN SELL net {_money(down.realized_pnl)} does not cover the "
            f"non-DOWN SELL loss {_money(non_down.realized_pnl)}",
        )
    if down.trades >= minimum and require_coverage:
        coverage = (
            f"; DOWN SELL net {_money(down.realized_pnl)} covers the non-DOWN loss"
        )
    return True, (
        f"non-DOWN SELL trades lose {_money(non_down.realized_pnl)} over "
        f"{non_down.trades} trades while DOWN SELL wins {_money(down.realized_pnl)}"
        f"{coverage}",
    )


def _h3_predicate(
    stats: ChampionWindowStats,
    config: WalkForwardConfig,
) -> tuple[bool, tuple[str, ...]]:
    """Suppress entries in SIDEWAYS regimes (trend days only)."""
    sideways = _sum_slices(stats, ("SIDEWAYS",), ("BUY", "SELL"))
    trend = _sum_slices(stats, ("UP", "DOWN"), ("BUY", "SELL"))
    minimum = config.min_hypothesis_regime_trades
    if sideways.trades < minimum:
        return False, (
            f"SIDEWAYS trades {sideways.trades} below minimum {minimum}",
        )
    if not (sideways.realized_pnl < _ZERO and trend.realized_pnl > _ZERO):
        return False, (
            f"SIDEWAYS net {_money(sideways.realized_pnl)} / trend-days net "
            f"{_money(trend.realized_pnl)} does not separate",
        )
    return True, (
        f"SIDEWAYS trades lose {_money(sideways.realized_pnl)} over "
        f"{sideways.trades} trades while trend-days trades win "
        f"{_money(trend.realized_pnl)}",
    )


@dataclass(frozen=True)
class ChallengerSpec:
    """One preregistered, fixed-parameter challenger hypothesis."""

    key: str
    title: str
    hypothesis: str
    strategy_name: str
    strategy_params: Mapping[str, object]
    predicate: Callable[[ChampionWindowStats, WalkForwardConfig], tuple[bool, tuple[str, ...]]]

    def build_strategy(self) -> Strategy:
        if self.strategy_name != "regime_filtered_ma_cross":
            raise ValueError(f"unsupported strategy {self.strategy_name!r}")
        return RegimeFilteredMovingAverageCross(**dict(self.strategy_params))


# Priority order matters: the first hypothesis whose evidence predicate fires
# within a round is selected (subject to the per-round budget cap).
CATALOG: tuple[ChallengerSpec, ...] = (
    ChallengerSpec(
        key="suppress_buys_not_up",
        title="Suppress BUY entries outside UP regimes",
        hypothesis=(
            "Champion BUY entries entered outside UP regimes are, on aggregate, "
            "negative after frictions while UP-regime BUY entries are positive -- "
            "evaluate suppressing BUY when the regime trend at the decision bar is "
            "not UP (WS 7.11 challenger)."
        ),
        strategy_name="regime_filtered_ma_cross",
        strategy_params={"fast": 5, "slow": 21, "trend_threshold_pct": "0.05", "allowed_trends": ["UP"]},
        predicate=_h1_predicate,
    ),
    ChallengerSpec(
        key="suppress_sells_not_down",
        title="Suppress SELL entries outside DOWN regimes",
        hypothesis=(
            "Champion SELL entries entered outside DOWN regimes are, on aggregate, "
            "negative after frictions while DOWN-regime SELL entries are positive -- "
            "evaluate suppressing SELL when the regime trend at the decision bar is "
            "not DOWN (WS 7.11 challenger)."
        ),
        strategy_name="regime_filtered_ma_cross",
        strategy_params={"fast": 5, "slow": 21, "trend_threshold_pct": "0.05", "allowed_trends": ["DOWN"]},
        predicate=_h2_predicate,
    ),
    ChallengerSpec(
        key="suppress_sideways_entries",
        title="Suppress entries in SIDEWAYS regimes",
        hypothesis=(
            "Champion entries taken in SIDEWAYS regimes are, on aggregate, "
            "negative after frictions while trend-day entries are positive -- "
            "evaluate suppressing entries when the regime trend at the decision "
            "bar is SIDEWAYS (WS 7.11 challenger)."
        ),
        strategy_name="regime_filtered_ma_cross",
        strategy_params={"fast": 5, "slow": 21, "trend_threshold_pct": "0.05", "allowed_trends": ["UP", "DOWN"]},
        predicate=_h3_predicate,
    ),
    ChallengerSpec(
        key="stricter_trend_gate",
        title="Stricter trend discernment for BUY suppression",
        hypothesis=(
            "The stronger-trend regime variant (0.10 gap threshold) separates "
            "UP-regime BUY winners from non-UP BUY losers more cleanly than the "
            "0.05 default -- evaluate it on future-only data (WS 7.11 challenger)."
        ),
        strategy_name="regime_filtered_ma_cross",
        strategy_params={"fast": 5, "slow": 21, "trend_threshold_pct": "0.10", "allowed_trends": ["UP"]},
        predicate=lambda stats, config: _h1_predicate(stats, config, require_coverage=True),
    ),
)

CATALOG_BY_KEY: dict[str, ChallengerSpec] = {spec.key: spec for spec in CATALOG}


def select_challengers(
    stats: ChampionWindowStats,
    config: WalkForwardConfig,
    *,
    unavailable_keys: set[str] | None = None,
) -> list[ChallengerSpec]:
    """Pick challenger hypotheses whose evidence predicates fire this round.

    ``unavailable_keys`` blocks hypotheses that already have an open challenger
    (a hypothesis is never duplicated while a sibling is still validating).
    Selection stops at ``max_challengers_per_round``.
    """
    blocked = unavailable_keys or set()
    chosen: list[ChallengerSpec] = []
    for spec in CATALOG:
        if spec.key in blocked:
            continue
        fired, _ = spec.predicate(stats, config)
        if fired:
            chosen.append(spec)
        if len(chosen) >= config.max_challengers_per_round:
            break
    return chosen