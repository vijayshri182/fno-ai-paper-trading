"""Fixed, human-approved semantics of the 15-minute directional experiment.

Everything that makes the experiment *deterministic* lives here: the strict
decision cadence, the signal/leg/action enums, the approved transition table,
the direction-aware protective stop, and the label-only option convention.

Time model (naive IST, consistent with ``paper_track``):

* 5-minute candles open at ``09:15`` and every 5 minutes after (75 per day,
  last open ``15:25``). A candle is *completed* at ``timestamp + 5 minutes``.
* A 15-minute window is exactly three consecutive completed 5-minute candles.
  Windows complete at ``09:30 + 15*k`` for ``k = 0..23`` (last *decision*
  moment ``15:15``). The final triple (``15:15/15:20/15:25``) completes at
  ``15:30``, after the ``15:20`` EOD flatten, so it is never a decision
  window. There are exactly 24 decision moments per trading day.
* Decisions (signal evaluation, protective stops, position switches, EOD
  flatten) happen **only** at those 15-minute points. Never on an incomplete
  window; never from a duplicate or out-of-order candle.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from enum import Enum

from fno_ai_paper_trading.models.market import MarketPrice

__all__ = [
    "EXPERIMENT_ID",
    "BAR_MINUTES",
    "DECISION_MINUTES",
    "WINDOW_BARS",
    "DECISION_START",
    "DECISION_STOP",
    "DONCHIAN_ENTRY_CHANNEL",
    "DONCHIAN_EXIT_CHANNEL",
    "Signal15m",
    "Leg",
    "Action",
    "PositionDecision",
    "decide",
    "signal_to_15m",
    "decision_moments",
    "DirectionalStopPolicy",
    "StopCheck",
]

EXPERIMENT_ID = "15M_DIRECTIONAL_OPTIONS_EXPERIMENT"

BAR_MINUTES = 5
DECISION_MINUTES = 15
WINDOW_BARS = 3

# First decision at 09:30 (window 09:15/09:20/09:25 completes), last at 15:15.
DECISION_START = time(9, 30)
DECISION_STOP = time(15, 15)

# Frozen signal parameters of the approved signal source.
DONCHIAN_ENTRY_CHANNEL = 20
DONCHIAN_EXIT_CHANNEL = 10


class Signal15m(str, Enum):
    """The 15-minute directional intent used by the position state machine."""

    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"


class Leg(str, Enum):
    """The open (or target) leg. Label-only on the index, never a priced option."""

    FLAT = "FLAT"
    CALL = "CALL"
    PUT = "PUT"


class Action(str, Enum):
    """Position actions executable at a 15-minute decision point."""

    NO_ACTION = "NO_ACTION"
    ENTER_CALL = "ENTER_CALL"
    ENTER_PUT = "ENTER_PUT"
    EXIT_CALL = "EXIT_CALL"
    EXIT_PUT = "EXIT_PUT"
    SWITCH_CALL_TO_PUT = "SWITCH_CALL_TO_PUT"
    SWITCH_PUT_TO_CALL = "SWITCH_PUT_TO_CALL"


def decision_moments(day: date) -> list[datetime]:
    """All 24 decision instants for ``day``: 09:30, 09:45, ..., 15:15."""
    out: list[datetime] = []
    current = datetime.combine(day, DECISION_START)
    stop = datetime.combine(day, DECISION_STOP)
    while current <= stop:
        out.append(current)
        current += timedelta(minutes=DECISION_MINUTES)
    return out


def signal_to_15m(signal: str) -> Signal15m:
    """Map the deterministic BUY/SELL/HOLD stream to the 15-minute intent."""
    from fno_ai_paper_trading.models.enums import Signal

    if signal == Signal.BUY.value:
        return Signal15m.BULLISH
    if signal == Signal.SELL.value:
        return Signal15m.BEARISH
    return Signal15m.NEUTRAL


@dataclass(frozen=True)
class PositionDecision:
    """One 15-minute point: the actions dictated from a (state, signal) pair."""

    state: Leg
    signal: Signal15m
    actions: tuple[Action, ...]
    reason: str


def decide(state: Leg, signal: Signal15m) -> PositionDecision:
    """The approved transition table. Decisions change the leg **only** at a
    15-minute window; nothing here can act mid-window.
    """
    table: dict[tuple[Leg, Signal15m], tuple[Action, ...]] = {
        # Flat: directional intents enter a leg; NEUTRAL keeps us flat.
        (Leg.FLAT, Signal15m.BULLISH): (Action.ENTER_CALL,),
        (Leg.FLAT, Signal15m.BEARISH): (Action.ENTER_PUT,),
        (Leg.FLAT, Signal15m.NEUTRAL): (),
        # Same-direction hold (no action) while the leg is already open.
        (Leg.CALL, Signal15m.BULLISH): (),
        (Leg.PUT, Signal15m.BEARISH): (),
        # Approved neutral rule: NEUTRAL closes the open leg back to FLAT.
        (Leg.CALL, Signal15m.NEUTRAL): (Action.EXIT_CALL,),
        (Leg.PUT, Signal15m.NEUTRAL): (Action.EXIT_PUT,),
        # Opposite signal: switch legs (close + enter) at the decision point.
        (Leg.CALL, Signal15m.BEARISH): (Action.SWITCH_CALL_TO_PUT,),
        (Leg.PUT, Signal15m.BULLISH): (Action.SWITCH_PUT_TO_CALL,),
    }
    actions = table[(state, signal)]
    reasons = {
        (Leg.FLAT, Signal15m.BULLISH): "open CALL (long index)",
        (Leg.FLAT, Signal15m.BEARISH): "open PUT (short index)",
        (Leg.FLAT, Signal15m.NEUTRAL): "stay FLAT",
        (Leg.CALL, Signal15m.BULLISH): "hold CALL",
        (Leg.PUT, Signal15m.BEARISH): "hold PUT",
        (Leg.CALL, Signal15m.NEUTRAL): "close CALL to FLAT",
        (Leg.PUT, Signal15m.NEUTRAL): "close PUT to FLAT",
        (Leg.CALL, Signal15m.BEARISH): "switch CALL to PUT",
        (Leg.PUT, Signal15m.BULLISH): "switch PUT to CALL",
    }
    return PositionDecision(state=state, signal=signal, actions=tuple(actions), reason=reasons[(state, signal)])


@dataclass(frozen=True)
class StopCheck:
    """Direction-aware protective exit check at a decision moment."""

    leg: Leg
    entry_price: Decimal
    stop_level: Decimal
    triggered: bool

    @property
    def reference(self) -> Decimal:
        return self.stop_level


class DirectionalStopPolicy:
    """Fixed-percentage protective stop, direction-aware.

    The committed ``risk/stop_loss.py::StopLossPolicy`` is intentionally
    LONG-ONLY (flat/short positions are ignored). That semantics cannot
    express a labelled PUT (short index) leg, so the mandate's ``§9``
    "isolate and document" branch applies: this policy mirrors the same
    fixed 2% distance but anchors it *above* entry for a PUT:

    * ``CALL`` (long): triggered when the reference close ``<= entry*(1-pct)``
    * ``PUT`` (short): triggered when the reference close ``>= entry*(1+pct)``

    ``stop_loss_pct`` and the 0.02 default are copied from the committed
    defaults (``SizerConfig``/``StopLossPolicy``) so no competing system is
    introduced; only the short-side anchor is option-specific.
    """

    def __init__(self, stop_loss_pct: Decimal = Decimal("0.02")) -> None:
        pct = Decimal(str(stop_loss_pct))
        if not pct > 0 or pct >= 1:
            raise ValueError("stop_loss_pct must be in (0, 1)")
        self.stop_loss_pct = pct

    def check(self, *, leg: Leg, entry_price: Decimal, bar: MarketPrice) -> StopCheck:
        entry = Decimal(str(entry_price))
        if leg == Leg.CALL:
            level = entry * (Decimal("1") - self.stop_loss_pct)
            triggered = bar.close <= level
        elif leg == Leg.PUT:
            level = entry * (Decimal("1") + self.stop_loss_pct)
            triggered = bar.close >= level
        else:
            raise ValueError(f"stop check is undefined for a {leg.value} leg")
        return StopCheck(leg=leg, entry_price=entry, stop_level=level, triggered=triggered)