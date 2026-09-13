"""Walk-forward promotion gate (WS 7.18, evidence-only).

A challenger may only become the champion after its future-only validation
window is complete AND every gate requirement clears: it must beat the champion
on the held-out window, be profitable after costs, respect drawdown / tail /
consecutive-loss / frequency / cost-efficiency limits, and must not degrade any
regime it participates in relative to the champion. Every rejected challenger is
recorded with its reasons; promotion is bookkeeping over evidence -- it never
enables live trading and never weakens risk controls.

The base comparison reuses the existing :class:`PromotionGate` (WS 7.12) by
projecting the challenger's future-only validation window onto the
``out_of_sample`` period view, then layers the walk-forward-specific objective
checks on top.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType
from typing import Any, Mapping

from fno_ai_paper_trading.promotion.gate import (
    DeltaView,
    PromotionCriteria,
    PromotionGate,
)

from fno_ai_paper_trading.walkforward.config import WalkForwardConfig

_ZERO = Decimal("0")
_INF = Decimal("Infinity")


@dataclass(frozen=True)
class WindowSummary:
    """Per-regime slice inside a window's aggregation."""

    trades: int
    realized_pnl: Decimal

    def to_dict(self) -> dict[str, object]:
        return {"trades": self.trades, "realized_pnl": str(self.realized_pnl)}


@dataclass(frozen=True)
class WindowMetrics:
    """Aggregated outcome of one algorithm over a future-only validation window."""

    days: int
    trades: int
    wins: int
    losses: int
    net_pnl: Decimal
    transaction_costs: Decimal
    slippage: Decimal
    gross_profit: Decimal
    gross_loss: Decimal
    profit_factor: Decimal | None
    max_drawdown_pct: Decimal
    consecutive_losing_days: int
    worst_single_loss: Decimal | None
    avg_trades_per_day: Decimal
    exposure_pct: Decimal
    per_regime: Mapping[str, WindowSummary]

    def to_dict(self) -> dict[str, Any]:
        return {
            "days": self.days,
            "trades": self.trades,
            "wins": self.wins,
            "losses": self.losses,
            "net_pnl": str(self.net_pnl),
            "transaction_costs": str(self.transaction_costs),
            "slippage": str(self.slippage),
            "gross_profit": str(self.gross_profit),
            "gross_loss": str(self.gross_loss),
            "profit_factor": str(self.profit_factor) if self.profit_factor is not None else None,
            "max_drawdown_pct": str(self.max_drawdown_pct),
            "consecutive_losing_days": self.consecutive_losing_days,
            "worst_single_loss": (
                str(self.worst_single_loss) if self.worst_single_loss is not None else None
            ),
            "avg_trades_per_day": str(self.avg_trades_per_day),
            "exposure_pct": str(self.exposure_pct),
            "per_regime": {k: v.to_dict() for k, v in self.per_regime.items()},
        }


@dataclass(frozen=True)
class WalkForwardGateCriteria:
    """Objective thresholds that gate a challenger's promotion."""

    min_validation_days: int = 20
    min_validation_trades: int = 10
    min_profit_factor: Decimal = Decimal("1.0")
    max_drawdown_pct: Decimal = Decimal("5")
    max_consecutive_losing_days: int = 8
    max_tail_loss_pct: Decimal = Decimal("10")
    min_net_pnl_per_cost: Decimal = Decimal("1.0")
    max_trades_per_day: Decimal = Decimal("10")
    min_regime_trades: int = 5
    regime_degradation_tolerance: Decimal = Decimal("0.75")

    def __post_init__(self) -> None:
        from fno_ai_paper_trading.utils.functions import positive_int

        positive_int(self.min_validation_days, "min_validation_days")
        positive_int(self.min_validation_trades, "min_validation_trades")
        positive_int(self.min_regime_trades, "min_regime_trades")
        for name in (
            "min_profit_factor",
            "max_drawdown_pct",
            "max_tail_loss_pct",
            "min_net_pnl_per_cost",
            "max_trades_per_day",
            "regime_degradation_tolerance",
        ):
            object.__setattr__(self, name, Decimal(str(getattr(self, name))))

    @classmethod
    def from_config(cls, config: WalkForwardConfig) -> "WalkForwardGateCriteria":
        return cls(
            min_validation_days=config.validation_window_days,
            min_validation_trades=config.min_validation_trades,
            min_profit_factor=config.min_profit_factor,
            max_drawdown_pct=config.max_drawdown_pct,
            max_consecutive_losing_days=config.max_consecutive_losing_days,
            max_tail_loss_pct=config.max_tail_loss_pct,
            min_net_pnl_per_cost=config.min_net_pnl_per_cost,
            max_trades_per_day=config.max_trades_per_day,
            min_regime_trades=config.min_regime_trades,
            regime_degradation_tolerance=config.regime_degradation_tolerance,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "min_validation_days": self.min_validation_days,
            "min_validation_trades": self.min_validation_trades,
            "min_profit_factor": str(self.min_profit_factor),
            "max_drawdown_pct": str(self.max_drawdown_pct),
            "max_consecutive_losing_days": self.max_consecutive_losing_days,
            "max_tail_loss_pct": str(self.max_tail_loss_pct),
            "min_net_pnl_per_cost": str(self.min_net_pnl_per_cost),
            "max_trades_per_day": str(self.max_trades_per_day),
            "min_regime_trades": self.min_regime_trades,
            "regime_degradation_tolerance": str(self.regime_degradation_tolerance),
        }


@dataclass(frozen=True)
class WalkForwardVerdict:
    """The gate's decision plus the full evidence trail."""

    challenger_id: str
    decision: str  # PROMOTE | REJECT | INSUFFICIENT_EVIDENCE
    reasons: tuple[str, ...]
    metrics: Mapping[str, Any]

    @property
    def promoted(self) -> bool:
        return self.decision == "PROMOTE"

    def to_dict(self) -> dict[str, Any]:
        return {
            "challenger_id": self.challenger_id,
            "decision": self.decision,
            "reasons": list(self.reasons),
            "metrics": dict(self.metrics),
        }


def agg_window_metrics(
    day_metrics: Mapping[str, Mapping[str, Any]],
) -> WindowMetrics:
    """Aggregate per-day metric dicts into one window-level :class:`WindowMetrics`."""
    days = sorted(day_metrics)
    total_trades = 0
    wins = 0
    losses = 0
    net = _ZERO
    costs = _ZERO
    slippage = _ZERO
    gross_profit = _ZERO
    gross_loss = _ZERO
    exposure_units = _ZERO
    max_dd = _ZERO
    worst: Decimal | None = None
    per_regime: dict[str, WindowSummary] = {}
    losing_streak = 0
    max_losing_streak = 0
    for day in days:
        m = day_metrics[day]
        total_trades += int(m.get("trades", 0))
        wins += int(m.get("wins", 0))
        losses += int(m.get("losses", 0))
        day_pnl = Decimal(str(m.get("net_pnl", "0")))
        net += day_pnl
        costs += Decimal(str(m.get("costs", "0")))
        slippage += Decimal(str(m.get("slippage", "0")))
        gross_profit += Decimal(str(m.get("gross_profit", "0")))
        gross_loss += Decimal(str(m.get("gross_loss", "0")))
        max_dd = max(max_dd, Decimal(str(m.get("max_drawdown_pct", "0"))))
        daily_worst = (
            Decimal(str(m["worst_loss"])) if m.get("worst_loss") is not None else None
        )
        if daily_worst is not None and (worst is None or daily_worst < worst):
            worst = daily_worst
        for label, chunk in m.get("per_regime", {}).items():
            summary = per_regime.get(label, WindowSummary(0, _ZERO))
            per_regime[label] = WindowSummary(
                trades=summary.trades + int(chunk.get("trades", 0)),
                realized_pnl=summary.realized_pnl
                + Decimal(str(chunk.get("realized_pnl", "0"))),
            )
        exposure_units += Decimal(str(m.get("exposure_pct", "0")))
        if day_pnl > _ZERO:
            losing_streak = 0
        else:
            losing_streak += 1
            max_losing_streak = max(max_losing_streak, losing_streak)

    profit_factor: Decimal | None
    if gross_loss > _ZERO:
        profit_factor = (gross_profit / gross_loss) if gross_profit > _ZERO else _ZERO
    else:
        profit_factor = _INF if gross_profit > _ZERO else _ZERO
    avg_trades = (Decimal(total_trades) / Decimal(len(days))) if days else _ZERO
    exposure = (exposure_units / Decimal(len(days))) if days else _ZERO

    return WindowMetrics(
        days=len(days),
        trades=total_trades,
        wins=wins,
        losses=losses,
        net_pnl=net,
        transaction_costs=costs,
        slippage=slippage,
        gross_profit=gross_profit,
        gross_loss=gross_loss,
        profit_factor=profit_factor,
        max_drawdown_pct=max_dd,
        consecutive_losing_days=max_losing_streak,
        worst_single_loss=worst,
        avg_trades_per_day=avg_trades,
        exposure_pct=exposure,
        per_regime=MappingProxyType(dict(sorted(per_regime.items()))),
    )


def _money(value: Decimal) -> str:
    return format(value, "f")


def _verdict(
    challenger_id: str,
    decision: str,
    reasons: tuple[str, ...],
    metrics: dict[str, Any],
) -> WalkForwardVerdict:
    return WalkForwardVerdict(
        challenger_id=challenger_id,
        decision=decision,
        reasons=reasons,
        metrics=MappingProxyType(metrics),
    )


class WalkForwardGate:
    """Pure, deterministic promotion decision over validation-window evidence."""

    def __init__(self, criteria: WalkForwardGateCriteria | None = None) -> None:
        self.criteria = criteria or WalkForwardGateCriteria()
        self._base = PromotionGate()

    def evaluate(
        self,
        challenger: WindowMetrics,
        champion: WindowMetrics,
        challenger_id: str,
        *,
        base_equity: Decimal,
    ) -> WalkForwardVerdict:
        criteria = self.criteria
        reasons: list[str] = []

        if challenger.days < criteria.min_validation_days:
            return _verdict(
                challenger_id,
                "INSUFFICIENT_EVIDENCE",
                (
                    f"validation days {challenger.days} below minimum "
                    f"{criteria.min_validation_days}",
                ),
                self._metrics_bundle(challenger, champion, challenger_id, criteria),
            )
        if challenger.trades < criteria.min_validation_trades:
            return _verdict(
                challenger_id,
                "INSUFFICIENT_EVIDENCE",
                (
                    f"validation trades {challenger.trades} below minimum "
                    f"{criteria.min_validation_trades}",
                ),
                self._metrics_bundle(challenger, champion, challenger_id, criteria),
            )

        base_view = DeltaView(
            challenger_name=challenger_id,
            period="out_of_sample",
            champion_net_pnl=champion.net_pnl,
            challenger_net_pnl=challenger.net_pnl,
            champion_max_drawdown_pct=champion.max_drawdown_pct,
            challenger_max_drawdown_pct=challenger.max_drawdown_pct,
            beats_champion=challenger.net_pnl > champion.net_pnl,
        )
        metrics = self._metrics_bundle(challenger, champion, challenger_id, criteria)

        base = self._base.evaluate(
            {"out_of_sample": {challenger_id: base_view}},
            {"out_of_sample": challenger.days},
            challenger_id,
            criteria=PromotionCriteria(
                oos_min_days=criteria.min_validation_days,
                require_validation_not_worse=False,
                require_positive_oos_pnl=True,
            ),
        )
        metrics["base"] = dict(base.evidence)
        metrics["base_decision"] = base.decision
        if base.decision != "PROMOTE":
            reasons.extend(base.reasons)

        if challenger.losses > 0:
            if (
                challenger.profit_factor is not None
                and challenger.profit_factor < criteria.min_profit_factor
            ):
                reasons.append(
                    f"profit factor {_money(challenger.profit_factor)} below "
                    f"minimum {_money(criteria.min_profit_factor)}"
                )
        if challenger.max_drawdown_pct > criteria.max_drawdown_pct:
            reasons.append(
                f"max drawdown {_money(challenger.max_drawdown_pct)}% above "
                f"limit {_money(criteria.max_drawdown_pct)}%"
            )
        if challenger.consecutive_losing_days > criteria.max_consecutive_losing_days:
            reasons.append(
                f"consecutive losing days {challenger.consecutive_losing_days} "
                f"above limit {criteria.max_consecutive_losing_days}"
            )
        if challenger.worst_single_loss is not None and base_equity > _ZERO:
            tail_limit = criteria.max_tail_loss_pct / Decimal("100") * base_equity
            if abs(challenger.worst_single_loss) > tail_limit:
                reasons.append(
                    f"worst single loss {_money(challenger.worst_single_loss)} "
                    f"exceeds tail limit {_money(-tail_limit)} "
                    f"(max_tail_loss_pct {_money(criteria.max_tail_loss_pct)}%)"
                )
        if challenger.transaction_costs > _ZERO:
            required = criteria.min_net_pnl_per_cost * challenger.transaction_costs
            if challenger.net_pnl < required:
                reasons.append(
                    f"net P&L {_money(challenger.net_pnl)} below {_money(required)} "
                    f"(= min_net_pnl_per_cost {_money(criteria.min_net_pnl_per_cost)} "
                    f"x costs {_money(challenger.transaction_costs)})"
                )
        if challenger.avg_trades_per_day > criteria.max_trades_per_day:
            reasons.append(
                f"average trades/day {_money(challenger.avg_trades_per_day)} "
                f"above limit {_money(criteria.max_trades_per_day)}"
            )
        for label, summary in challenger.per_regime.items():
            if summary.trades < criteria.min_regime_trades:
                continue
            champion_summary = champion.per_regime.get(label)
            champion_net = (
                champion_summary.realized_pnl if champion_summary is not None else _ZERO
            )
            allowed_loss = criteria.regime_degradation_tolerance * abs(champion_net)
            if summary.realized_pnl < -allowed_loss:
                reasons.append(
                    f"regime {label} degrades: challenger net "
                    f"{_money(summary.realized_pnl)} vs champion "
                    f"{_money(champion_net)} (allowed loss {_money(-allowed_loss)})"
                )

        if reasons:
            return _verdict(challenger_id, "REJECT", tuple(reasons), metrics)

        decision_reasons = (
            f"future-only validation window of {challenger.days} day(s) beats "
            f"the champion: net P&L delta "
            f"{_money(challenger.net_pnl - champion.net_pnl)} (challenger "
            f"{_money(challenger.net_pnl)} vs champion {_money(champion.net_pnl)}) "
            f"after costs {_money(challenger.transaction_costs)}; profit factor "
            f"{_money(challenger.profit_factor)}, maxDD% "
            f"{_money(challenger.max_drawdown_pct)}, no regime degradation; "
            "promotion is paper-only evidence -- it never enables live trading "
            "and never weakens risk controls",
        )
        return _verdict(challenger_id, "PROMOTE", (decision_reasons,), metrics)

    @staticmethod
    def _metrics_bundle(
        challenger: WindowMetrics,
        champion: WindowMetrics,
        challenger_id: str,
        criteria: WalkForwardGateCriteria,
    ) -> dict[str, Any]:
        return {
            "challenger_id": challenger_id,
            "window_days": challenger.days,
            "challenger": challenger.to_dict(),
            "champion": champion.to_dict(),
            "net_pnl_delta": str(challenger.net_pnl - champion.net_pnl),
            "criteria": criteria.to_dict(),
        }


__all__ = [
    "WalkForwardGate",
    "WalkForwardGateCriteria",
    "WalkForwardVerdict",
    "WindowMetrics",
    "WindowSummary",
    "agg_window_metrics",
]