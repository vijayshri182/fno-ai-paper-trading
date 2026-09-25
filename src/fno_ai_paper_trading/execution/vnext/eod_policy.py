"""End-of-day / overnight hold policy for the isolated vNext layer.

Implements Policy A (intraday flatten) from
``docs/execution_semantics_vnext_design.md`` section E: positions must be FLAT
before the NIFTY cash-session close; the module refuses any opening that cannot
finish a full hold + flatten inside the trading window, and demands a flatten
for any held position outside the window. Policy B (multi-day) is expressed as a
distinct class so the two policies are never conflated — the vNext machine stays
wired to Policy A; Policy B remains a research future.

Isolation: this module imports ONLY the vNext tree (no ``execution/risk``
OvernightGuard, no harness), so the static isolation audit still passes and the
module can never be autowired into the WS 7.9 harness. The clock is injectable
and deterministic; production semantics assume India Standard Time (UTC+5:30,
no DST) for the session boundaries.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time as dtime
from enum import Enum
from typing import Protocol

from fno_ai_paper_trading.execution.vnext.enums import PositionState, SignalDirection
from fno_ai_paper_trading.execution.vnext.mapping import OrderStep, plan_orders


class EodDecisionKind(str, Enum):
    """What the EOD policy demands of the position controller."""

    HOLD = "HOLD"
    FLATTEN_DUE = "FLATTEN_DUE"


@dataclass(frozen=True)
class EodDecision:
    """One EOD policy verdict for the given position at a given wall-clock time."""

    kind: EodDecisionKind
    position: PositionState
    now: datetime
    message: str = ""


class EodPolicy(Protocol):
    """Decision surface consumed by the position controller (never the harness)."""

    def decide(self, position: PositionState, now: datetime) -> EodDecision:
        ...

    def can_open(self, now: datetime) -> bool:
        ...


class IntradayFlattenPolicy:
    """Policy A: flat-before-close, no overnight.

    Defaults mirror the WS 7.9 OvernightGuard intent (no position may survive
    the 15:30 cash-close) with an explicit flatten cutoff so a held position
    has a guaranteed window to exit. The cutoff and session bounds are
    configurable solely to keep the tests deterministic — production keeps the
    NIFTY defaults.
    """

    def __init__(
        self,
        *,
        session_open: dtime = dtime(9, 15),
        session_close: dtime = dtime(15, 30),
        flatten_cutoff: dtime = dtime(15, 25),
    ) -> None:
        if not (session_open < flatten_cutoff <= session_close):
            raise ValueError(
                "session_open < flatten_cutoff <= session_close required"
            )
        self.session_open = session_open
        self.session_close = session_close
        self.flatten_cutoff = flatten_cutoff

    def _in_session(self, now: datetime) -> bool:
        if now.weekday() >= 5:  # Saturday / Sunday
            return False
        return self.session_open <= now.time() < self.session_close

    def _inside_hold_window(self, now: datetime) -> bool:
        if now.weekday() >= 5:
            return False
        return self.session_open <= now.time() < self.flatten_cutoff

    def decide(self, position: PositionState, now: datetime) -> EodDecision:
        if position is PositionState.FLAT:
            return EodDecision(EodDecisionKind.HOLD, position, now, "flat; nothing to do")
        if not self._in_session(now):
            return EodDecision(
                EodDecisionKind.FLATTEN_DUE,
                position,
                now,
                "session closed; held position must flatten",
            )
        if now.time() >= self.flatten_cutoff:
            return EodDecision(
                EodDecisionKind.FLATTEN_DUE,
                position,
                now,
                "flatten cutoff reached; position must not cross into the close",
            )
        return EodDecision(EodDecisionKind.HOLD, position, now, "inside intraday hold window")

    def can_open(self, now: datetime) -> bool:
        # An opening must leave time to hold then flatten inside one session.
        return self._inside_hold_window(now)


class MultiDayHoldPolicy:
    """Policy B: multi-day holds (research future) — never wired into the machine.

    Returns HOLD for held positions and permits openings at any session time.
    Distinct class on purpose: the divergence makes it impossible to silently
    route the intraday machine to overnight behavior.
    """

    def decide(self, position: PositionState, now: datetime) -> EodDecision:
        if position is PositionState.FLAT:
            return EodDecision(EodDecisionKind.HOLD, position, now, "flat; nothing to do")
        return EodDecision(
            EodDecisionKind.HOLD, position, now, "multi-day policy holds overnight"
        )

    def can_open(self, now: datetime) -> bool:
        return True


def flatten_orders(position: PositionState) -> tuple[OrderStep, ...]:
    """The ordered steps that flatten a held position (SELL the held leg).

    Reuses the pinned transition table (``plan_orders(position, FLAT)``) so the
    EOD layer and the machine emit byte-identical closing semantics. FLAT yields
    no orders.
    """
    steps, _ = plan_orders(position, SignalDirection.FLAT)
    return tuple(steps)


__all__ = [
    "EodDecisionKind",
    "EodDecision",
    "EodPolicy",
    "IntradayFlattenPolicy",
    "MultiDayHoldPolicy",
    "flatten_orders",
]