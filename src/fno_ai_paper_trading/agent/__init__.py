"""Continuous paper-trading agent (WS 7.8).

An always-running, paper-only orchestrator that safely transitions between
``MARKET_CLOSED`` (replay / evaluation / learning jobs) and ``MARKET_OPEN``
(completed-candle paper trading through ``PaperSession``) without changing any
safety rule. Live market data never implies live broker execution.
"""
from fno_ai_paper_trading.agent.agent import (
    AgentConfig,
    ContinuousPaperAgent,
    CycleResult,
)
from fno_ai_paper_trading.agent.heartbeat import AgentHeartbeat, heartbeat_from_session
from fno_ai_paper_trading.agent.jobs import (
    ClosedJob,
    ClosedJobContext,
    JobResult,
    RunPolicy,
    build_learning_cycle_job,
    build_replay_capture_job,
)
from fno_ai_paper_trading.agent.persistence import (
    AgentStateRecord,
    StoredAgentState,
    load_agent_state,
    save_agent_state,
)
from fno_ai_paper_trading.agent.states import (
    ALLOWED_TRANSITIONS,
    AgentState,
    StateTransition,
    agent_state_for,
    phase_to_agent_state,
    session_snapshot_for,
)

__all__ = [
    "AgentConfig",
    "ContinuousPaperAgent",
    "CycleResult",
    "AgentHeartbeat",
    "heartbeat_from_session",
    "ClosedJob",
    "ClosedJobContext",
    "JobResult",
    "RunPolicy",
    "build_learning_cycle_job",
    "build_replay_capture_job",
    "AgentStateRecord",
    "StoredAgentState",
    "load_agent_state",
    "save_agent_state",
    "AgentState",
    "ALLOWED_TRANSITIONS",
    "StateTransition",
    "agent_state_for",
    "phase_to_agent_state",
    "session_snapshot_for",
]