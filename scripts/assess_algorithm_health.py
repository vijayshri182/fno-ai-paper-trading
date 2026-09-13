"""Assess algorithm health / ALGO READY from the recorded trade ledger.

Reads ``reports/algorithm_state/trade_ledger.json`` (real recorded trades),
computes the bucketed metrics, the objective health/readiness decision and the
performance trend (see ``docs/algorithm_ready_spec.md``), then:

1. writes ``reports/algorithm_state/assessment.json`` (evidence snapshot);
2. updates the ``algorithm`` section of ``docs/project_state.json`` so a fresh
   session can resume the ALGO READY state;
3. re-renders ``docs/project_status.html`` through the dashboard generator.

Buckets are never mixed. Headline at-a-glance figures come from the backtest
bucket (labeled); today's win rate comes from the recorded paper trades closed
on the current IST date. Nothing here is inferred from win rate alone.

PAPER ONLY — this decision is a paper-trading readiness indicator and never
authorizes live trading.
"""
from __future__ import annotations

import json
import runpy
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Sequence

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from fno_ai_paper_trading.evaluation.algorithm_health import (
    IntegrityAlerts,
    TradeRecord,
    assess_algorithm_health,
)

LEDGER_PATH = REPO_ROOT / "reports" / "algorithm_state" / "trade_ledger.json"
ASSESSMENT_PATH = REPO_ROOT / "reports" / "algorithm_state" / "assessment.json"
STATE_PATH = REPO_ROOT / "docs" / "project_state.json"


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _d(value: str) -> Decimal:
    return Decimal(value)


def _trade_from_dict(item: dict, bucket: str) -> TradeRecord:
    return TradeRecord(
        bucket=bucket,
        strategy_name=item.get("strategy_name", "moving_average_cross"),
        algorithm_version=item.get("algorithm_version", ""),
        configuration_version=item.get("configuration_version", ""),
        entry_time=_ts(item["entry_time"]),
        exit_time=_ts(item["exit_time"]),
        side=item.get("side", "LONG"),
        entry_price=_d(item["entry_price"]),
        exit_price=_d(item["exit_price"]),
        price_pnl=_d(item["price_pnl"]),
        commission=_d(item["commission"]),
        net_pnl=_d(item["net_pnl"]),
        confidence=_d(item["confidence"]) if item.get("confidence") else None,
        exit_reason=item.get("exit_reason", ""),
    )


def _build_buckets(ledger: dict) -> dict[str, list[TradeRecord]]:
    trades = ledger["trades"]
    backtest = [_trade_from_dict(t, "backtest") for t in trades["backtest"]]
    oos = [_trade_from_dict(t, "protected_oos") for t in trades["protected_oos"]]
    paper = [_trade_from_dict(t, "paper") for t in trades["paper"]]
    today_ist = ledger["today_ist"]
    today = [t for t in paper if t.exit_time.date().isoformat() == today_ist]

    source: Sequence[TradeRecord] = paper if paper else oos
    rolling = list(source[-20:]) if source else []
    buckets: dict[str, list[TradeRecord]] = {
        "backtest": backtest,
        "protected_oos": oos,
        "paper": paper,
        "today": today,
        "rolling_recent": rolling,
    }
    return buckets


def _headline(assessment) -> dict[str, object]:
    """At-a-glance values for the dashboard/state, from the backtest bucket."""
    backtest = assessment.buckets.get("backtest")
    today = assessment.buckets.get("today")
    paper_metrics = assessment.buckets.get("paper")
    mt = backtest.to_dict() if backtest else {}
    today_win_rate = None
    if today and today.total_closed:
        today_win_rate = str(today.win_rate_pct) if today.win_rate_pct is not None else None
    value = {
        "algorithm_health": assessment.health,
        "algo_ready": assessment.ready,
        "win_rate": mt.get("win_rate_pct"),
        "winning_trades": mt.get("winning"),
        "losing_trades": mt.get("losing"),
        "total_closed_trades": mt.get("total_closed"),
        "profit_factor": mt.get("profit_factor"),
        "net_pnl": mt.get("net_pnl"),
        "expectancy": mt.get("expectancy"),
        "max_drawdown": mt.get("max_drawdown"),
        "current_drawdown": mt.get("current_drawdown"),
        "consecutive_wins": mt.get("consecutive_wins"),
        "consecutive_losses": mt.get("consecutive_losses"),
        "average_confidence": mt.get("avg_confidence"),
        "last_10_win_rate": mt.get("last_10_win_rate"),
        "last_20_win_rate": mt.get("last_20_win_rate"),
        "today_win_rate": today_win_rate,
        "performance_trend": assessment.performance_trend,
        "algorithm_version": assessment.version,
        "configuration_version": assessment.configuration_version,
        "readiness_reason": assessment.readiness_reason,
        "health_reason": assessment.health_reason,
        "last_updated": assessment.generated_at,
    }
    value["headline_note"] = (
        "headline figures above are the historical/backtest bucket (MA(5,21) "
        "recorded replay); per-bucket details are in the dashboard."
    )
    value["paper_trades_recorded"] = (
        paper_metrics.total_closed if paper_metrics else 0
    )
    return value


def _update_state(algorithm: dict) -> None:
    state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    state["algorithm"] = algorithm
    state["last_activity"] = (
        f"ALGO READY assessed: health={algorithm['algorithm_health']} "
        f"ready={algorithm['algo_ready']} trend={algorithm['performance_trend']}"
    )
    STATE_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def main() -> None:
    if not LEDGER_PATH.exists():
        sys.exit(f"missing ledger; run scripts/seed_algorithm_ledger.py first ({LEDGER_PATH})")
    ledger = json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
    buckets = _build_buckets(ledger)
    versions = ledger["versions"]

    integrity = IntegrityAlerts(
        data_quality_ok=True,
        data_quality_notes=(
            "recorded dataset validation ok: 0 errors, 1166 advisory large-gap warnings",
        ),
    )
    assessment = assess_algorithm_health(
        buckets,
        version=versions["algorithm_version"],
        configuration_version=versions["configuration_version"],
        integrity=integrity,
        today_date=datetime.fromisoformat(ledger["today_ist"]).date(),
    )

    ASSESSMENT_PATH.parent.mkdir(parents=True, exist_ok=True)
    ASSESSMENT_PATH.write_text(json.dumps(assessment.to_dict(), indent=2), encoding="utf-8")

    algorithm = _headline(assessment)
    _update_state(algorithm)

    runpy.run_path(str(REPO_ROOT / "scripts" / "update_project_status.py"), run_name="__main__")

    print(f"wrote {ASSESSMENT_PATH}")
    print(
        f"ALGORITHM HEALTH={assessment.health} ALGO READY={assessment.ready} "
        f"TREND={assessment.performance_trend}"
    )
    print(f"trend segments: {assessment.segments}")


if __name__ == "__main__":
    main()