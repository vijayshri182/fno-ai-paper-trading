"""Deterministic NIFTY 50 cash-market session gate for the scheduled collector.

The NIFTY cash session is Monday-Friday ``[09:15, 15:30)`` IST
(``Asia/Kolkata``). 09:15:00 is OPEN, 15:29:59 is OPEN, 15:30:00 is CLOSED;
Saturday and Sunday are always CLOSED.

This is ONLY a scheduling/acquisition efficiency guard used by the scheduled
path (``scheduler``): it never changes what the collector stores, never makes a
network request, and never replaces the collector's boundary/integrity rules.

Timezone handling is deterministic and never consults the machine's local
timezone:

* timezone-aware instants are converted into ``Asia/Kolkata`` explicitly;
* naive instants are interpreted AS ``Asia/Kolkata`` (never as local time);
* ``market_now()`` returns an aware ``Asia/Kolkata`` timestamp.

NSE holidays are intentionally NOT modelled here: weekends are the only known
closed days. Full holiday awareness belongs to a trusted NSE calendar, which
this repository does not (yet) ship -- the ``data/market_hours.HOLIDAYS_2026``
list is explicitly best-effort/advisory and is not reused by this gate.

``zoneinfo.ZoneInfo("Asia/Kolkata")`` is used when the platform ships the IANA
tz database (or the ``tzdata`` package). Windows typically does neither, so the
module falls back to the repository's documented fixed ``UTC+05:30`` offset
(``data/market_hours.py``): India observes no daylight saving time, therefore
``Asia/Kolkata`` and ``UTC+05:30`` are identical for every practical date and
the gate remains fully deterministic on Windows.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fno_ai_paper_trading.fresh_oos.protocol import (
    SESSION_END_EXCLUSIVE,
    SESSION_FIRST_TIME,
)

REASON_OPEN = "OPEN"
REASON_OUTSIDE_MARKET_HOURS = "OUTSIDE_NIFTY_MARKET_HOURS"
REASON_WEEKEND = "WEEKEND"

_WEEKEND_DAYS = frozenset({5, 6})  # Saturday, Sunday (datetime.weekday())


def _resolve_kolkata_tz() -> tzinfo:
    try:
        return ZoneInfo("Asia/Kolkata")
    except ZoneInfoNotFoundError:  # Windows: no IANA db / no tzdata package
        return timezone(timedelta(hours=5, minutes=30))  # IST == UTC+05:30 (no DST)


MARKET_TZ = _resolve_kolkata_tz()


def market_now() -> datetime:
    """Current time as an aware ``Asia/Kolkata`` datetime (no local-TZ dependency)."""
    return datetime.now(MARKET_TZ)


@dataclass(frozen=True)
class SessionResult:
    """Deterministic market-session verdict for a single instant.

    ``reason`` is machine-readable (``REASON_OPEN``/``REASON_OUTSIDE_MARKET_HOURS``/
    ``REASON_WEEKEND``); ``observed_at`` is the instant expressed in IST.
    """

    is_open: bool
    reason: str
    observed_at: datetime

    @property
    def phase(self) -> str:
        return "OPEN" if self.is_open else "CLOSED"

    def __str__(self) -> str:
        return self.reason


def evaluate_session(now: datetime | None = None) -> SessionResult:
    """Return the session verdict for ``now`` (or the current IST time).

    timezone-aware instants are converted to ``Asia/Kolkata``; naive instants
    are interpreted as already being ``Asia/Kolkata``. Comparison uses the
    ``[09:15, 15:30)`` window, i.e. 09:15:00 OPEN .. 15:29:59 OPEN and
    15:30:00 CLOSED, with Saturday/Sunday always CLOSED. No holidays modelled.
    """
    stamp = market_now() if now is None else now
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=MARKET_TZ)
    else:
        stamp = stamp.astimezone(MARKET_TZ)
    if stamp.weekday() in _WEEKEND_DAYS:
        return SessionResult(is_open=False, reason=REASON_WEEKEND, observed_at=stamp)
    instant = stamp.time().replace(microsecond=0)
    if SESSION_FIRST_TIME <= instant < SESSION_END_EXCLUSIVE:
        return SessionResult(is_open=True, reason=REASON_OPEN, observed_at=stamp)
    return SessionResult(
        is_open=False, reason=REASON_OUTSIDE_MARKET_HOURS, observed_at=stamp
    )