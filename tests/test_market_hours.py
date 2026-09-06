"""Tests for NSE market-hours logic.

All tests use fixed naive datetimes interpreted in the market timezone
(Asia/Kolkata) so they are fully deterministic and need no clock or network.
"""
from __future__ import annotations

from datetime import datetime

from fno_ai_paper_trading.data.market_hours import (
    HOLIDAYS_2026,
    is_market_open,
    market_phase,
    market_session,
    next_open,
)
from fno_ai_paper_trading.models.enums import MarketPhase


def h(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 9, day, hour, minute)


class TestMarketPhase:
    def test_open_during_continuous_session(self) -> None:
        assert market_phase(h(1, 9, 15)) is MarketPhase.OPEN
        assert market_phase(h(1, 12, 30)) is MarketPhase.OPEN
        assert market_phase(h(1, 15, 30)) is MarketPhase.OPEN

    def test_pre_open_before_continuous_session(self) -> None:
        assert market_phase(h(1, 9, 0)) is MarketPhase.PRE_OPEN
        assert market_phase(h(1, 9, 14)) is MarketPhase.PRE_OPEN

    def test_closed_outside_session(self) -> None:
        assert market_phase(h(1, 8, 59)) is MarketPhase.CLOSED
        assert market_phase(h(1, 15, 31)) is MarketPhase.CLOSED
        assert market_phase(h(1, 0, 0)) is MarketPhase.CLOSED

    def test_closed_on_weekend(self) -> None:
        # 2026-09-05 is a Saturday, 2026-09-06 a Sunday.
        assert market_phase(h(5, 11, 0)) is MarketPhase.CLOSED
        assert market_phase(h(6, 11, 0)) is MarketPhase.CLOSED

    def test_closed_on_holiday(self) -> None:
        # 2026-12-25 (Friday) is a fixed-date holiday.
        holiday = datetime(2026, 12, 25, 12, 0)
        assert holiday.date() in HOLIDAYS_2026
        assert market_phase(holiday) is MarketPhase.CLOSED
        assert is_market_open(holiday) is False


class TestIsMarketOpen:
    def test_open_bool_follows_phase(self) -> None:
        assert is_market_open(h(1, 10, 0)) is True
        assert is_market_open(h(1, 15, 30)) is True
        assert is_market_open(h(1, 15, 31)) is False
        assert is_market_open(h(1, 9, 14)) is False


class TestMarketSession:
    def test_session_snapshot(self) -> None:
        session = market_session(h(1, 10, 0))
        assert session.is_open is True
        assert session.phase is MarketPhase.OPEN
        assert session.exchange == "NSE"
        assert session.label == "NSE Equity Derivatives"
        assert session.open_time == datetime(2026, 9, 1, 9, 15)
        assert session.close_time == datetime(2026, 9, 1, 15, 30)
        assert session.open_time <= session.close_time

    def test_closed_session(self) -> None:
        session = market_session(h(5, 10, 0))  # Saturday
        assert session.is_open is False
        assert session.phase is MarketPhase.CLOSED


class TestNextOpen:
    def test_next_open_before_session_on_trading_day(self) -> None:
        assert next_open(h(1, 8, 0)) == datetime(2026, 9, 1, 9, 15)

    def test_next_open_rolls_to_next_trading_day(self) -> None:
        # Friday 2026-09-04 16:00 -> Monday 2026-09-07 09:15.
        assert next_open(h(4, 16, 0)) == datetime(2026, 9, 7, 9, 15)

    def test_next_open_skips_holiday(self) -> None:
        # After Christmas 2026-12-25 (Friday) -> Monday 2026-12-28.
        assert next_open(datetime(2026, 12, 25, 16, 0)) == datetime(2026, 12, 28, 9, 15)

    def test_next_open_after_weekend(self) -> None:
        assert next_open(h(6, 10, 0)) == datetime(2026, 9, 7, 9, 15)

    def test_next_open_during_session_rolls_to_next_day(self) -> None:
        # 2026-09-01 is Tuesday; next trading day is Wednesday the 2nd.
        assert next_open(h(1, 12, 0)) == datetime(2026, 9, 2, 9, 15)