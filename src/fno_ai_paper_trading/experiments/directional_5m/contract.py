"""Fixed, human-approved semantics of the 5-minute directional experiment.

This is a *separate* experiment (``5M_DIRECTIONAL_OPTIONS_EXPERIMENT``) that
reuses the approved transition machinery of the 15-minute directional
experiment read-only. Nothing in this module modifies the 15M experiment, the
frozen MA(5,21) baseline, the ``DonchianBreakout(20, 10)`` contract, or any
risk control.

Time model (naive IST, consistent with ``paper_track``):

* 5-minute candles open at ``09:15`` and every 5 minutes after (75 per day,
  last open ``15:25``). A candle is *completed* at ``timestamp + 5 minutes``.
* Unlike the 15M experiment, there is **no 3-candle window**: a decision is
  taken after **every completed 5-minute candle**. The decision instants are
  exactly the bar-completion grid points ``09:20, 09:25, ..., 15:15``
  (72 per day). At a decision instant ``T`` the completed candle that opened
  at ``T - 5m`` is the reference candle.
* The final grid points ``15:20`` (candle ``15:15`` completes), ``15:25``
  (``15:20``) and ``15:30`` (``15:25``) fall at/after the ``15:20`` EOD
  flatten and are **never decision instants** - exactly parallel to the 15M
  rule that the final triple is never a decision window. This preserves the
  track's hard safety invariant that a trading day always ends flat.
* Decisions (signal evaluation, protective stops, position switches, EOD
  flatten) happen **only** at those 72 points. Never on an incomplete candle;
  never from a duplicate or out-of-order candle; never after the flatten.

Reused unmodified from ``directional_15m.contract`` (identical contract, no
behaviour divergence possible): the approved transition table ``decide``, the
``Signal15m``/``Leg``/``Action`` enums, the ``BUY/SELL/HOLD`` intent mapping
``signal_to_15m`` and the direction-aware ``DirectionalStopPolicy``.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from fno_ai_paper_trading.experiments.directional_15m.contract import (
    Action,
    DirectionalStopPolicy,
    Leg,
    PositionDecision,
    Signal15m,
    StopCheck,
    decide,
    signal_to_15m,
)

__all__ = [
    "EXPERIMENT_ID",
    "BAR_MINUTES",
    "DECISION_MINUTES",
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

EXPERIMENT_ID = "5M_DIRECTIONAL_OPTIONS_EXPERIMENT"

BAR_MINUTES = 5
DECISION_MINUTES = 5

# First decision at 09:20 (candle 09:15 completes), last at 15:15 (candle
# 15:10 completes). Every 5-minute grid point thereafter is at/after the
# 15:20 EOD flatten and is never a decision instant.
DECISION_START = time(9, 20)
DECISION_STOP = time(15, 15)

# Frozen signal parameters of the approved signal source (unchanged contract).
DONCHIAN_ENTRY_CHANNEL = 20
DONCHIAN_EXIT_CHANNEL = 10


def decision_moments(day: date) -> list[datetime]:
    """All 72 decision instants for ``day``: 09:20, 09:25, ..., 15:15."""
    out: list[datetime] = []
    current = datetime.combine(day, DECISION_START)
    stop = datetime.combine(day, DECISION_STOP)
    while current <= stop:
        out.append(current)
        current += timedelta(minutes=DECISION_MINUTES)
    return out