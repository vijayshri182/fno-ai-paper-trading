"""Shared helpers for the Daily Paper Trading Track test suite.

These helpers are test-only glue: deterministic synthetic feeds, minimal-tick
drivers and a plan-based strategy for controlling exactly when BUY/SELL/flatten
happen (used by crash-restart and boundary tests).
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from pathlib import Path

from fno_ai_paper_trading.models.enums import Signal
from fno_ai_paper_trading.models.instruments import Instrument
from fno_ai_paper_trading.paper_track.clock import FixedClock
from fno_ai_paper_trading.paper_track.engine import TrackConfig, TrackEngine
from fno_ai_paper_trading.paper_track.feed import SyntheticFeed, trading_day_sequence, TRACK_INSTRUMENT
from fno_ai_paper_trading.paper_track.store import TrackStore
from fno_ai_paper_trading.strategies.base import SignalResult, Strategy

DAY0 = date(2026, 9, 21)  # a Monday


def instrument() -> Instrument:
    return TRACK_INSTRUMENT("NIFTY 50")


def feed_for(days: list[date], seed: int = 20260921, base: int = 25000) -> SyntheticFeed:
    return SyntheticFeed.build(days, seed=seed, instrument=instrument(), base_price=str(base))


def closes_feed(closes: list, day: date = DAY0, base=25000) -> SyntheticFeed:
    """A hand-built single-session feed whose bar closes follow ``closes``."""
    from decimal import Decimal

    from fno_ai_paper_trading.models.market import MarketPrice

    bars = []
    prev = Decimal(str(base))
    start = datetime(day.year, day.month, day.day, 9, 15, 0)
    for i, c in enumerate(closes):
        close = Decimal(str(c))
        high = max(prev, close) * Decimal("1.0005")
        low = min(prev, close) * Decimal("0.9995")
        bars.append(
            MarketPrice(
                instrument=instrument(),
                timestamp=start + timedelta(minutes=5 * i),
                open=prev,
                high=high,
                low=low,
                close=close,
                volume=1000,
            )
        )
        prev = close
    return SyntheticFeed(instrument(), {day: bars})


def day_ticks(day: date) -> list[datetime]:
    start = datetime(day.year, day.month, day.day, 9, 15, 0)
    ticks = [
        datetime(day.year, day.month, day.day, 9, 14, 0),
        datetime(day.year, day.month, day.day, 9, 15, 0),
    ]
    ticks += [start + timedelta(minutes=5 * (k + 1)) for k in range(75)]
    ticks += [datetime(day.year, day.month, day.day, 15, 36, 0)]
    return ticks


def full_ticks(days: list[date]) -> list[datetime]:
    out: list[datetime] = []
    for day in days:
        out.extend(day_ticks(day))
    return out


def make_engine(
    store_dir: Path,
    *,
    account: str = "tracktest",
    days: list[date] | None = None,
    seed: int = 20260921,
    strategy: Strategy | None = None,
    failpoints: set[str] | None = None,
    run_id: str | None = None,
    base: int = 25000,
) -> tuple[TrackEngine, TrackStore, SyntheticFeed]:
    days = days or [DAY0]
    config = TrackConfig(account=account, store_dir=store_dir, strategy=strategy)
    store = TrackStore(store_dir, account)
    feed = feed_for(days, seed=seed, base=base)
    engine = TrackEngine(
        config, store=store, clock=FixedClock(datetime(1970, 1, 1)), bars_source=feed,
        failpoints=failpoints, run_id=run_id,
    )
    return engine, store, feed


def drive(engine: TrackEngine, ticks: list[datetime]) -> list:
    """Step ``engine`` at each tick; returns the list of step results."""
    results = []
    for tick in ticks:
        engine.clock.set(tick)
        results.append(engine.step())
    engine.save_report_payload()
    return results


def run_simulated_day(
    store_dir: Path,
    *,
    account: str = "simday",
    strategy: Strategy | None = None,
    seed: int = 20260921,
    failpoints: set[str] | None = None,
) -> TrackEngine:
    engine, store, feed = make_engine(
        store_dir, account=account, days=[DAY0], seed=seed, strategy=strategy, failpoints=failpoints
    )
    drive(engine, day_ticks(DAY0))
    return engine


class PlanStrategy(Strategy):
    """Deterministic, stateless strategy: BUY at ``buy_at``, SELL at ``sell_at``.

    The TIMES are bar open times (naive IST); each bar is analysed exactly once
    at its completion, so equality triggers exactly on the intended bar.
    Anything else HOLDs (a stop-loss may still fire, as in production).
    """

    name = "plan"

    def __init__(self, buy_at: time | None = None, sell_at: time | None = None) -> None:
        self.buy_at = buy_at
        self.sell_at = sell_at

    def analyze(self, bars: list) -> SignalResult:
        last = bars[-1]
        moment = last.timestamp.time()
        if self.buy_at is not None and moment == self.buy_at:
            return SignalResult(Signal.BUY, instrument=last.instrument, timestamp=last.timestamp, reason="plan buy")
        if self.sell_at is not None and moment == self.sell_at:
            return SignalResult(Signal.SELL, instrument=last.instrument, timestamp=last.timestamp, reason="plan sell")
        return SignalResult(Signal.HOLD, instrument=last.instrument, timestamp=last.timestamp, reason="plan hold")