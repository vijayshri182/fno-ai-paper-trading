"""Agent states and state transitions (WS 7.8).

The continuous paper-trading agent maps the NSE market phase (§17h) to one of
two safe agent states and only ever transitions along the verified edges:

* ``INIT``            -- agent constructed, not yet started,
* ``MARKET_CLOSED``   -- replay / evaluation / learning jobs (offline work),
* ``MARKET_OPEN``     -- completed-candle **paper** trading through the
                          existing ``PaperSession`` pipeline,
* ``HALTED``          -- operator-halted; ``cycle()`` becomes a no-op.

PRE_OPEN and CLOSED NSE phases both map to ``MARKET_CLOSED``: near-real-time
paper trading happens only during the OPEN phase (§17h). Live market data never
implies live broker execution; the agent's only execution path is ``PaperBroker``
(:class:`~fno_ai_paper_trading.services.paper_session.PaperSession`).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from fno_ai_paper_trading.data.market_hours import (
    NSE_TZ,
    market_phase,
    market_session,
)
from fno_ai_paper_trading.models.enums import MarketPhase
from fno_ai_paper_trading.models.market import MarketSession


class AgentState(str, Enum):
    """Safe states of the continuous paper-trading agent."""

    INIT = "INIT"
    MARKET_CLOSED = "MARKET_CLOSED"
    MARKET_OPEN = "MARKET_OPEN"
    HALTED = "HALTED"


#: Transitions allowed by :meth:`ContinuousPaperAgent.transition`.
ALLOWED_TRANSITIONS = frozenset({
    (AgentState.INIT, AgentState.MARKET_CLOSED),
    (AgentState.INIT, AgentState.MARKET_OPEN),
    (AgentState.INIT, AgentState.HALTED),
    (AgentState.MARKET_CLOSED, AgentState.MARKET_OPEN),
    (AgentState.MARKET_OPEN, AgentState.MARKET_CLOSED),
    (AgentState.MARKET_CLOSED, AgentState.HALTED),
    (AgentState.MARKET_OPEN, AgentState.HALTED),
    (AgentState.HALTED, AgentState.INIT),
})


@dataclass(frozen=True)
class StateTransition:
    """One recorded state transition (source -> target at a point in time)."""

    source: AgentState
    target: AgentState
    at: datetime
    reason: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "source": self.source.value,
            "target": self.target.value,
            "at": self.at.isoformat(timespec="seconds"),
            "reason": self.reason,
        }


def phase_to_agent_state(phase: MarketPhase) -> AgentState:
    """Map an NSE market phase to the agent's two-state machine.

    PRE_OPEN and CLOSED are both ``MARKET_CLOSED``: they are offline windows
    for replay / evaluation / learning; only the OPEN phase paper-trades
    completed candles near-real-time.
    """
    if phase is MarketPhase.OPEN:
        return AgentState.MARKET_OPEN
    return AgentState.MARKET_CLOSED


def agent_state_for(dt: datetime) -> AgentState:
    """Agent state implied by the NSE phase at ``dt`` (naive/aware IST)."""
    return phase_to_agent_state(market_phase(dt, tz=NSE_TZ))


def session_snapshot_for(dt: datetime) -> MarketSession:
    """Normalized :class:`MarketSession` for ``dt`` (used for heartbeats)."""
    return market_session(dt, tz=NSE_TZ)


def validate_transition(source: AgentState, target: AgentState) -> None:
    """Raise ``ValueError`` for an unrecognised state edge."""
    if (source, target) not in ALLOWED_TRANSITIONS:
        raise ValueError(
            f"illegal agent transition {source.value!r} -> {target.value!r}"
        )