"""Build the daily strategy/family performance report for the research layer.

Attribution is recorded-only:

* champion replay (``reports/model_performance/trades.csv``) -> bucket
  ``research_replay`` (the recorded champion daily P&L series);
* recorded closed paper trades (``reports/algorithm_state/paper_trades.json``)
  -> bucket ``paper`` (empty until live paper trading produces trades).

Writes ``reports/algorithm_state/daily_performance.json`` (machine-readable)
read by the dashboard and future sessions.  Nothing recomputes protected-OOS
trade-level statistics; daily rows are sums of recorded closed trades.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from fno_ai_paper_trading.evaluation.daily_performance import (
    aggregate_attributed,
    champion_replay_rows,
    latest_trading_day,
    to_report,
    write_report,
)
from fno_ai_paper_trading.evaluation.paper_trades import load_paper_trades

TRADES_CSV = REPO_ROOT / "reports" / "model_performance" / "trades.csv"
PAPER_TRADES_JSON = REPO_ROOT / "reports" / "algorithm_state" / "paper_trades.json"
OUTPUT_JSON = REPO_ROOT / "reports" / "algorithm_state" / "daily_performance.json"


def main() -> None:
    replay_rows: list[dict] = []
    if TRADES_CSV.exists():
        replay_rows = champion_replay_rows(
            TRADES_CSV,
            strategy_id="moving_average_cross",
            family="TREND_FOLLOWING",
            strategy_version="1.0.0",
            configuration_version="",
        )
        print(f"champion replay rows: {len(replay_rows)} (recorded, bucket=research_replay)")
    else:
        print("WARNING: trades.csv missing; champion daily series unavailable")

    paper = load_paper_trades(PAPER_TRADES_JSON)
    print(f"recorded paper trades: {len(paper)}")

    rows = aggregate_attributed(paper_trades=paper, recorded_rows=replay_rows)
    write_report(OUTPUT_JSON, rows)
    print(f"wrote {OUTPUT_JSON} ({len(rows)} daily strategy rows; latest {latest_trading_day(rows)})")


if __name__ == "__main__":
    main()