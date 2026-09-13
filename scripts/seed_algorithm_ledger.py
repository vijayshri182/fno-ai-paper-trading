"""Seed the closed-trade ledger for the ALGO READY / HEALTH monitor (real records).

Reads *recorded* closed trades from project artifacts and writes a bucketed,
dataset-separated trade ledger under ``reports/algorithm_state/trade_ledger.json``
(git-ignored; the single source the health assessment consumes):

* ``backtest``      — champion MA(5,21) continuous 5m replay trades entered
                      before 2026-01-01 (design+validation), from
                      ``reports/model_performance/trades.csv``.
* ``protected_oos`` — the same recorded replay trades entered on/after
                      2026-01-01 (the single-use OOS segment).
* ``paper``         — recorded closed paper trades from
                      ``reports/algorithm_state/paper_trades.json`` (empty when
                      no paper trades exist yet).
* ``today``         — paper trades closed on the current IST date.

The seeder cross-checks sums/counts against the recorded champion artifacts
(``reports/model_performance/summary.json`` and ``oos_confirmation.json``) and
fails loudly on any mismatch that is not within the documented tolerance.

No data is invented: metrics are computed from these records. Confidence is
None for the MA(5,21) baseline (it emits no confidence). PAPER ONLY.
"""
from __future__ import annotations

import csv
import json
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Sequence

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from fno_ai_paper_trading.data import market_hours
from fno_ai_paper_trading.evaluation.algorithm_health import (
    TradeMetrics,
    TradeRecord,
    compute_trade_metrics,
)

TRADES_CSV = REPO_ROOT / "reports" / "model_performance" / "trades.csv"
SUMMARY_JSON = REPO_ROOT / "reports" / "model_performance" / "summary.json"
OOS_CONFIRMATION_JSON = REPO_ROOT / "reports" / "model_performance" / "oos_confirmation.json"
PAPER_TRADES_JSON = REPO_ROOT / "reports" / "algorithm_state" / "paper_trades.json"
OUTPUT_JSON = REPO_ROOT / "reports" / "algorithm_state" / "trade_ledger.json"

ALGORITHM_VERSION = "v1-baseline-ma521"
CONFIGURATION_VERSION = "v1-paper-defaults"
STRATEGY_NAME = "moving_average_cross"
OOS_CUTOFF = datetime(2026, 1, 1, 0, 0, 0)
SLOW_PERIOD = 21
INTERVAL_MINUTES = 5

# A fresh, isolated OOS run can only emit its first crossover signal after the
# MA(slow) has enough bars: bar index `slow` (0-based -> 22 bars), i.e.
# `slow + 1` 5-minute bars after 09:15 IST. A full-replay trade entered inside
# that warm-up horizon in the protected OOS uses *pre-OOS* history and does not
# belong to the single-use OOS evidence (it is exactly the trade that makes the
# entry-time slice differ from the recorded OOS confirmation).
OOS_FIRST_SIGNAL = OOS_CUTOFF.replace(hour=9, minute=15) + timedelta(
    minutes=(SLOW_PERIOD + 1) * INTERVAL_MINUTES
)

CROSS_CHECK_TOLERANCE = Decimal("0.01")


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _d(value: str) -> Decimal:
    return Decimal(value)


def load_champion_trades(csv_path: Path) -> list[TradeRecord]:
    """Load the recorded champion replay round trips from the trades CSV."""
    records: list[TradeRecord] = []
    with csv_path.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            entry = _ts(row["entry_time"])
            records.append(TradeRecord(
                bucket="backtest",
                strategy_name=STRATEGY_NAME,
                algorithm_version=ALGORITHM_VERSION,
                configuration_version=CONFIGURATION_VERSION,
                entry_time=entry,
                exit_time=_ts(row["exit_time"]),
                side=row["side"],
                entry_price=_d(row["entry_price"]),
                exit_price=_d(row["exit_price"]),
                price_pnl=_d(row["price_pnl"]),
                commission=_d(row["commission"]),
                net_pnl=_d(row["net_pnl"]),
                confidence=None,
                exit_reason="",
            ))
    return records


def load_paper_trades(path: Path) -> list[TradeRecord]:
    """Load recorded closed paper trades from the paper ledger JSON contract."""
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        entries = payload.get("trades", [])
        if not isinstance(entries, list):
            raise ValueError(f"{path} trades must be a list")
    elif isinstance(payload, list):
        entries = payload
    else:
        raise ValueError(f"{path} must be a list or {path.name} with a trades list")
    records: list[TradeRecord] = []
    for item in entries:
        bucket = item.get("bucket", "paper")
        if bucket != "paper":
            raise ValueError(f"paper ledger entries must use bucket='paper', got {bucket!r}")
        records.append(TradeRecord(
            bucket="paper",
            strategy_name=item.get("strategy_name", STRATEGY_NAME),
            algorithm_version=item.get("algorithm_version", ALGORITHM_VERSION),
            configuration_version=item.get("configuration_version", CONFIGURATION_VERSION),
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
        ))
    return records


def _recorded_value(path: Path, key: str) -> Decimal | None:
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    for container in (payload, payload.get("continuous", {}), payload.get("periods", {})):
        if key in container:
            return _d(container[key])
    return None


def _recorded_oos_champion_net_pnl(path: Path) -> Decimal | None:
    """Champion OOS net P&L as recorded in the OOS confirmation artifact."""
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    for verdict in payload.get("verdicts", {}).values():
        evidence = verdict.get("gate_credible_positive_oos", {}).get("evidence", {})
        oos = evidence.get("out_of_sample", {})
        if oos.get("period") == "out_of_sample" and "champion_net_pnl" in oos:
            return _d(oos["champion_net_pnl"])
    return None


def cross_check(full_replay: Sequence[TradeRecord], oos: Sequence[TradeRecord]) -> dict[str, object]:
    """Verify sums/counts against the recorded champion artifacts."""
    all_trades = list(full_replay)
    full_net = sum((t.net_pnl for t in all_trades), Decimal("0"))
    full_winning = sum(1 for t in all_trades if t.net_pnl > 0)
    oos_net = sum((t.net_pnl for t in oos), Decimal("0"))
    oos_winning = sum(1 for t in oos if t.net_pnl > 0)

    summary_net = _recorded_value(SUMMARY_JSON, "net_pnl")
    summary_payload = json.loads(SUMMARY_JSON.read_text(encoding="utf-8"))
    summary_trades = None
    for container in (
        summary_payload,
        summary_payload.get("continuous", {}),
        summary_payload.get("periods", {}),
    ):
        if "num_trades" in container:
            summary_trades = container["num_trades"]
            break
    oos_confirmed = _recorded_oos_champion_net_pnl(OOS_CONFIRMATION_JSON)

    checks: dict[str, object] = {
        "full_trade_count": len(all_trades),
        "full_net_pnl_sum": str(full_net),
        "full_winning": full_winning,
        "oos_trade_count": len(oos),
        "oos_net_pnl_sum": str(oos_net),
        "oos_winning": oos_winning,
    }
    problems: list[str] = []

    if summary_net is not None:
        delta = abs(full_net - summary_net)
        ok = delta <= CROSS_CHECK_TOLERANCE
        checks["recorded_summary_net_pnl"] = str(summary_net)
        checks["delta_summary_net_pnl"] = str(delta)
        checks["summary_net_pnl_match"] = ok
        if not ok:
            problems.append(
                f"full replay net P&L {full_net} differs from recorded summary "
                f"{summary_net} by {delta}"
            )
    if summary_trades is not None:
        ok = summary_trades == len(all_trades)
        checks["recorded_summary_num_trades"] = summary_trades
        checks["summary_trade_count_match"] = ok
        if not ok:
            problems.append(
                f"trade count {len(all_trades)} differs from recorded {summary_trades}"
            )
    if oos_confirmed is not None:
        delta = abs(oos_net - oos_confirmed)
        ok = delta <= CROSS_CHECK_TOLERANCE
        checks["recorded_oos_net_pnl"] = str(oos_confirmed)
        checks["delta_oos_net_pnl"] = str(delta)
        checks["oos_net_pnl_match"] = ok
        if not ok:
            problems.append(
                f"protected OOS net P&L {oos_net} differs from recorded confirmation "
                f"{oos_confirmed} by {delta}"
            )

    checks["ok"] = not problems
    checks["problems"] = problems
    return checks


def metrics_snapshot(
    bucket: str, trades: Sequence[TradeRecord], today_ist: date
) -> dict[str, object]:
    metrics: TradeMetrics = compute_trade_metrics(
        trades, bucket=bucket, today_date=today_ist
    )
    return metrics.to_dict()


def main() -> None:
    if not TRADES_CSV.exists():
        sys.exit(f"missing recorded trades csv: {TRADES_CSV}")

    today_ist = datetime.now(timezone.utc).astimezone(market_hours.NSE_TZ).date()
    champion = load_champion_trades(TRADES_CSV)
    backtest = [t for t in champion if t.entry_time < OOS_CUTOFF]
    oos_full_slice = [t for t in champion if t.entry_time >= OOS_CUTOFF]
    oos_warmup_excluded = [t for t in oos_full_slice if t.entry_time < OOS_FIRST_SIGNAL]
    oos = [t for t in oos_full_slice if t.entry_time >= OOS_FIRST_SIGNAL]
    paper = load_paper_trades(PAPER_TRADES_JSON)
    today_records = [t for t in paper if t.exit_time.date() == today_ist]

    checks = cross_check(champion, oos)
    checks["oos_warmup_excluded_trades"] = len(oos_warmup_excluded)
    if not checks["ok"]:
        sys.exit(
            "ledger cross-check failed against recorded champion artifacts: "
            + "; ".join(checks["problems"])
        )

    ledger = {
        "schema_version": "1",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "deliverable": "algorithm_trade_ledger",
        "versions": {
            "algorithm_version": ALGORITHM_VERSION,
            "configuration_version": CONFIGURATION_VERSION,
        },
        "today_ist": today_ist.isoformat(),
        "sources": {
            "backtest": "reports/model_performance/trades.csv (champion MA(5,21) replay, entry < 2026-01-01)",
            "protected_oos": "reports/model_performance/trades.csv (champion MA(5,21) replay, entry >= 2026-01-01, excluding trades entered before the fresh-OOS first-signal time 09:15 + 22x5m so the slice matches the recorded OOS confirmation)",
            "paper": "reports/algorithm_state/paper_trades.json (recorded closed paper trades)",
            "today": "paper trades closed on current IST date",
        },
        "cross_checks": checks,
        "trades": {
            "backtest": [t.to_dict() for t in backtest],
            "protected_oos": [t.to_dict() for t in oos],
            "paper": [t.to_dict() for t in paper],
            "today": [t.to_dict() for t in today_records],
        },
        "metrics": {
            "backtest": metrics_snapshot("backtest", backtest, today_ist),
            "protected_oos": metrics_snapshot("protected_oos", oos, today_ist),
            "paper": metrics_snapshot("paper", paper, today_ist),
            "today": metrics_snapshot("today", today_records, today_ist),
        },
    }
    OUTPUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_JSON.write_text(json.dumps(ledger, indent=2), encoding="utf-8")
    print(f"wrote {OUTPUT_JSON}")
    print(
        f"records: backtest={len(backtest)} protected_oos={len(oos)} "
        f"paper={len(paper)} today={len(today_records)}"
    )
    print(f"cross-check ok={checks['ok']}")


if __name__ == "__main__":
    main()