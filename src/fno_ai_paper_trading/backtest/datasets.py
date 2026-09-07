"""Deterministic historical datasets for backtesting and tests.

Every series is hand-verified against the moving-average crossover strategy
(default ``fast=2, slow=3``) so expected signals/fills can be asserted exactly.
These are the only datasets the offline backtest demo and the test suite use —
no live market data is ever required.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from fno_ai_paper_trading.models.instruments import Instrument
from fno_ai_paper_trading.models.market import MarketPrice
from fno_ai_paper_trading.utils.functions import non_negative_int


def _closes_to_bars(instrument: Instrument, closes: list, start: datetime = datetime(2026, 1, 5, 9, 15)) -> list:
    """Convert a list of closing prices into OHLCV bars (daily, oldest first)."""
    bars: list[MarketPrice] = []
    for i, close in enumerate(closes):
        bars.append(
            MarketPrice(
                instrument=instrument,
                timestamp=start + timedelta(days=i),
                open=close - Decimal("1"),
                high=close + Decimal("2"),
                low=close - Decimal("2"),
                close=Decimal(close),
                volume=1000 + i * 100,
            )
        )
    return bars


# Public alias so other packages (e.g. research regimes) build deterministic bars.
closes_to_bars = _closes_to_bars


def build_profitable_series(instrument: Instrument) -> list:
    """Rises after warm-up, tops out, then retreats.

    Hand-verified with ``MovingAverageCrossStrategy(fast=2, slow=3)``:
    BUY at bar 5 (close 110), SELL at bar 10 (close 120) — one profitable round
    trip of +10 per unit before costs.
    """
    closes = [100, 100, 100, 100, 100, 110, 120, 130, 140, 130, 120, 110, 100]
    return _closes_to_bars(instrument, closes)


def build_losing_series(instrument: Instrument) -> list:
    """Brief spike up, then collapse.

    Hand-verified: BUY at bar 5 (close 110), SELL at bar 8 (close 100) — one
    losing round trip of -10 per unit before costs.
    """
    closes = [100, 100, 100, 100, 100, 110, 150, 100, 100, 100, 100]
    return _closes_to_bars(instrument, closes)


def build_drawdown_series(instrument: Instrument) -> list:
    """Rise then persistent fall while in a position.

    Hand-verified: BUY at bar 5 (close 110), SELL at bar 7 (close 90). The
    equity peaks at bar 6 (close 120) and drops hard afterwards — used to make
    drawdown non-trivial.
    """
    closes = [100, 100, 100, 100, 100, 110, 120, 90, 88, 86, 84, 82, 80]
    return _closes_to_bars(instrument, closes)


def build_multiple_trades_series(instrument: Instrument) -> list:
    """Two complete up/down cycles so two round trips occur.

    Hand-verified: BUY@110 (bar 5) -> SELL@120 (bar 10), then after a reset the
    price climbs again: BUY@105 (bar 18) -> SELL@140 (bar 23).
    """
    closes = [
        100, 100, 100, 100, 100, 110, 120, 130, 140, 130, 120, 110, 100,
        95, 95, 95, 95, 95, 105, 130, 150, 160, 150, 140, 130,
    ]
    return _closes_to_bars(instrument, closes)


def build_no_trade_series(instrument: Instrument, bars: int = 12) -> list:
    """A constant close — the fast and slow averages never strictly cross."""
    non_negative_int(bars, "bars")
    return _closes_to_bars(instrument, [100] * bars)


def build_short_profit_series(instrument: Instrument) -> list:
    """Declining market triggers a SELL-to-open, then a rebound BUY closes it.

    Hand-verified: SELL@140 (bar 5), BUY@130 (bar 10) — a profitable short of
    +10 per unit before costs.
    """
    closes = [150, 150, 150, 150, 150, 140, 130, 120, 110, 120, 130, 140, 150]
    return _closes_to_bars(instrument, closes)