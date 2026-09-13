"""Tests for the attributed paper-trade schema (evaluation/paper_trades.py)."""
from __future__ import annotations

from decimal import Decimal

from fno_ai_paper_trading.evaluation.paper_trades import (
    AttributedPaperTrade,
    append_paper_trade,
    load_paper_trades,
    save_paper_trades,
)


def _trade(**overrides) -> AttributedPaperTrade:
    base = dict(
        strategy_id="moving_average_cross",
        strategy_family="TREND_FOLLOWING",
        strategy_version="1.0.0",
        configuration_version="abc",
        entry_time="2026-09-11T09:25:00",
        exit_time="2026-09-11T11:30:00",
        side="SELL",
        entry_price=Decimal("100"),
        exit_price=Decimal("99"),
        price_pnl=Decimal("50"),
        commission=Decimal("1"),
        net_pnl=Decimal("49"),
        signal="SELL",
        regime="sideways_normal",
    )
    base.update(overrides)
    return AttributedPaperTrade(**base)


def test_defaults_and_attribution():
    trade = _trade()
    assert trade.bucket == "paper"
    assert trade.strategy_id == "moving_average_cross"
    assert trade.strategy_family == "TREND_FOLLOWING"
    assert trade.trading_date == "2026-09-11"
    assert trade.strategy_name == "moving_average_cross"


def test_round_trip_keeps_bucket_and_fields(tmp_path):
    trade = _trade(bucket="research_replay", regime=None)
    save_paper_trades(tmp_path / "p.json", [trade])
    loaded = load_paper_trades(tmp_path / "p.json")
    assert len(loaded) == 1
    assert loaded[0].bucket == "research_replay"
    assert loaded[0].trading_date == "2026-09-11"
    assert loaded[0].regime is None
    assert loaded[0].net_pnl == Decimal("49")


def test_append(tmp_path):
    path = tmp_path / "p.json"
    append_paper_trade(path, _trade(side="LONG", strategy_id="c4_trend_gated_ma_cross"))
    append_paper_trade(path, _trade(side="SELL"))
    loaded = load_paper_trades(path)
    assert len(loaded) == 2
    assert {t.strategy_id for t in loaded} == {"c4_trend_gated_ma_cross", "moving_average_cross"}


def test_missing_file_is_empty(tmp_path):
    assert load_paper_trades(tmp_path / "nope.json") == []


def test_compat_with_list_form(tmp_path):
    path = tmp_path / "p.json"
    path.write_text(
        '[{"bucket": "paper", "strategy_id": "x", "strategy_family": "X", '
        '"strategy_version": "1", "configuration_version": "c", '
        '"entry_time": "2026-09-11T09:15:00", "exit_time": "2026-09-11T09:20:00", '
        '"side": "LONG", "entry_price": "10", "exit_price": "10", '
        '"price_pnl": "0", "commission": "0", "net_pnl": "0"}]',
        encoding="utf-8",
    )
    loaded = load_paper_trades(path)
    assert len(loaded) == 1
    assert loaded[0].strategy_id == "x"
    assert loaded[0].bucket == "paper"