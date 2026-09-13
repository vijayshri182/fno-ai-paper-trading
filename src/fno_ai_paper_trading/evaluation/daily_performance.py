"""Daily strategy/family aggregation for the research & competition layer.

Every closed trade observed on a trading day is aggregated by
(strategy_id, strategy_family, strategy_version).  The report answers "which
algorithm earned/rented what, on which day", never which algorithm to run:
selections come only from the family competition / promotion discipline.

Rows expose cumulative P&L and a max-drawdown on that running series, win
rate and counts.  No denominator is invented: days with zero closed trades are
simply not listed.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from fno_ai_paper_trading.evaluation.paper_trades import AttributedPaperTrade

ZERO = Decimal("0")


@dataclass(frozen=True)
class DailyRow:
    trading_date: str
    strategy_id: str
    strategy_family: str
    strategy_version: str
    trades: int
    wins: int
    losses: int
    win_rate: Decimal | None
    daily_pnl: Decimal
    cumulative_pnl: Decimal
    max_drawdown: Decimal
    active_version: str
    bucket: str = "paper"

    def to_dict(self) -> dict[str, Any]:
        return {
            "trading_date": self.trading_date,
            "strategy_id": self.strategy_id,
            "strategy_family": self.strategy_family,
            "strategy_version": self.strategy_version,
            "trades": self.trades,
            "wins": self.wins,
            "losses": self.losses,
            "win_rate_pct": str(self.win_rate) if self.win_rate is not None else None,
            "daily_pnl": str(self.daily_pnl),
            "cumulative_pnl": str(self.cumulative_pnl),
            "max_drawdown": str(self.max_drawdown),
            "active_version": self.active_version,
            "bucket": self.bucket,
        }


def _win_rate(wins: int, trades: int) -> Decimal | None:
    if trades == 0:
        return None
    return (Decimal(wins) * 100) / Decimal(trades)


def aggregate(rows: Iterable[AttributedPaperTrade]) -> list[DailyRow]:
    """Group attributed trades by (trading_date, strategy_id) and return rows.

    Rows are sorted by date then strategy_id.  Cumulative P&L and max drawdown
    are computed per strategy series across the whole window.
    """
    grouped: dict[tuple[str, str], list[AttributedPaperTrade]] = {}
    for trade in rows:
        key = (trade.trading_date, trade.strategy_id)
        grouped.setdefault(key, []).append(trade)

    per_strategy: dict[str, list[AttributedPaperTrade]] = {}
    for (_, strategy_id), day_trades in grouped.items():
        per_strategy.setdefault(strategy_id, []).extend(day_trades)

    result: list[DailyRow] = []
    for (trading_date, strategy_id), day_trades in sorted(grouped.items()):
        series = per_strategy[strategy_id]
        run_total = ZERO
        peak = ZERO
        max_dd = ZERO
        for trade in series:
            run_total += trade.net_pnl
            if run_total > peak:
                peak = run_total
            if run_total < peak:
                dd = peak - run_total
                if dd > max_dd:
                    max_dd = dd
        day_net = sum((t.net_pnl for t in day_trades), ZERO)
        wins = sum(1 for t in day_trades if t.net_pnl > ZERO)
        losses = sum(1 for t in day_trades if t.net_pnl < ZERO)
        first = day_trades[0]
        result.append(DailyRow(
            trading_date=trading_date,
            strategy_id=strategy_id,
            strategy_family=first.strategy_family,
            strategy_version=first.strategy_version,
            trades=len(day_trades),
            wins=wins,
            losses=losses,
            win_rate=_win_rate(wins, len(day_trades)),
            daily_pnl=day_net,
            cumulative_pnl=run_total,
            max_drawdown=max_dd,
            active_version=first.strategy_version,
            bucket=first.bucket,
        ))
    return result


def aggregate_attributed(
    *,
    paper_trades: Sequence[AttributedPaperTrade] = (),
    recorded_rows: Sequence[Mapping[str, Any]] = (),
) -> list[DailyRow]:
    """Combine recorded paper trades with legacy recorded rows (e.g. champion
    replay CSV rows converted via :func:`champion_replay_rows`)."""
    trades: list[AttributedPaperTrade] = list(paper_trades)
    for item in recorded_rows:
        trades.append(AttributedPaperTrade.from_dict(item))
    return aggregate(trades)


def champion_replay_rows(
    csv_path: Path, strategy_id: str = "moving_average_cross", family: str = "TREND_FOLLOWING",
    strategy_version: str = "1.0.0", configuration_version: str = "",
) -> list[dict[str, Any]]:
    """Convert the recorded champion replay CSV (trades.csv) to attributed
    rows.  Purely recorded evidence; never re-evaluates or re-trains."""
    import csv

    rows: list[dict[str, Any]] = []
    with csv_path.open(encoding="utf-8", newline="") as handle:
        for item in csv.DictReader(handle):
            rows.append({
                "strategy_id": strategy_id,
                "strategy_family": family,
                "strategy_version": strategy_version,
                "configuration_version": configuration_version,
                "entry_time": item["entry_time"],
                "exit_time": item["exit_time"],
                "side": item["side"],
                "entry_price": item["entry_price"],
                "exit_price": item["exit_price"],
                "price_pnl": item["price_pnl"],
                "commission": item["commission"],
                "net_pnl": item["net_pnl"],
                "signal": "",
                "regime": "",
                "bucket": "research_replay",
            })
    return rows


def to_report(rows: Sequence[DailyRow]) -> dict[str, Any]:
    return {
        "schema_version": "1",
        "deliverable": "daily_strategy_performance",
        "rows": [r.to_dict() for r in rows],
    }


def write_report(path: Path, rows: Sequence[DailyRow]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(to_report(rows), indent=2, ensure_ascii=False), encoding="utf-8")


def today_ist() -> str:
    # IST = UTC+5:30
    from datetime import datetime, timezone

    return (datetime.now(timezone.utc) + __import__("datetime").timedelta(hours=5, minutes=30)).date().isoformat()


def latest_trading_day(rows: Sequence[DailyRow]) -> str | None:
    return max((r.trading_date for r in rows), default=None)