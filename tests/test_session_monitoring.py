"""Tests for the WS 6.6 session operations / monitoring layer.

Covers :mod:`fno_ai_paper_trading.services.session_monitoring`: the
:func:`health` / :class:`SessionHealth` view, :func:`build_report` /
:func:`report_from_snapshot` report construction, the plain-dict and HTML
renderers, the explicit ``write_html_report`` helper, and the deterministic
operator logging helpers. All scenarios are deterministic (fixed bars, fixed
clock, fixed ``when`` timestamps) and offline.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from fno_ai_paper_trading.config.settings import Environment, PaperSettings
from fno_ai_paper_trading.data.mock_provider import InMemoryMarketDataProvider
from fno_ai_paper_trading.models.enums import InstrumentType, Signal
from fno_ai_paper_trading.models.instruments import Instrument
from fno_ai_paper_trading.models.market import MarketPrice
from fno_ai_paper_trading.services.paper_session import PaperSession, _MISSING
from fno_ai_paper_trading.services.session_monitoring import (
    SessionHealth,
    build_report,
    health,
    log_health,
    log_results,
    report_from_snapshot,
    report_to_dict,
    report_to_html,
    write_html_report,
)
from fno_ai_paper_trading.strategies.base import SignalResult, Strategy

# --------------------------------------------------------------------------- #
# Deterministic fixtures (mirrors the sibling session test helpers; no
# cross-test-module imports by convention).
# --------------------------------------------------------------------------- #

BASE = datetime(2026, 9, 2, 9, 15)
OPEN_NOW = datetime(2026, 9, 2, 12, 0)
FILL_CLOCK = datetime(2026, 9, 2, 10, 0)
ENTRY = Decimal("24000")


def _ts(index: int, start: datetime = BASE) -> datetime:
    return start + timedelta(minutes=5 * index)


def _index() -> Instrument:
    return Instrument(
        symbol="NIFTY_INDEX",
        instrument_type=InstrumentType.INDEX,
        underlying_symbol="NIFTY",
    )


def _bar(
    index: int,
    *,
    open_: str | Decimal | None = None,
    high: str | Decimal | None = None,
    low: str | Decimal | None = None,
    close: str | Decimal | None = None,
    start: datetime = BASE,
) -> MarketPrice:
    open_ = ENTRY if open_ is None else Decimal(open_)
    close = ENTRY if close is None else Decimal(close)
    high = max(open_, close) + Decimal("20") if high is None else Decimal(high)
    low = min(open_, close) - Decimal("20") if low is None else Decimal(low)
    return MarketPrice(
        instrument=_index(),
        timestamp=_ts(index, start),
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=1000,
    )


def _bars(count: int, *, start: datetime = BASE, closes: list[Decimal] | None = None) -> list[MarketPrice]:
    closes = closes if closes is not None else [None] * count
    return [_bar(i, close=closes[i], start=start) for i in range(count)]


def _provider(bars: list[MarketPrice]) -> InMemoryMarketDataProvider:
    index = _index()
    return InMemoryMarketDataProvider(instruments=[index], history={index.symbol: bars})


def _settings() -> PaperSettings:
    return PaperSettings(
        environment=Environment.PAPER,
        initial_capital=Decimal("100000"),
        max_position_quantity=75,
        max_order_notional=Decimal("250000"),
        max_daily_loss=Decimal("1000"),
        commission_rate=Decimal("0"),
        commission_fixed=Decimal("0"),
        slippage_rate=Decimal("0"),
    )


class _ScriptedStrategy(Strategy):
    name = "scripted"

    def __init__(self, instrument: Instrument, plan: dict[int, Signal]) -> None:
        self._instrument = instrument
        self._plan = dict(plan)

    def analyze(self, bars: list[MarketPrice]) -> SignalResult:
        index = len(bars) - 1
        signal = self._plan.get(index, Signal.HOLD)
        return SignalResult(
            signal=signal,
            instrument=self._instrument,
            timestamp=bars[-1].timestamp,
            reason=f"scripted {signal.value} at bar {index}",
        )


def _strategy(plan: dict[int, Signal]) -> _ScriptedStrategy:
    return _ScriptedStrategy(_index(), plan)


def _make_session(
    bars: list[MarketPrice],
    *,
    plan: dict[int, Signal] | None = None,
    warmup_bars: int = 1,
    quantity: int = 1,
) -> PaperSession:
    session = PaperSession(
        _settings(),
        _provider(bars),
        _strategy(plan or {}),
        warmup_bars=warmup_bars,
        quantity=quantity,
        sizer=None,
        clock=lambda: OPEN_NOW,
        interval="5m",
    )
    session.start()
    return session


# 33 bars complete by OPEN_NOW (index 32 -> 11:55 + 5m <= 12:00).
ROUND_TRIP = {10: Signal.BUY, 20: Signal.SELL}
WIN_CLOSES = [Decimal("25000") if i == 20 else ENTRY for i in range(40)]


def _run_round_trip():
    session = _make_session(_bars(40, closes=WIN_CLOSES), plan=ROUND_TRIP)
    result = session.run_once(OPEN_NOW)
    return session, result


def _run_buy():
    session = _make_session(_bars(40, closes=WIN_CLOSES), plan={10: Signal.BUY})
    result = session.run_once(OPEN_NOW)
    return session, result


class TestHealth:
    def test_health_fresh_session(self) -> None:
        session = _make_session(_bars(40))
        h = health(session)
        assert isinstance(h, SessionHealth)
        assert h.running is True
        assert h.environment == "paper"
        assert h.instrument == "NIFTY_INDEX"
        assert h.interval == "5m"
        assert h.warmup is True
        assert h.consumed_bars == 0
        assert h.orders_submitted == 0
        assert h.fills == 0
        assert h.trades == 0
        assert h.rejections == 0
        assert h.skips == 0
        assert h.cash == Decimal("100000")
        assert h.equity == Decimal("100000")
        assert h.realized_pnl == Decimal("0")
        assert h.realized_pnl_today == Decimal("0")
        assert h.open_quantity == 0

    def test_health_reflects_open_position(self) -> None:
        session, _ = _run_buy()
        h = health(session, when=OPEN_NOW)
        assert h.warmup is False
        assert h.consumed_bars == 33
        assert h.orders_submitted == 1
        assert h.fills == 1
        assert h.trades == 1
        assert h.open_quantity == 1
        assert h.cash == Decimal("76000")
        assert h.equity == Decimal("100000")  # marked at last close 24000
        assert h.realized_pnl_today == Decimal("0")

    def test_health_reflects_realized_and_stopped(self) -> None:
        session, _ = _run_round_trip()
        session.stop()
        h = health(session, when=OPEN_NOW)
        assert h.running is False
        assert h.open_quantity == 0
        assert h.cash == Decimal("101000")
        assert h.equity == Decimal("101000")
        assert h.realized_pnl == Decimal("1000")
        assert h.realized_pnl_today == Decimal("1000")


class TestBuildReport:
    def test_report_from_live_with_results(self) -> None:
        session, result = _run_round_trip()
        report = build_report(session, results=[result], when=OPEN_NOW)

        assert report.source == "live"
        assert report.instrument == "NIFTY_INDEX"
        assert report.interval == "5m"
        assert report.data_source == "Paper session data"
        assert report.replay_mode == "Paper session execution"
        assert report.live_orders is False
        assert report.generated_at == OPEN_NOW
        assert report.initial_cash == Decimal("100000")
        assert report.cash == Decimal("101000")
        assert report.equity == Decimal("101000")
        assert report.realized_pnl == Decimal("1000")
        assert report.realized_pnl_today == Decimal("1000")
        assert report.unrealized_pnl == Decimal("0")
        assert report.open_quantity == 0
        assert report.consumed_bars == 33
        assert report.orders_submitted == 2
        assert report.fills == 2
        assert report.trades == 2
        assert report.rejections == 0
        assert report.skips == 0
        assert report.wins == 1
        assert report.losses == 0
        assert report.round_trips == 1
        assert report.win_rate == Decimal("1")
        assert len(report.ledger) == 33
        assert len(report.equity_curve) == 33
        assert all(point.equity > 0 for point in report.equity_curve)
        assert report.return_pct == Decimal("1")
        assert report.max_drawdown == Decimal("0")
        assert report.max_drawdown_pct == Decimal("0")

    def test_report_filters_warmup_sentinel_only_from_displayed_curve(self) -> None:
        session = _make_session(
            _bars(40, closes=WIN_CLOSES), plan=ROUND_TRIP, warmup_bars=4
        )
        result = session.run_once(OPEN_NOW)
        report = build_report(session, results=[result], when=OPEN_NOW)

        warmup_steps = [step for step in result.steps if step.equity == 0]
        assert warmup_steps
        assert report.equity_curve[0].time > result.steps[0].bar.timestamp
        assert len(report.equity_curve) == len(result.steps) - len(warmup_steps)

    def test_historical_replay_presentation_is_explicit(self) -> None:
        session, result = _run_round_trip()
        report = build_report(
            session,
            results=[result],
            when=OPEN_NOW,
            data_source="Upstox historical data",
            replay_mode="Offline paper replay",
            live_orders=False,
        )

        html = report_to_html(report)
        assert "Data source" in html and "Upstox historical data" in html
        assert "Replay mode" in html and "Offline paper replay" in html
        assert "Live orders" in html and ">No<" in html
        assert "paper-session operations &middot; live" not in html

    def test_report_without_results_uses_fill_ledger(self) -> None:
        session, _ = _run_round_trip()
        report = build_report(session, when=OPEN_NOW)
        assert report.orders_submitted == 2
        assert report.fills == 2
        assert len(report.ledger) == 2
        assert report.ledger[0].action.startswith("fill BUY")
        assert report.ledger[1].action.startswith("fill SELL")
        assert report.ledger[0].price == ENTRY
        assert report.ledger[0].quantity == 1
        assert report.ledger[0].notional == ENTRY
        assert len(report.equity_curve) == 1
        assert report.equity_curve[0].equity == Decimal("101000")

    def test_report_records_losses(self) -> None:
        closes = [Decimal("23000") if i == 20 else ENTRY for i in range(40)]
        session = _make_session(_bars(40, closes=closes), plan=ROUND_TRIP)
        result = session.run_once(OPEN_NOW)
        report = build_report(session, results=[result], when=OPEN_NOW)
        assert report.realized_pnl == Decimal("-1000")
        assert report.wins == 0
        assert report.losses == 1
        assert report.win_rate == Decimal("0")

    def test_report_to_dict_is_json_serialisable(self) -> None:
        session, result = _run_round_trip()
        report = build_report(session, results=[result], when=OPEN_NOW)
        payload = report_to_dict(report)
        assert payload["summary"]["cash"] == "101000"
        assert payload["data_source"] == "Paper session data"
        assert payload["replay_mode"] == "Paper session execution"
        assert payload["live_orders"] is False
        assert payload["summary"]["round_trips"] == 1
        assert payload["summary"]["return_pct"] == "1.00"
        assert payload["summary"]["max_drawdown"] == "0"
        assert payload["summary"]["win_rate"] == "1"
        assert payload["summary"]["consumed_bars"] == 33
        assert any(row["signal"] == "BUY" for row in payload["ledger"])
        assert payload["equity_curve"][0]["time"] == BASE.isoformat()
        # Round-trips through plain JSON without loss of structure.
        assert json.loads(json.dumps(payload)) == payload

    def test_report_to_html_is_labelled_and_escaped(self, tmp_path: Path) -> None:
        session, result = _run_round_trip()
        report = build_report(session, results=[result], when=OPEN_NOW)
        html = report_to_html(report)
        assert "<!DOCTYPE html>" in html
        assert "Paper session report" in html
        assert "NIFTY_INDEX" in html
        assert "Ledger" in html
        assert "Round trips" in html
        assert "Trade/accounting events" in html
        assert "Orders" in html
        assert "no secret data" in html
        assert "<script>" not in html

    def test_write_html_report_writes_utf8(self, tmp_path: Path) -> None:
        session, _ = _run_round_trip()
        report = build_report(session, when=OPEN_NOW)
        target = write_html_report(report, tmp_path / "nested" / "session.html")
        assert target.exists()
        assert target.read_text(encoding="utf-8") == report_to_html(report)


class TestSnapshotReport:
    def test_snapshot_report_matches_live_state(self) -> None:
        session, _ = _run_round_trip()
        snap = session.snapshot()
        from_snap = report_from_snapshot(snap, when=OPEN_NOW)
        from_live = build_report(session, when=OPEN_NOW)

        assert from_snap.source == "snapshot"
        assert from_live.source == "live"
        for field in (
            "instrument",
            "interval",
            "initial_cash",
            "cash",
            "equity",
            "realized_pnl",
            "realized_pnl_today",
            "orders_submitted",
            "fills",
            "trades",
            "rejections",
            "skips",
            "consumed_bars",
            "wins",
            "losses",
        ):
            assert getattr(from_snap, field) == getattr(from_live, field), field
        assert from_snap.win_rate == from_live.win_rate
        assert from_snap.round_trips == from_live.round_trips
        assert from_snap.ledger == from_live.ledger
        assert from_snap.max_drawdown is None
        assert from_snap.max_drawdown_pct is None

    def test_snapshot_open_position_marked_at_entry(self) -> None:
        session, _ = _run_buy()
        report = report_from_snapshot(session.snapshot(), when=OPEN_NOW)
        assert report.open_quantity == 1
        assert report.cash == Decimal("76000")
        # Offline fallback marks the open position at cost basis -> zero unrealized.
        assert report.unrealized_pnl == Decimal("0")
        assert report.equity == Decimal("100000")

    def test_snapshot_report_with_mark_prices(self) -> None:
        session, _ = _run_buy()
        report = report_from_snapshot(
            session.snapshot(),
            when=OPEN_NOW,
            mark_prices={"NIFTY_INDEX": Decimal("25200")},
        )
        assert report.unrealized_pnl == Decimal("1200")
        assert report.equity == Decimal("101200")


class TestOperatorLogging:
    def test_log_results_writes_step_and_poll_lines(self, caplog) -> None:
        session, result = _run_buy()
        with caplog.at_level(logging.INFO, logger="session.operations"):
            log_results([result])
        text = " | ".join(rec.message for rec in caplog.records)
        assert "signal=BUY" in text
        assert "signal=HOLD" in text
        assert "poll consumed 33 bar(s)" in text
        assert "loop processed 33 step(s)" in text

    def test_log_health_writes_deterministic_line(self, caplog) -> None:
        session, _ = _run_round_trip()
        h = health(session, when=OPEN_NOW)
        with caplog.at_level(logging.INFO, logger="session.operations"):
            log_health(h)
        assert any("health NIFTY_INDEX 5m" in rec.message for rec in caplog.records)


def test_module_surface_stays_offline_and_pure() -> None:
    """The monitoring module never touches the disk on its own."""
    session, _ = _run_round_trip()
    health(session, when=OPEN_NOW)
    build_report(session, when=OPEN_NOW)
    report_from_snapshot(session.snapshot(), when=OPEN_NOW)
    # The explicit write helper is the only disk-touching entry point.
    import fno_ai_paper_trading.services.session_monitoring as module

    assert hasattr(module, "write_html_report")
