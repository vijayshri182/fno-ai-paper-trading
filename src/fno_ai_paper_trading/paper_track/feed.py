"""Deterministic synthetic market feed for the Daily Paper Trading Track.

Used for the smoke runner and the long-run simulation phase. Bars are generated
from a seeded sum of a smooth sine wave (guaranteed MA(5,21) crossovers) plus a
tiny warp, so a session always contains realistic long/exit opportunities while
remaining byte-for-byte reproducible from a seed. Timestamps are naive NSE-IST,
one 5-minute bar per slot from 09:15 to 15:25 (75 bars/day).
"""

from __future__ import annotations

import math
import random
from datetime import date, datetime, timedelta
from decimal import Decimal

from fno_ai_paper_trading.data.market_hours import NSE_TZ, is_trading_day
from fno_ai_paper_trading.models.enums import InstrumentType
from fno_ai_paper_trading.models.instruments import Instrument
from fno_ai_paper_trading.models.market import MarketPrice

__all__ = [
    "TRACK_INSTRUMENT",
    "trading_day_sequence",
    "build_session_bars",
    "SyntheticFeed",
]

_BAR_MINUTES = 5
_BARS_PER_SESSION = 75  # 09:15..15:25 inclusive


def _time_index(index: int) -> timedelta:
    return timedelta(minutes=_BAR_MINUTES * index)


def TRACK_INSTRUMENT(symbol: str = "NIFTY 50") -> Instrument:
    """Canonical long-only index vehicle for the track (lot=1, multiplier=1)."""
    return Instrument(
        symbol=symbol,
        instrument_type=InstrumentType.INDEX,
        underlying_symbol=symbol,
        exchange="NSE",
        lot_size=1,
        multiplier=1,
    )


def trading_day_sequence(start: date, count: int) -> list[date]:
    """Next ``count`` NSE trading days from ``start`` (weekdays minus holidays)."""
    days: list[date] = []
    candidate: date = start
    while len(days) < count:
        if is_trading_day(datetime(candidate.year, candidate.month, candidate.day, 10, 0), tz=NSE_TZ):
            days.append(candidate)
        candidate = candidate + timedelta(days=1)
    return days


def build_session_bars(
    day: date,
    *,
    seed: int,
    instrument: Instrument,
    bars: int = _BARS_PER_SESSION,
    interval_minutes: int = _BAR_MINUTES,
    base_price: Decimal = Decimal("25000"),
    amplitude: Decimal = Decimal("0.01"),
) -> list[MarketPrice]:
    """Deterministic OHLCV bars for one session (naive NSE-IST)."""
    rng = random.Random(seed)
    base = Decimal(str(base_price))
    amp = Decimal(str(amplitude))
    period = 40  # bars per full cycle
    n_noise = Decimal("0.0004")
    series: list[MarketPrice] = []
    start = datetime(day.year, day.month, day.day, 9, 15, 0)
    previous_close = base

    for i in range(bars):
        price = base * (
            Decimal("1")
            + amp * Decimal(str(math.sin(2 * math.pi * i / period)))
            + n_noise * Decimal(str(rng.uniform(-1, 1)))
        )
        rounded = Decimal(round(price, 2))
        spread = base * Decimal(str(rng.uniform(0.0001, 0.0003)))
        sample_open = previous_close
        high = max(sample_open, rounded) + spread
        low = min(sample_open, rounded) - spread
        volume = 10000 + rng.randint(0, 80000)
        bar = MarketPrice(
            instrument=instrument,
            timestamp=start + _time_index(i),
            open=sample_open,
            high=high,
            low=low,
            close=rounded,
            volume=volume,
        )
        series.append(bar)
        previous_close = rounded

    return series


class SyntheticFeed:
    """A provider-shaped, seed-reproducible bar source."""

    def __init__(self, instrument: Instrument, sessions: dict[date, list[MarketPrice]]) -> None:
        self.instrument = instrument
        self.sessions = sessions
        self._flat = sorted(
            (bar for bars in sessions.values() for bar in bars),
            key=lambda bar: bar.timestamp,
        )

    @classmethod
    def build(
        cls,
        days: list[date],
        *,
        seed: int,
        instrument: Instrument | None = None,
        base_price: Decimal = Decimal("25000"),
    ) -> "SyntheticFeed":
        """Build a feed over ``days`` where every session is deterministic."""
        instrument = instrument if instrument is not None else TRACK_INSTRUMENT()
        sessions: dict[date, list[MarketPrice]] = {}
        for offset, day in enumerate(days):
            sessions[day] = build_session_bars(
                day,
                seed=seed + offset * 1000,
                instrument=instrument,
                base_price=base_price,
            )
        return cls(instrument, sessions)

    def bars_up_to(self, moment: datetime) -> list[MarketPrice]:
        """All bars whose 5-minute window is complete by ``moment``."""
        return [bar for bar in self._flat if bar.timestamp + timedelta(minutes=_BAR_MINUTES) <= moment]

    def last_close(self, moment: datetime) -> Decimal | None:
        done = self.bars_up_to(moment)
        return done[-1].close if done else None