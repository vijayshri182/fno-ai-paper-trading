"""Injectable clock for the Daily Paper Trading Track.

All timestamps in the track are naive datetimes expressed in NSE market time
(UTC+05:30), consistent with ``data/market_hours.py``. The engine never reads
``datetime.now`` directly: it asks its :class:`Clock` so tests can freeze or
advance time deterministically.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Protocol, runtime_checkable

from fno_ai_paper_trading.data.market_hours import NSE_TZ

__all__ = ["Clock", "SystemClock", "FixedClock"]


@runtime_checkable
class Clock(Protocol):
    """Anything exposing ``now() -> naive NSE-IST datetime``."""

    def now(self) -> datetime: ...


class SystemClock:
    """Real wall clock, expressed as naive NSE-IST time (UTC+05:30)."""

    def now(self) -> datetime:
        return datetime.now(NSE_TZ).replace(tzinfo=None)


class FixedClock:
    """Deterministic clock for tests and simulations.

    ``advance`` moves the frozen instant forward so a single fixed clock can
    drive a whole simulated day/session without rebinding.
    """

    def __init__(self, value: datetime) -> None:
        self._value = value

    def now(self) -> datetime:
        return self._value

    def set(self, value: datetime) -> None:
        self._value = value

    def advance(self, delta: timedelta) -> datetime:
        self._value = self._value + delta
        return self._value