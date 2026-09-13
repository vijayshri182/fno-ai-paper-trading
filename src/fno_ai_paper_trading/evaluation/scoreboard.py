"""Persistent machine-readable scoreboard for the research & competition layer.

Builds the single ``research_scoreboard.json`` consumed by the dashboard and
by fresh sessions.  Everything is derived from *recorded* artifacts
(``candidates_eval.json``, ``oos_confirmation.json``, the trade ledger cross
checks).  It never recomputes protected-OOS numbers, never re-reads datasets.

Structure::

    {
      schema_version, generated_at,
      registry_catalog, families,
      champion: {strategy_id, family, version, status},
      competition: {...}                <- from family_competition (A-E tiers)
      algo_health_attribution: {...},   <- family/strategy/version attribution
      research_allocation: {...},       <- equal now; evidence-based priority
      ensemble_status: {...},           <- architecture contract, NOT deployed
      daily_performance_ref, insuffices
    }

Insufficient data is reported as such; nothing is fabricated.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from fno_ai_paper_trading.evaluation.family_competition import (
    build_competition,
    criteria,
)
from fno_ai_paper_trading.strategies.registry import StrategyRegistry, discover

DEFAULT_PATHS = {
    "candidates_eval": Path("reports/model_performance/candidates_eval.json"),
    "oos_confirmation": Path("reports/model_performance/oos_confirmation.json"),
    "trade_ledger": Path("reports/algorithm_state/trade_ledger.json"),
    "summary": Path("reports/model_performance/summary.json"),
}

# Research allocation is equal by default; an evidence-based priority is a
# ranking hint for the *nightly research loop* only - it is never a capital
# allocation and never changes live paper trading.
EQUAL_ALLOCATION_NOTE = (
    "research-budget allocation is EQUAL across registered families; "
    "research_priority is an evidence-based order for the nightly research "
    "loop, never a capital allocation."
)


def _load(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, ValueError):
        return {}


def _champion_oos_trades(ledger: Mapping[str, Any]) -> int | None:
    cc = ledger.get("cross_checks") or {}
    value = cc.get("oos_trade_count")
    return int(value) if value is not None else None


def research_priority(competition: Mapping[str, Any]) -> dict[str, Any]:
    families = competition.get("leaderboard", {})
    ranking: list[dict[str, Any]] = []
    for family, rows in sorted(families.items()):
        tiers = [r["tier"] for r in rows]
        total_trades = sum((r.get("oos_trades") or 0) for r in rows)
        ranking.append({
            "family": family,
            "members": len(rows),
            "best_tier": min(tiers) if tiers else "D",
            "tiers": {t: tiers.count(t) for t in sorted(set(tiers))},
            "recorded_oos_trades": total_trades,
            "priority_hint": f"evaluate next against a FRESH untouched OOS period",
        })
    ranking.sort(key=lambda r: {"C": 0, "B": 1, "D": 2, "E": 3}.get(r["best_tier"], 9))
    return {
        "model_equal_allocation": True,
        "note": EQUAL_ALLOCATION_NOTE,
        "families": ranking,
    }


def ensemble_status() -> dict[str, Any]:
    return {
        "status": "ARCHITECTURE_ONLY_NOT_DEPLOYED",
        "note": (
            "An ensemble/meta-decision layer is a future *candidate*; any real "
            "ensemble must pass the same design/validation/protected-OOS "
            "discipline (no easier path), and only an adopted champion may "
            "decide paper-trading entries. No ensemble weights are invented "
            "and none are active."
        ),
    }


def champion_row(registry: StrategyRegistry) -> dict[str, Any]:
    spec = registry.get("moving_average_cross")
    return {
        "strategy_id": spec.strategy_id,
        "strategy_family": spec.family,
        "strategy_name": spec.strategy_name,
        "version": spec.version,
        "configuration_version": spec.configuration_version,
        "status": "FROZEN_NOT_PROMOTABLE-under-current-recorded-evidence",
        "paper_trading": "champion drives paper entries today via the paper server",
    }


def build_scoreboard(
    registry: StrategyRegistry | None = None,
    paths: Mapping[str, Path] | None = None,
) -> dict[str, Any]:
    registry = registry or discover()
    paths = paths or {k: Path(v) for k, v in DEFAULT_PATHS.items()}
    recorded = _load(paths["candidates_eval"])
    oos = _load(paths["oos_confirmation"])
    ledger = _load(paths["trade_ledger"])

    competition = build_competition(
        registry, recorded, oos, champion_oos_trades=_champion_oos_trades(ledger)
    )
    health = ledger.get("versions") or {}
    board = {
        "schema_version": "1",
        "deliverable": "algorithm_research_scoreboard",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "registry_catalog": registry.to_dict().get("catalog", {}),
        "families": registry.to_dict().get("families", {}),
        "champion": champion_row(registry),
        "competition": competition,
        "algo_health_attribution": {
            "algorithm_version": health.get("algorithm_version", "v1-baseline-ma521"),
            "configuration_version": health.get("configuration_version", "v1-paper-defaults"),
            "champion_strategy_id": "moving_average_cross",
            "champion_strategy_family": "TREND_FOLLOWING",
            "champion_strategy_version": registry.get("moving_average_cross").version,
            "note": "Algorithm Health is attributed to the champion strategy; " "family/strategy/version attribution shown here feeds daily aggregation.",
        },
        "research_allocation": research_priority(competition),
        "ensemble_status": ensemble_status(),
        "insufficient_data": {
            s: "protected OOS not consumed; cannot rank"
            for s, row in competition.get("criteria", {}).items()
            if row.get("oos_net_pnl") is None
        },
    }
    return board


def write_scoreboard(path: Path, board: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(board, indent=2, ensure_ascii=False), encoding="utf-8")


def load_scoreboard(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return _load(path)


def compact_for_state(scoreboard: Mapping[str, Any]) -> dict[str, Any]:
    """Small serializable summary embedded into docs/project_state.json."""
    comp = scoreboard.get("competition") or {}
    board = {
        "current_champion": (scoreboard.get("champion") or {}).get("strategy_id", "moving_average_cross"),
        "champion_family": (scoreboard.get("champion") or {}).get("strategy_family", "TREND_FOLLOWING"),
        "best_tested": comp.get("best_tested"),
        "best_tested_present": comp.get("best_tested_present"),
        "headline": comp.get("headline", ""),
        "families": list((scoreboard.get("families") or {}).keys()),
        "research_allocation": (scoreboard.get("research_allocation") or {}).get("model_equal_allocation"),
        "ensemble_status": (scoreboard.get("ensemble_status") or {}).get("status"),
        "scoreboard_artifact": "reports/algorithm_state/research_scoreboard.json",
    }
    return board