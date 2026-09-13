"""Family-competition scoring and classification for the research layer.

Entirely *recorded-evidence* scoring: it reads the preregistered evaluation
artifacts (``candidates_eval.json``, ``oos_confirmation.json``, the champion
summary) and never recomputes, re-trains or re-reads protected OOS data.

Tiers (per strategy):

* ``C PROMOTABLE``      - recorded OOS net > 0, enough trades, per-trade t >= 2
* ``B CREDIBLE CANDIDATE`` - recorded OOS net > 0 but not yet robust enough
* ``D INSUFFICIENT DATA``  - OOS not (yet) read; cannot be ranked
* ``E REJECTED``        - recorded evidence is plainly negative (incl. the
                          frozen champion, which simply stays the champion)

``A BEST TESTED`` only exists when some entry reaches B or C.  The code thus
never calls the least-negative strategy a winner: with negative/uncertain OOS
evidence every member is E/D and "A" is reported as absent.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Mapping

from fno_ai_paper_trading.strategies.registry import StrategyRegistry

MIN_OOS_TRADES = 30
T_TWO = Decimal("2.0")
ZERO = Decimal("0")

SG_DESIGN_VALIDATION_NOTES = {
    "c1_long_only_ma_cross": "preregistered DROP on design+validation; protected OOS not consumed",
    "c2_slow_long_only_ma_cross": "shortlisted; OOS confirmation recorded",
    "c3_momentum_gated_ma_cross": "preregistered DROP on design+validation; protected OOS not consumed",
    "c4_trend_gated_ma_cross": "shortlisted; OOS confirmation recorded",
    "c5_donchian_breakout": "preregistered DROP on design+validation; protected OOS not consumed",
}

PREREGISTERED_DROPS = {"c1_long_only_ma_cross", "c3_momentum_gated_ma_cross", "c5_donchian_breakout"}


def _num(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (ValueError, TypeError, ArithmeticError):
        return None


@dataclass(frozen=True)
class CompetitionEntry:
    strategy_id: str
    strategy_family: str
    strategy_name: str
    version: str
    configuration_version: str
    design: Mapping[str, Any] = field(default_factory=dict)
    validation: Mapping[str, Any] = field(default_factory=dict)
    protected_oos: Mapping[str, Any] = field(default_factory=dict)
    oos_per_trade: Mapping[str, Any] = field(default_factory=dict)
    robustness: list[Mapping[str, Any]] = field(default_factory=list)
    cost_scan: list[Mapping[str, Any]] = field(default_factory=list)
    preregistered_note: str = ""
    recorded_gate_decision: str | None = None

    @property
    def has_oos(self) -> bool:
        return bool(self.protected_oos)

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "family": self.strategy_family,
            "strategy_name": self.strategy_name,
            "version": self.version,
            "configuration_version": self.configuration_version,
            "preregistered_note": self.preregistered_note,
        }


def _field(segment: Mapping[str, Any] | None, name: str) -> Any:
    if not segment:
        return None
    return segment.get(name)


def oos_net(entry: CompetitionEntry) -> Decimal | None:
    return _num(_field(entry.protected_oos, "net_pnl"))


def oos_return(entry: CompetitionEntry) -> Decimal | None:
    return _num(_field(entry.protected_oos, "net_return_pct"))


def oos_trades(entry: CompetitionEntry) -> int | None:
    value = _field(entry.protected_oos, "num_trades")
    return int(value) if value is not None else None


def oos_t(entry: CompetitionEntry) -> Decimal | None:
    return _num(entry.oos_per_trade.get("t"))


def criteria(entry: CompetitionEntry) -> dict[str, Any]:
    """The 21-criteria scorecard row for one entry.  Missing data is None."""
    oos = entry.protected_oos or {}
    design = entry.design or {}
    validation = entry.validation or {}

    def both(name: str) -> float | None:
        d, v = _num(_field(design, name)), _num(_field(validation, name))
        if d is None or v is None or not v:
            return None
        try:
            return abs(float(d) - float(v)) / max(abs(float(v)), 1e-9)
        except (ValueError, ZeroDivisionError):
            return None

    avg_win = _num(oos.get("avg_win"))
    avg_loss = _num(oos.get("avg_loss"))
    data_up_to = _field(oos, "end") or _field(validation, "end") or _field(design, "end")
    row = {
        "oos_net_pnl": str(oos_net(entry)) if oos_net(entry) is not None else None,
        "oos_return_pct": str(oos_return(entry)) if oos_return(entry) is not None else None,
        "expectancy_per_trade": str(_num(oos.get("expectancy_net"))) if _num(oos.get("expectancy_net")) is not None else None,
        "profit_factor": str(_num(oos.get("profit_factor"))) if _num(oos.get("profit_factor")) is not None else None,
        "win_rate_pct": str(_num(oos.get("win_rate_pct"))) if _num(oos.get("win_rate_pct")) is not None else None,
        "trade_count": oos_trades(entry),
        "max_drawdown_pct": str(_num(oos.get("max_drawdown_pct"))) if _num(oos.get("max_drawdown_pct")) is not None else None,
        "avg_win_pct": str(avg_win) if avg_win is not None else None,
        "avg_loss_pct": str(avg_loss) if avg_loss is not None else None,
        "per_trade_t": str(oos_t(entry)) if oos_t(entry) is not None else None,
        "transaction_costs": str(_num(oos.get("transaction_costs"))) if _num(oos.get("transaction_costs")) is not None else None,
        "per_trade_cost": str(_num(oos.get("per_trade_cost"))) if _num(oos.get("per_trade_cost")) is not None else None,
        "perturbation_robustness": _robustness_summary(entry.robustness),
        "oos_cost_scan_rows": len(entry.cost_scan),
        "reconciliation_ok": oos.get("reconciliation_ok"),
        "risk_violations": "none recorded" if _field(oos, "risk_violations") is None else str(oos.get("risk_violations")),
        "design_validation_stability": _design_validation_stability(entry, abs_rel=both),
        "active_version_at_oos": entry.version,
        "regime_consistency": _regime_consistency(entry.protected_oos),
        "data_thru": _field(oos, "end") or data_up_to,
        "exposure_pct": str(_num(oos.get("exposure_pct"))) if _num(oos.get("exposure_pct")) is not None else None,
    }
    return row


def _robustness_summary(robust: list[Mapping[str, Any]]) -> str | None:
    matches = [r.get("generous_sign_match") for r in robust if isinstance(r, dict)]
    if not matches:
        return None
    if all(m is True for m in matches):
        return "robust"
    if any(m is True for m in matches):
        return "mixed"
    return "not_robust"


def _design_validation_stability(entry: CompetitionEntry, abs_rel) -> str | None:
    d = _num(_field(entry.design, "net_pnl"))
    v = _num(_field(entry.validation, "net_pnl"))
    if d is None or v is None or not v:
        return None
    rel = abs_rel("net_pnl")
    if rel is None:
        return None
    drift = "stable" if rel <= 0.5 else "unstable"
    sign_flip = (d > ZERO and v < ZERO) or (d < ZERO and v > ZERO)
    if sign_flip:
        return f"{drift}; SIGNS FLIP"
    return drift


def _regime_consistency(oos: Mapping[str, Any]) -> None | str:
    # Regime detail is not part of the OOS confirmation schema; it is recorded
    # separately in the regime-eval report for the champion only.
    from fno_ai_paper_trading.evaluation import regime_eval  # noqa: F401  (may not exist in minimal envs)

    return None


def classify(entry: CompetitionEntry, min_oos_trades: int = MIN_OOS_TRADES) -> str:
    """Per-entry tier: C / B / D / E (never A; A is leaderboard-level)."""
    if entry.recorded_gate_decision:
        decision = str(entry.recorded_gate_decision).lower()
        if decision in ("promote", "promotable", "yes", "pass", "accepted"):
            return "C"
        return "E"
    net = oos_net(entry)
    trades = oos_trades(entry)
    if net is None:
        return "D"
    if net <= ZERO:
        return "E"
    if trades is None or trades < min_oos_trades:
        return "D"
    t = oos_t(entry)
    if t is not None and t >= T_TWO:
        return "C"
    return "B"


def label_text(tier: str) -> str:
    return {
        "A": "A BEST TESTED",
        "B": "B CREDIBLE CANDIDATE",
        "C": "C PROMOTABLE",
        "D": "D INSUFFICIENT DATA",
        "E": "E REJECTED",
    }[tier.upper()]


def rank_key(entry: CompetitionEntry) -> tuple:
    tier_order = {"C": 0, "B": 1, "D": 2, "E": 3}
    return (tier_order.get(classify(entry), 9), -(oos_net(entry) or ZERO))


def family_leaderboard(entries: list[CompetitionEntry]) -> dict[str, list[dict[str, Any]]]:
    board: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        row = {
            "strategy_id": entry.strategy_id,
            "strategy_name": entry.strategy_name,
            "version": entry.version,
            "tier": classify(entry),
            "tier_label": label_text(classify(entry)),
            "oos_net_pnl": str(oos_net(entry)) if oos_net(entry) is not None else None,
            "oos_return_pct": str(oos_return(entry)) if oos_return(entry) is not None else None,
            "oos_trades": oos_trades(entry),
            "oos_per_trade_t": str(oos_t(entry)) if oos_t(entry) is not None else None,
            "preregistered_note": entry.preregistered_note,
        }
        board.setdefault(entry.strategy_family, []).append(row)
    for family in board:
        board[family].sort(key=lambda r: (r["tier"], {"C": 0, "B": 1, "D": 2, "E": 3}.get(r["tier"], 9), -(float(r["oos_net_pnl"] or 0))))
    return board


def best_tested(entries: list[CompetitionEntry]) -> dict[str, Any] | None:
    """A BEST TESTED only when some entry is B or C.  Never the least-negative."""
    promotable = [e for e in entries if classify(e) in ("B", "C")]
    if not promotable:
        return None
    best = min(promotable, key=rank_key)
    return {
        "strategy_id": best.strategy_id,
        "strategy_family": best.strategy_family,
        "version": best.version,
        "tier": "A",
        "tier_label": label_text("A"),
        "oos_net_pnl": str(oos_net(best)) if oos_net(best) is not None else None,
        "oos_return_pct": str(oos_return(best)) if oos_return(best) is not None else None,
    }


def build_entries(registry: StrategyRegistry, recorded: Mapping[str, Any]) -> list[CompetitionEntry]:
    """Build CompetitionEntries from recorded artifacts + registry catalog."""
    candidates = {c["candidate"]: c for c in recorded.get("candidates", [])}
    champion_design = recorded.get("champion_design") or {}
    champion_validation = recorded.get("champion_validation") or {}
    entries: list[CompetitionEntry] = []
    for spec in registry.specs():
        design = champion_design if spec.strategy_id == "moving_average_cross" else (candidates.get(spec.strategy_id, {}).get("design") or {})
        validation = champion_validation if spec.strategy_id == "moving_average_cross" else (candidates.get(spec.strategy_id, {}).get("validation") or {})
        robustness = [] if spec.strategy_id == "moving_average_cross" else (candidates.get(spec.strategy_id, {}).get("design_validation_robustness") or [])
        cost_scan = [] if spec.strategy_id == "moving_average_cross" else (candidates.get(spec.strategy_id, {}).get("design_validation_cost_scan") or [])
        entries.append(CompetitionEntry(
            strategy_id=spec.strategy_id,
            strategy_family=spec.family,
            strategy_name=spec.strategy_name,
            version=spec.version,
            configuration_version=spec.configuration_version,
            design=dict(design),
            validation=dict(validation),
            robustness=[dict(r) for r in robustness],
            cost_scan=[dict(r) for r in cost_scan],
            preregistered_note=SG_DESIGN_VALIDATION_NOTES.get(spec.strategy_id, ""),
            recorded_gate_decision="rejected" if spec.strategy_id in PREREGISTERED_DROPS else None,
        ))
    return entries


def attach_oos(
    entry: CompetitionEntry,
    oos_confirmation: Mapping[str, Any],
    champion_oos_trades: int | None = None,
) -> CompetitionEntry:
    """Merge recorded protected-OOS data into an entry (no computation)."""
    if entry.strategy_id == "moving_average_cross":
        # Champion OOS is recorded in each candidate verdict's gate evidence
        # (champion_net_pnl), not under a champion verdict key.
        champion_oos: dict[str, Any] = {}
        for cand_verdict in oos_confirmation.get("verdicts", {}).values():
            evidence = ((cand_verdict or {}).get("gate_credible_positive_oos") or {}).get("evidence") or {}
            sample = evidence.get("out_of_sample")
            if isinstance(sample, list):
                sample = next((sub for sub in sample if isinstance(sub, dict) and "champion_net_pnl" in sub), None)
            if isinstance(sample, dict) and "champion_net_pnl" in sample:
                champion_oos = {
                    "net_pnl": sample["champion_net_pnl"],
                    "num_trades": champion_oos_trades,
                    "max_drawdown_pct": sample.get("champion_max_drawdown_pct"),
                    "period": sample.get("period"),
                }
                break
        return CompetitionEntry(
            strategy_id=entry.strategy_id,
            strategy_family=entry.strategy_family,
            strategy_name=entry.strategy_name,
            version=entry.version,
            configuration_version=entry.configuration_version,
            design=entry.design,
            validation=entry.validation,
            protected_oos=champion_oos,
            oos_per_trade=entry.oos_per_trade,
            robustness=entry.robustness,
            cost_scan=entry.cost_scan,
            preregistered_note=entry.preregistered_note,
            recorded_gate_decision=entry.recorded_gate_decision,
        )

    verdict = oos_confirmation.get("verdicts", {}).get(entry.strategy_id)
    if not verdict:
        return entry
    oos = verdict.get("protected_oos") or {}
    gate = (verdict.get("gate_credible_positive_oos") or {}).get("decision")
    oos_per_trade = verdict.get("oos_per_trade_slippage_adj_pre_commission") or {}
    robustness = verdict.get("oos_robustness") or []
    cost_scan = verdict.get("oos_cost_scan") or []
    recorded_gate = gate if gate else None
    return CompetitionEntry(
        strategy_id=entry.strategy_id,
        strategy_family=entry.strategy_family,
        strategy_name=entry.strategy_name,
        version=entry.version,
        configuration_version=entry.configuration_version,
        design=entry.design,
        validation=entry.validation,
        protected_oos=dict(oos),
        oos_per_trade=dict(oos_per_trade),
        robustness=entry.robustness + [dict(r) for r in robustness],
        cost_scan=entry.cost_scan + [dict(r) for r in cost_scan],
        preregistered_note=entry.preregistered_note,
        recorded_gate_decision=recorded_gate,
    )


def build_competition(
    registry: StrategyRegistry,
    recorded: Mapping[str, Any],
    oos_confirmation: Mapping[str, Any],
    champion_oos_trades: int | None = None,
) -> dict[str, Any]:
    entries = [attach_oos(e, oos_confirmation, champion_oos_trades) for e in build_entries(registry, recorded)]
    board = family_leaderboard(entries)
    top = best_tested(entries)
    return {
        "schema_version": "1",
        "deliverable": "algorithm_family_competition",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "min_oos_trades": MIN_OOS_TRADES,
        "conclusion": "B" if top is None else "A/B",
        "best_tested": top,
        "best_tested_present": top is not None,
        "headline": (
            "No family produced a credible positive-OOS candidate: A BEST TESTED "
            "is ABSENT. All recorded evidence remains net-negative, per-trade "
            "t < +2.0, or OOS not consumed. Family competition continues with a "
            "fresh untouched OOS period. The frozen MA(5,21) champion is NOT "
            "promotable and stays the current champion."
            if top is None else
            f"Best tested: {top['strategy_id']} tier {top['tier']}.  Promotion still requires the gate."
        ),
        "families": {f: sorted((r["strategy_id"] for r in rows), key=lambda x: x) for f, rows in board.items()},
        "leaderboard": board,
        "criteria": {e.strategy_id: criteria(e) for e in entries},
    }