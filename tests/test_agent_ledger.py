"""Tests for the paper-trade ledger sync (G6: nothing wrote it → agent does).

Covers the round-trip record contract (numbers mirror the research layer's
convention), idempotent ledger sync, and the wiring that makes the continuous
paper agent the producer of ``reports/algorithm_state/paper_trades.json``.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from fno_ai_paper_trading.agent.agent import AgentConfig, ContinuousPaperAgent
from fno_ai_paper_trading.agent.ledger import (
    CHAMPION_IDENTITY,
    sync_paper_ledger,
    to_attributed_trade,
)
from fno_ai_paper_trading.alerting.engine import AlertEngine
from fno_ai_paper_trading.config.settings import Environment, PaperSettings
from fno_ai_paper_trading.data.mock_provider import (
    InMemoryMarketDataProvider,
    build_crossing_ohlcv,
    build_sample_instruments,
)
from fno_ai_paper_trading.evaluation.paper_trades import load_paper_trades
from fno_ai_paper_trading.models.enums import OrderSide
from fno_ai_paper_trading.models.order import Fill
from fno_ai_paper_trading.portfolio.portfolio import Portfolio


def _settings() -> PaperSettings:
    return PaperSettings(
        environment=Environment.TEST,
        initial_capital=Decimal("100000"),
        max_position_quantity=75,
        max_order_notional=Decimal("250000"),
        max_daily_loss=Decimal("10000"),
        commission_rate=Decimal("0.0003"),
        commission_fixed=Decimal("0"),
        slippage_rate=Decimal("0.001"),
    )


def _round_trip_fills() -> list[Fill]:
    instrument = build_sample_instruments()[0]
    return [
        Fill(
            order_id="ORD-E",
            instrument=instrument,
            side=OrderSide.BUY,
            quantity=2,
            price=Decimal("100"),
            commission=Decimal("1"),
            filled_at=datetime(2026, 9, 8, 9, 15),
        ),
        Fill(
            order_id="ORD-X",
            instrument=instrument,
            side=OrderSide.SELL,
            quantity=2,
            price=Decimal("120"),
            commission=Decimal("2"),
            filled_at=datetime(2026, 9, 8, 9, 45),
        ),
    ]


def _trade_history(*, open_remaining: int = 0) -> list:
    portfolio = Portfolio(Decimal("100000"), long_only=True)
    for fill in _round_trip_fills():
        portfolio.apply_fill(fill)
    if open_remaining:
        buy = _round_trip_fills()[0]
        for _ in range(open_remaining):
            portfolio.apply_fill(
                Fill(
                    order_id="ORD-O",
                    instrument=buy.instrument,
                    side=OrderSide.BUY,
                    quantity=2,
                    price=Decimal("110"),
                    commission=Decimal("1"),
                    filled_at=buy.filled_at + timedelta(days=1),
                )
            )
    return portfolio.trade_history


# --------------------------------------------------------------------------
# Record contract
# --------------------------------------------------------------------------
def test_to_attributed_trade_matches_research_convention():
    entry, exit_ = _trade_history()
    record = to_attributed_trade(entry, exit_)
    # price P&L = closing fill realized P&L; commission = both legs; net = diff.
    assert record.price_pnl == Decimal("40")
    assert record.commission == Decimal("3")
    assert record.net_pnl == Decimal("37")
    assert record.entry_price == Decimal("100")
    assert record.exit_price == Decimal("120")
    assert record.entry_time == "2026-09-08T09:15:00"
    assert record.exit_time == "2026-09-08T09:45:00"
    assert record.side == "SELL"  # closing fill side
    assert record.signal == "BUY"  # the decision that opened the trade
    assert record.regime is None
    assert record.strategy_id == "moving_average_cross"
    assert record.strategy_family == "TREND_FOLLOWING"
    assert record.metadata["symbol"] == "NIFTY1"


def test_to_attributed_trade_honors_identity_override():
    entry, exit_ = _trade_history()
    record = to_attributed_trade(
        entry, exit_, {"strategy_id": "x", "strategy_family": "Y", "strategy_version": "9", "configuration_version": "z"}
    )
    assert record.strategy_id == "x"
    assert record.strategy_family == "Y"
    assert record.strategy_version == "9"
    assert record.configuration_version == "z"


# --------------------------------------------------------------------------
# Idempotent sync
# --------------------------------------------------------------------------
def test_sync_appends_new_closed_round_trip(tmp_path):
    target = tmp_path / "paper_trades.json"
    ledger = sync_paper_ledger(_trade_history(), target)
    assert len(ledger) == 1
    assert ledger[0].net_pnl == Decimal("37")
    on_disk = load_paper_trades(target)
    assert len(on_disk) == 1


def test_sync_is_idempotent(tmp_path):
    target = tmp_path / "paper_trades.json"
    first = sync_paper_ledger(_trade_history(), target)
    second = sync_paper_ledger(_trade_history(), target)
    assert len(first) == 1
    assert len(second) == 1  # no duplicate on re-sync
    assert len(load_paper_trades(target)) == 1


def test_sync_preserves_existing_records_and_appends_later_close(tmp_path):
    target = tmp_path / "paper_trades.json"
    sync_paper_ledger(_trade_history(), target)

    entry, exit_ = _trade_history()
    later_fills = [
        Fill(
            order_id="ORD-E2",
            instrument=entry.instrument,
            side=OrderSide.BUY,
            quantity=2,
            price=Decimal("90"),
            commission=Decimal("1"),
            filled_at=datetime(2026, 9, 9, 9, 15),
        ),
        Fill(
            order_id="ORD-X2",
            instrument=entry.instrument,
            side=OrderSide.SELL,
            quantity=2,
            price=Decimal("100"),
            commission=Decimal("1"),
            filled_at=datetime(2026, 9, 9, 10, 0),
        ),
    ]
    portfolio = Portfolio(Decimal("100000"), long_only=True)
    for fill in [*_round_trip_fills(), *later_fills]:
        portfolio.apply_fill(fill)

    ledger = sync_paper_ledger(portfolio.trade_history, target)
    assert len(ledger) == 2
    assert [r.exit_time for r in ledger] == [
        "2026-09-08T09:45:00",
        "2026-09-09T10:00:00",
    ]


def test_sync_leaves_open_positions_out(tmp_path):
    target = tmp_path / "paper_trades.json"
    ledger = sync_paper_ledger(_trade_history(open_remaining=1), target)
    assert len(ledger) == 1  # only the completed round trip; the open leg is not a trade
    assert ledger[0].exit_time == "2026-09-08T09:45:00"


def test_sync_missing_file_creates_ledger(tmp_path):
    ledger = sync_paper_ledger(_trade_history(), tmp_path / "paper_trades.json")
    assert ledger[0].bucket == "paper"


def test_sync_records_metadata_in_identity(tmp_path):
    target = tmp_path / "paper_trades.json"
    sync_paper_ledger(_trade_history(), target, identity=CHAMPION_IDENTITY)
    payload = target.read_text(encoding="utf-8")
    assert '"deliverable": "attributed_paper_trade_ledger"' in payload


# --------------------------------------------------------------------------
# Agent wiring
# --------------------------------------------------------------------------
def _weekday_compressed(bars):
    """Rebase the daily series onto consecutive weekdays (trading days)."""
    rebased = []
    cursor = datetime(2026, 8, 31, 9, 15)  # a Monday
    for b in bars:
        while cursor.weekday() >= 5:  # 5=Saturday, 6=Sunday
            cursor += timedelta(days=1)
        rebased.append(replace(b, timestamp=cursor))
        cursor += timedelta(days=1)
    return rebased


def _agent(tmp_path, ledger_path):
    instrument = build_sample_instruments()[0]
    bars = _weekday_compressed(build_crossing_ohlcv(instrument))
    # Latest bar is the last weekday before the 09:25 clock; the watchdog then
    # sees a fresh bar (<=11 min old).
    provider = InMemoryMarketDataProvider(
        instruments=[instrument], history={instrument.symbol: bars}
    )
    return ContinuousPaperAgent(
        AgentConfig(
            settings=_settings(),
            provider=provider,
            instrument=instrument,
            state_dir=tmp_path,
            clock=lambda: datetime(2026, 11, 13, 9, 25),
            alert_engine=AlertEngine(),
            allow_sandbox=True,
            sizer=None,  # fixed-quantity path so the small crossing entry fills
            ledger_path=ledger_path,
        ),
        run_id="AGENT_LEDGER",
    )


def test_agent_is_the_ledger_producer(tmp_path):
    ledger_path = tmp_path / "paper_trades.json"
    agent = _agent(tmp_path, ledger_path)
    res = agent.cycle()
    assert res.ran is True
    assert ledger_path.is_file()
    trades = load_paper_trades(ledger_path)
    assert len(trades) >= 1
    assert trades[0].strategy_id == "moving_average_cross"

    agent.cycle()
    assert len(load_paper_trades(ledger_path)) == len(trades)  # no duplicates


def test_agent_without_ledger_path_never_writes(tmp_path):
    agent = _agent(tmp_path, None)
    agent.cycle()
    assert not (tmp_path / "paper_trades.json").exists()
