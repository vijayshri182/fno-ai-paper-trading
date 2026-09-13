"""Tests for daily strategy/family aggregation (evaluation/daily_performance.py).

Pure-math aggregation over synthetic trades; `champion_replay_rows` is tested
against a tiny CSV written to a tmp dir.
"""
from __future__ import annotations

from decimal import Decimal

from fno_ai_paper_trading.evaluation.daily_performance import (
    aggregate,
    aggregate_attributed,
    champion_replay_rows,
    latest_trading_day,
    to_report,
    write_report,
)
from fno_ai_paper_trading.evaluation.paper_trades import AttributedPaperTrade


def _t(net: str, day="2026-09-11", strategy="moving_average_cross", family="TREND_FOLLOWING", bucket="paper") -> AttributedPaperTrade:
    value = Decimal(net)
    return AttributedPaperTrade(
        strategy_id=strategy,
        strategy_family=family,
        strategy_version="1.0.0",
        configuration_version="abc",
        entry_time=f"{day}T09:15:00",
        exit_time=f"{day}T10:00:00",
        side="SELL",
        entry_price=Decimal("100"),
        exit_price=Decimal("100"),
        price_pnl=value,
        commission=Decimal("0"),
        net_pnl=value,
        signal="SELL",
        regime="sideways_normal",
        bucket=bucket,
    )


def test_daily_aggregation_math():
    rows = aggregate([
        _t("10", day="2026-09-11"),
        _t("-4", day="2026-09-11"),
        _t("3", day="2026-09-11"),
        _t("-7", day="2026-09-11"),
    ])
    assert len(rows) == 1
    row = rows[0]
    assert row.trading_date == "2026-09-11"
    assert row.trades == 4
    assert row.wins == 2
    assert row.losses == 2
    assert row.win_rate == Decimal("50")
    assert row.daily_pnl == Decimal("2")
    assert row.cumulative_pnl == Decimal("2")
    assert row.max_drawdown == Decimal("8")
    assert row.strategy_family == "TREND_FOLLOWING"


def test_cumulative_and_drawdown_span_days():
    rows = aggregate([
        _t("100", day="2026-09-10"),
        _t("-40", day="2026-09-11"),
        _t("-20", day="2026-09-12"),
        _t("30", day="2026-09-13"),
    ])
    by_date = {r.trading_date: r for r in rows}
    assert by_date["2026-09-13"].cumulative_pnl == Decimal("70")
    assert by_date["2026-09-13"].max_drawdown == Decimal("60")
    assert [r.trading_date for r in rows] == ["2026-09-10", "2026-09-11", "2026-09-12", "2026-09-13"]


def test_grouping_by_strategy_and_family():
    rows = aggregate([
        _t("5", day="2026-09-11", strategy="a"),
        _t("7", day="2026-09-11", strategy="b", family="MOMENTUM"),
    ])
    assert len(rows) == 2
    families = {r.strategy_family for r in rows}
    assert families == {"TREND_FOLLOWING", "MOMENTUM"}


def test_latest_trading_day():
    rows = aggregate([_t("0", day="2026-09-11"), _t("0", day="2026-09-09")])
    assert latest_trading_day(rows) == "2026-09-11"


def test_champion_replay_rows(tmp_path):
    csv_path = tmp_path / "trades.csv"
    csv_path.write_text(
        "rt_index,entry_time,exit_time,side,quantity,entry_price,exit_price,entry_regime,exit_regime,"
        "holding_bars,price_pnl,commission,net_pnl\n"
        "1,2026-01-02T09:15:00,2026-01-02T10:00:00,SELL,1,100,99,down_low,down_low,1,50,1,49\n",
        encoding="utf-8",
    )
    rows = champion_replay_rows(
        csv_path,
        strategy_id="moving_average_cross",
        family="TREND_FOLLOWING",
        strategy_version="1.0.0",
        configuration_version="cfg",
    )
    assert len(rows) == 1
    assert rows[0]["strategy_id"] == "moving_average_cross"
    assert rows[0]["bucket"] == "research_replay"
    assert rows[0]["net_pnl"] == "49"


def test_aggregate_attributed_combines_buckets():
    rows = aggregate_attributed(
        paper_trades=[_t("-1", day="2026-09-11", bucket="paper")],
        recorded_rows=[
            {
                "strategy_id": "moving_average_cross",
                "strategy_family": "TREND_FOLLOWING",
                "strategy_version": "1.0.0",
                "configuration_version": "",
                "entry_time": "2026-09-11T09:15:00",
                "exit_time": "2026-09-11T09:20:00",
                "side": "SELL",
                "entry_price": "100",
                "exit_price": "100",
                "price_pnl": "-2",
                "commission": "0",
                "net_pnl": "-2",
                "signal": "",
                "regime": "",
                "bucket": "research_replay",
            }
        ],
    )
    assert len(rows) == 1
    assert rows[0].bucket == "paper"
    assert rows[0].daily_pnl == Decimal("-3")


def test_report_round_trip(tmp_path):
    rows = aggregate([_t("1")])
    path = tmp_path / "daily.json"
    write_report(path, rows)
    report = to_report(rows)
    assert report["deliverable"] == "daily_strategy_performance"
    assert path.exists()
    import json

    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert len(loaded["rows"]) == 1