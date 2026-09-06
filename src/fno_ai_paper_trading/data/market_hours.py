"""NSE derivatives market hours and session state.

The NSE F&O session runs Monday to Friday in India Standard Time (UTC+05:30):

* 09:00–09:15 — pre-open (order entry / call auction)
* 09:15–15:30 — continuous trading

India does not observe daylight saving time, so a fixed UTC+05:30 offset is
used instead of an IANA timezone database (which Windows may not ship). All
public helpers accept either naive datetimes (interpreted in the market
timezone) or timezone-aware datetimes, and return naive datetimes expressed in
the market timezone — consistent with the rest of the domain models.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

from fno_ai_paper_trading.models.enums import MarketPhase
from fno_ai_paper_trading.models.market import MarketSession

NSE_TZ = timezone(timedelta(hours=5, minutes=30))

PRE_OPEN_TIME = time(9, 0)
OPEN_TIME = time(9, 15)
CLOSE_TIME = time(15, 30)

# Fixed-date national holidays. Market-specific holidays (Holi, Diwali, ...)
# change every year and must be verified against the official NSE calendar
# before being added. This list is best-effort and advisory only.
HOLIDAYS_2026: frozenset[date] = frozenset({
    date(2026, 1, 26),  # Republic Day (Monday)
    date(2026, 4, 3),  # Good Friday
    date(2026, 12, 25),  # Christmas (Friday)
})

# A weekday is a trading day unless it is a holiday.
TRADING_DAYS = range(0, 5)  # Monday..Friday


def _aware(dt: datetime, tz) -> datetime:
    """Interpret ``dt`` in ``tz`` if naive, otherwise convert it to ``tz``."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=tz)
    return dt.astimezone(tz)


def is_trading_day(dt: datetime, *, tz=NSE_TZ, holidays=HOLIDAYS_2026) -> bool:
    """True when ``dt`` falls on a weekday that is not in ``holidays``."""
    local = _aware(dt, tz).astimezone(tz)
    return local.weekday() in TRADING_DAYS and local.date() not in holidays


def market_phase(dt: datetime, *, tz=NSE_TZ, holidays=HOLIDAYS_2026) -> MarketPhase:
    """Return the :class:`MarketPhase` for ``dt`` (useful for strategy gating)."""
    if not is_trading_day(dt, tz=tz, holidays=holidays):
        return MarketPhase.CLOSED
    local = _aware(dt, tz).astimezone(tz)
    clock = local.time().replace(second=0, microsecond=0)
    if PRE_OPEN_TIME <= clock < OPEN_TIME:
        return MarketPhase.PRE_OPEN
    if OPEN_TIME <= clock <= CLOSE_TIME:
        return MarketPhase.OPEN
    return MarketPhase.CLOSED


def is_market_open(dt: datetime, *, tz=NSE_TZ, holidays=HOLIDAYS_2026) -> bool:
    """True when continuous trading is active at ``dt``."""
    return market_phase(dt, tz=tz, holidays=holidays) is MarketPhase.OPEN


def market_session(
    dt: datetime, *, tz=NSE_TZ, holidays=HOLIDAYS_2026, label: str = "NSE Equity Derivatives"
) -> MarketSession:
    """Build the normalized :class:`MarketSession` snapshot for ``dt``."""
    local = _aware(dt, tz).astimezone(tz)
    phase = market_phase(local, tz=tz, holidays=holidays)
    day = local.date()
    open_time = datetime(day.year, day.month, day.day, OPEN_TIME.hour, OPEN_TIME.minute)
    close_time = datetime(day.year, day.month, day.day, CLOSE_TIME.hour, CLOSE_TIME.minute)
    return MarketSession(
        is_open=phase is MarketPhase.OPEN,
        phase=phase,
        observed_at=local.replace(tzinfo=None),
        open_time=open_time,
        close_time=close_time,
        exchange="NSE",
        label=label,
    )


def next_open(dt: datetime, *, tz=NSE_TZ, holidays=HOLIDAYS_2026) -> datetime:
    """Next upcoming 09:15 IST open (naive IST datetime) strictly after ``dt``."""
    local = _aware(dt, tz).astimezone(tz)
    candidate = local.replace(hour=OPEN_TIME.hour, minute=OPEN_TIME.minute, second=0, microsecond=0)
    if (
        local.time().replace(second=0, microsecond=0) < OPEN_TIME
        and is_trading_day(candidate, tz=tz, holidays=holidays)
    ):
        return candidate.replace(tzinfo=None)

    day = local.date()
    while True:
        day += timedelta(days=1)
        candidate = datetime(day.year, day.month, day.day, OPEN_TIME.hour, OPEN_TIME.minute)
        if is_trading_day(candidate, tz=tz, holidays=holidays):
            return candidate