"""Promotion gate — decides, from evidence only, whether a challenger may be promoted.

A challenger becomes the champion only after robust, risk-preserving evidence
(§17f.9): an out-of-sample performance edge is required, validation must not be
materially worse, drawdown must not worsen, and the number of out-of-sample days
must meet a minimum. The gate is a pure decision over *evidence views* — it
imports no strategy, risk, broker, or service code and can never enable live
trading or change risk controls. A rejected challenger is untouched; a promoted
one is recorded by the :class:`VersionRegistry` as the new champion for later
paper-loop stages to reference.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping


@dataclass(frozen=True)
class DeltaView:
    """Plain, JSON-friendly slice of a challenger-vs-champion delta."""

    challenger_name: str
    period: str | None
    champion_net_pnl: Decimal
    challenger_net_pnl: Decimal
    champion_max_drawdown_pct: Decimal
    challenger_max_drawdown_pct: Decimal
    beats_champion: bool

    @property
    def net_pnl_delta(self) -> Decimal:
        return self.challenger_net_pnl - self.champion_net_pnl

    @classmethod
    def from_delta(cls, delta: Any) -> "DeltaView":
        """Build a view from a ``ChallengerDelta`` (WS 7.11) record."""
        return cls(
            challenger_name=delta.challenger_name,
            period=delta.period,
            champion_net_pnl=delta.champion_net_pnl,
            challenger_net_pnl=delta.challenger_net_pnl,
            champion_max_drawdown_pct=delta.champion_max_drawdown_pct,
            challenger_max_drawdown_pct=delta.challenger_max_drawdown_pct,
            beats_champion=delta.beats_champion,
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "DeltaView":
        """Build a view from a serialized delta dict (e.g. report JSON)."""
        return cls(
            challenger_name=str(payload["challenger_name"]),
            period=payload.get("period"),
            champion_net_pnl=Decimal(str(payload["champion_net_pnl"])),
            challenger_net_pnl=Decimal(str(payload["challenger_net_pnl"])),
            champion_max_drawdown_pct=Decimal(
                str(payload["champion_max_drawdown_pct"])
            ),
            challenger_max_drawdown_pct=Decimal(
                str(payload["challenger_max_drawdown_pct"])
            ),
            beats_champion=bool(payload["beats_champion"]),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "challenger_name": self.challenger_name,
            "period": self.period,
            "champion_net_pnl": str(self.champion_net_pnl),
            "challenger_net_pnl": str(self.challenger_net_pnl),
            "champion_max_drawdown_pct": str(self.champion_max_drawdown_pct),
            "challenger_max_drawdown_pct": str(self.challenger_max_drawdown_pct),
            "beats_champion": self.beats_champion,
        }


@dataclass(frozen=True)
class PromotionCriteria:
    """Predefined thresholds a challenger must clear to be promoted."""

    oos_min_days: int = 3
    require_validation_not_worse: bool = True
    require_positive_oos_pnl: bool = False

    def __post_init__(self) -> None:
        if self.oos_min_days < 1:
            raise ValueError("oos_min_days must be >= 1")


@dataclass(frozen=True)
class PromotionVerdict:
    """The gate's decision plus the full evidence trail."""

    candidate_name: str
    decision: str  # "PROMOTE" | "REJECT"
    reasons: tuple[str, ...]
    evidence: Mapping[str, Any]

    @property
    def promoted(self) -> bool:
        return self.decision == "PROMOTE"


def _money(value: Decimal) -> str:
    return f"{value:f}"


class PromotionGate:
    """Pure, deterministic decision over challenger evidence.

    ``evaluate`` operates on plain :class:`DeltaView` values (period ->
    challenger name -> delta) plus day counts, so it never depends on the
    evaluation machinery. ``evaluate_from_comparison`` adapts a WS 7.11
    ``MultiPeriodComparison`` into those views.
    """

    def evaluate(
        self,
        deltas: Mapping[str, Mapping[str, DeltaView]],
        period_counts: Mapping[str, int],
        challenger_name: str,
        *,
        criteria: PromotionCriteria | None = None,
    ) -> PromotionVerdict:
        criteria = criteria or PromotionCriteria()
        oos_days = period_counts.get("out_of_sample", 0)
        blockers: list[str] = []
        evidence: dict[str, Any] = {}

        if deltas.get("out_of_sample") is None:
            blockers.append("no out-of-sample period was evaluated")
            oos_delta: DeltaView | None = None
        else:
            oos_delta = deltas["out_of_sample"].get(challenger_name)
            if oos_delta is None:
                blockers.append(f"challenger {challenger_name!r} has no out-of-sample delta")

        if oos_days < criteria.oos_min_days:
            blockers.append(
                f"out-of-sample days {oos_days} < minimum {criteria.oos_min_days}"
            )

        validation_delta = (
            deltas.get("validation", {}).get(challenger_name)
            if deltas.get("validation")
            else None
        )
        found_validation = deltas.get("validation") is not None

        if oos_delta is not None:
            if not oos_delta.beats_champion:
                blockers.append(
                    "out-of-sample does not beat the champion "
                    f"(net P&L delta {_money(oos_delta.net_pnl_delta)}, "
                    f"maxDD % {_money(oos_delta.challenger_max_drawdown_pct)} "
                    f"vs {_money(oos_delta.champion_max_drawdown_pct)})"
                )
            if criteria.require_positive_oos_pnl and oos_delta.challenger_net_pnl <= 0:
                blockers.append("out-of-sample net P&L is not positive")

        if (
            criteria.require_validation_not_worse
            and found_validation
            and validation_delta is not None
            and validation_delta.net_pnl_delta < 0
        ):
            blockers.append(
                "validation is worse than the champion "
                f"(net P&L delta {_money(validation_delta.net_pnl_delta)})"
            )

        if oos_delta is not None:
            evidence["out_of_sample_days"] = oos_days
            evidence["out_of_sample"] = oos_delta.to_dict()
        if validation_delta is not None:
            evidence["validation"] = validation_delta.to_dict()
        evidence["criteria"] = {
            "oos_min_days": criteria.oos_min_days,
            "require_validation_not_worse": criteria.require_validation_not_worse,
            "require_positive_oos_pnl": criteria.require_positive_oos_pnl,
        }

        if blockers:
            return PromotionVerdict(
                candidate_name=challenger_name,
                decision="REJECT",
                reasons=tuple(blockers),
                evidence=evidence,
            )

        reasons = (
            f"out-of-sample beats champion: net P&L delta {_money(oos_delta.net_pnl_delta)} "
            f"(challenger {_money(oos_delta.challenger_net_pnl)} vs "
            f"{_money(oos_delta.champion_net_pnl)}), maxDD % "
            f"{_money(oos_delta.challenger_max_drawdown_pct)} <= "
            f"{_money(oos_delta.champion_max_drawdown_pct)} over {oos_days} day(s)",
            "validation is not worse than the champion"
            if validation_delta is not None
            else "no validation evidence required",
            "promotion is paper-only evidence; it never enables live trading "
            "and never weakens risk controls",
        )
        return PromotionVerdict(
            candidate_name=challenger_name,
            decision="PROMOTE",
            reasons=reasons,
            evidence=evidence,
        )

    def evaluate_from_comparison(
        self,
        comparison: Any,
        challenger_name: str,
        *,
        criteria: PromotionCriteria | None = None,
    ) -> PromotionVerdict:
        """Evaluate a challenger inside a WS 7.11 ``MultiPeriodComparison``."""
        deltas: dict[str, dict[str, DeltaView]] = {}
        for period, report in comparison.by_period.items():
            for entry in report.entries:
                if entry.delta is None:
                    continue
                deltas.setdefault(period, {})[entry.name] = DeltaView.from_delta(
                    entry.delta
                )
        return self.evaluate(
            deltas,
            dict(comparison.period_counts),
            challenger_name,
            criteria=criteria,
        )

    def evaluate_from_report_dict(
        self,
        report: Mapping[str, Any],
        challenger_name: str,
        *,
        criteria: PromotionCriteria | None = None,
    ) -> PromotionVerdict:
        """Evaluate from a serialized multi-period comparison (report JSON)."""
        deltas: dict[str, dict[str, DeltaView]] = {}
        for period, period_report in report.get("by_period", {}).items():
            for entry in period_report.get("challengers", []):
                delta = entry.get("delta")
                if delta is None:
                    continue
                deltas.setdefault(period, {})[entry["name"]] = DeltaView.from_dict(delta)
        period_counts = dict(report.get("period_counts", {}))
        return self.evaluate(deltas, period_counts, challenger_name, criteria=criteria)