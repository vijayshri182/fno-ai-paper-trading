"""Session operating policy for the Daily Paper Trading Track (NSE IST).

The track is a 5-minute, single-position, long-only simulation that must always
end a trading day flat:

* ``window_open``   09:15 IST  — continuous trading starts
* ``flatten_time``  15:20 IST  — entries are refused; any open position is
  force-closed on the next completed/available candle close (protective exit)
* ``window_close``  15:30 IST  — after this the gate is ``CLOSING``: only a
  protective flatten may run so the track can never sleep overnight with risk

Gates (see :meth:`SessionPolicy.gate`):

* ``SKIP``        — weekend / holiday / pre-open; no data fetch, no orders
* ``TRADING``     — entries, exits and stops allowed
* ``FLATTENING``  — entries refused; exits, stops and the EOD flatten allowed
* ``CLOSING``     — after 15:30 (or a day the market is closed): only the
  protective flatten may still fire, and only if a position is actually open

Safety contract honoured by the engine:

* no *entry* order is ever created without ``RiskManager`` pre-trade approval;
* every *exit* (signal SELL, stop-loss, EOD flatten) is a protective exit that
  always stays executable — this mirrors the repository's existing stop-loss
  executor rules (a protective exit must survive the daily-loss limit);
* no order is allowed to open or increase a short position (the portfolio is
  long-only); a full close of an existing long can never produce a short;
* a flatten never fires on a day whose position was not opened on this account
  and day (stale-position protection lives in the engine's restore path).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import Enum

from fno_ai_paper_trading.data.market_hours import NSE_TZ, market_phase, is_trading_day
from fno_ai_paper_trading.models.enums import MarketPhase

__all__ = [
    "Gate",
    "GateDecision",
    "SessionPolicy",
    "TRACK_SESSION",
]


class Gate(str, Enum):
    """What the engine is allowed to do right now."""

    SKIP = "SKIP"  # outside the trading day / pre-open: nothing happens
    TRADING = "TRADING"  # entries allowed (09:15 <= t < flatten_time)
    FLATTENING = "FLATTENING"  # flatten_time <= t < window_close: exits only
    CLOSING = "CLOSING"  # t >= window_close or closed market day: flatten only


@dataclass(frozen=True)
class GateDecision:
    """The policy verdict for one clock tick."""

    gate: Gate
    reason: str
    phase: MarketPhase

    @property
    def allows_entries(self) -> bool:
        return self.gate is Gate.TRADING

    @property
    def allows_exits(self) -> bool:
        return self.gate in (Gate.TRADING, Gate.FLATTENING, Gate.CLOSING)

    @property
    def allows_flatten(self) -> bool:
        return self.gate in (Gate.FLATTENING, Gate.CLOSING)


@dataclass(frozen=True)
class SessionPolicy:
    """Fixed intraday window plus flatten/close boundaries (naive NSE IST times)."""

    window_open: time = time(9, 15)
    flatten_time: time = time(15, 20)
    window_close: time = time(15, 30)

    def __post_init__(self) -> None:
        if not (self.window_open < self.flatten_time < self.window_close):
            raise ValueError("expected window_open < flatten_time < window_close")

    def day_bounds(self, day: date) -> tuple[datetime, datetime]:
        """Naive IST ``(open, close)`` datetimes for ``day``."""
        return (
            datetime.combine(day, self.window_open),
            datetime.combine(day, self.window_close),
        )

    def gate(self, moment: datetime) -> GateDecision:
        """Decide the permitted gate for an IST clock moment."""
        phase = market_phase(moment, tz=NSE_TZ)
        if phase is MarketPhase.PRE_OPEN:
            return GateDecision(Gate.SKIP, f"pre-open phase ({self.window_open:%H:%M} IST)", phase)
        if phase is MarketPhase.OPEN:
            t = moment.time().replace(second=0, microsecond=0)
            if t < self.flatten_time:
                return GateDecision(
                    Gate.TRADING,
                    f"continuous trading before {self.flatten_time:%H:%M} IST",
                    phase,
                )
            return GateDecision(
                Gate.FLATTENING,
                f"flatten window {self.flatten_time:%H:%M}-{self.window_close:%H:%M} IST",
                phase,
            )
        # CLOSED: only the protective flatten may still run on the same trading
        # day once the close has passed; anything else (weekend/holiday) is SKIP.
        if phase is MarketPhase.CLOSED and is_trading_day(moment, tz=NSE_TZ):
            return GateDecision(
                Gate.CLOSING,
                f"after {self.window_close:%H:%M} IST: protective flatten only",
                phase,
            )
        return GateDecision(Gate.SKIP, "outside trading day (weekend/holiday)", phase)

    def describe(self) -> dict:
        return {
            "window_open": self.window_open.isoformat(),
            "flatten_time": self.flatten_time.isoformat(),
            "window_close": self.window_close.isoformat(),
        }


TRACK_SESSION = SessionPolicy()
_TRACK_POLL = timedelta(minutes=1)


def next_poll_tick(current: datetime) -> datetime:
    """Next one-minute poll instant after ``current`` (used by smoke runners)."""
    return current.replace(second=0, microsecond=0) + _TRACK_POLL