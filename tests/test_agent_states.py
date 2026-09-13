"""Tests for agent state machine mappings (WS 7.8)."""
from __future__ import annotations

from datetime import datetime

import pytest

from fno_ai_paper_trading.agent.states import (
    ALLOWED_TRANSITIONS,
    AgentState,
    StateTransition,
    agent_state_for,
    phase_to_agent_state,
    session_snapshot_for,
    validate_transition,
)
from fno_ai_paper_trading.models.enums import MarketPhase


def test_phase_mapping_open():
    assert phase_to_agent_state(MarketPhase.OPEN) is AgentState.MARKET_OPEN


@pytest.mark.parametrize("phase", [MarketPhase.PRE_OPEN, MarketPhase.CLOSED])
def test_phase_mapping_closed_windows(phase):
    assert phase_to_agent_state(phase) is AgentState.MARKET_CLOSED


def test_agent_state_for_weekday_open():
    # Tuesday 2026-09-08 11:45 IST is inside the NSE OPEN phase.
    dt = datetime(2026, 9, 8, 11, 45)
    assert agent_state_for(dt) is AgentState.MARKET_OPEN


@pytest.mark.parametrize(
    "dt",
    [
        datetime(2026, 9, 8, 8, 30),  # before pre-open
        datetime(2026, 9, 8, 9, 5),  # pre-open (09:00-09:15)
        datetime(2026, 9, 8, 15, 35),  # after close
    ],
)
def test_agent_state_for_closed_windows(dt):
    assert agent_state_for(dt) is AgentState.MARKET_CLOSED


def test_agent_state_for_weekend():
    assert agent_state_for(datetime(2026, 9, 12, 11, 0)) is AgentState.MARKET_CLOSED


def test_session_snapshot_for():
    snap = session_snapshot_for(datetime(2026, 9, 8, 12, 0))
    assert snap.is_open is True
    assert snap.exchange == "NSE"


def test_validate_transition_allowed():
    validate_transition(AgentState.MARKET_CLOSED, AgentState.MARKET_OPEN)


def test_validate_transition_illegal():
    with pytest.raises(ValueError):
        validate_transition(AgentState.MARKET_OPEN, AgentState.HALTED)  # allowed
        validate_transition(AgentState.MARKET_OPEN, AgentState.INIT)  # illegal


def test_allowed_transitions_collection():
    assert (AgentState.INIT, AgentState.MARKET_OPEN) in ALLOWED_TRANSITIONS
    assert (AgentState.INIT, AgentState.HALTED) in ALLOWED_TRANSITIONS
    assert (AgentState.HALTED, AgentState.INIT) in ALLOWED_TRANSITIONS
    assert (AgentState.MARKET_OPEN, AgentState.INIT) not in ALLOWED_TRANSITIONS


def test_transition_round_trip():
    t = StateTransition(
        AgentState.INIT, AgentState.MARKET_OPEN, datetime(2026, 9, 8, 12, 0), reason="boot"
    )
    payload = t.to_dict()
    assert payload["source"] == "INIT"
    assert payload["target"] == "MARKET_OPEN"
    assert payload["reason"] == "boot"
    assert "at" in payload