"""Agent heartbeat record (WS 7.8).

A single, serializable snapshot of the continuous paper-trading agent's
operational state. It mirrors the WS 7.7 SYSTEM dashboard view fields and is
always labelled paper-trading-only. Money values are stored as
``str(Decimal)`` losslessly; timestamps are naive IST like the rest of the
domain models.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Mapping

from fno_ai_paper_trading.agent.states import AgentState
from fno_ai_paper_trading.alerting.health import SafetyDecision, TradingSafety
from fno_ai_paper_trading.config.settings import Environment

AGENT_VERSION = "1.0.0"
HEARTBEAT_DISCLAIMER = (
    "Continuous paper-trading agent (WS 7.8). PAPER TRADING ONLY - NO LIVE "
    "ORDER. Live market data must never imply live broker execution; the only "
    "execution path is PaperBroker."
)


def _dt(value: datetime | None) -> str:
    return value.isoformat(timespec="seconds") if value is not None else ""


@dataclass(frozen=True)
class AgentHeartbeat:
    """Deterministic operational snapshot of the agent at one point in time."""

    agent_version: str = AGENT_VERSION
    run_id: str = ""
    environment: str = Environment.DEVELOPMENT.value
    state: str = AgentState.INIT.value
    market_phase: str = ""
    state_since: str = ""
    cycle_at: str = field(default="")
    polls: int = 0
    consumed_bars: int = 0
    orders_submitted: int = 0
    fills: int = 0
    trades: int = 0
    skips: int = 0
    rejections: int = 0
    cash: str = "0"
    equity: str = "0"
    open_quantity: int = 0
    safety_decision: str = "N/A"
    safety_instruction: str = ""
    latest_bar_time: str = ""
    jobs_run: int = 0
    last_error: str = ""
    checkpointed_at: str = ""
    disclaimer: str = HEARTBEAT_DISCLAIMER

    def to_dict(self) -> dict[str, object]:
        return {
            "agent_version": self.agent_version,
            "run_id": self.run_id,
            "environment": self.environment,
            "state": self.state,
            "market_phase": self.market_phase,
            "state_since": self.state_since,
            "cycle_at": self.cycle_at,
            "polls": self.polls,
            "consumed_bars": self.consumed_bars,
            "orders_submitted": self.orders_submitted,
            "fills": self.fills,
            "trades": self.trades,
            "skips": self.skips,
            "rejections": self.rejections,
            "cash": self.cash,
            "equity": self.equity,
            "open_quantity": self.open_quantity,
            "safety_decision": self.safety_decision,
            "safety_instruction": self.safety_instruction,
            "latest_bar_time": self.latest_bar_time,
            "jobs_run": self.jobs_run,
            "last_error": self.last_error,
            "checkpointed_at": self.checkpointed_at,
            "disclaimer": self.disclaimer,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "AgentHeartbeat":
        return cls(
            agent_version=str(payload.get("agent_version", AGENT_VERSION)),
            run_id=str(payload.get("run_id", "")),
            environment=str(
                payload.get("environment", Environment.DEVELOPMENT.value)
            ),
            state=str(payload.get("state", AgentState.INIT.value)),
            market_phase=str(payload.get("market_phase", "")),
            state_since=str(payload.get("state_since", "")),
            cycle_at=str(payload.get("cycle_at", "")),
            polls=int(payload.get("polls", 0)),
            consumed_bars=int(payload.get("consumed_bars", 0)),
            orders_submitted=int(payload.get("orders_submitted", 0)),
            fills=int(payload.get("fills", 0)),
            trades=int(payload.get("trades", 0)),
            skips=int(payload.get("skips", 0)),
            rejections=int(payload.get("rejections", 0)),
            cash=str(payload.get("cash", "0")),
            equity=str(payload.get("equity", "0")),
            open_quantity=int(payload.get("open_quantity", 0)),
            safety_decision=str(payload.get("safety_decision", "N/A")),
            safety_instruction=str(payload.get("safety_instruction", "")),
            latest_bar_time=str(payload.get("latest_bar_time", "")),
            jobs_run=int(payload.get("jobs_run", 0)),
            last_error=str(payload.get("last_error", "")),
            checkpointed_at=str(payload.get("checkpointed_at", "")),
            disclaimer=str(payload.get("disclaimer", HEARTBEAT_DISCLAIMER)),
        )


def heartbeat_from_session(
    *,
    run_id: str,
    environment: Environment,
    state: AgentState,
    market_phase: str,
    state_since: datetime | None,
    cycle_at: datetime | None,
    session=None,
    safety: TradingSafety | None = None,
    latest_bar_time: datetime | None = None,
    jobs_run: int = 0,
    last_error: str = "",
    checkpointed_at: datetime | None = None,
) -> AgentHeartbeat:
    """Build a heartbeat, reading session counters where ``session`` exists."""
    common: dict[str, object] = {
        "run_id": run_id,
        "environment": environment.value,
        "state": state.value,
        "market_phase": market_phase,
        "state_since": _dt(state_since),
        "cycle_at": _dt(cycle_at),
        "jobs_run": jobs_run,
        "last_error": last_error,
        "checkpointed_at": _dt(checkpointed_at),
        "latest_bar_time": _dt(latest_bar_time),
    }
    if safety is not None:
        common["safety_decision"] = safety.decision.value
        common["safety_instruction"] = safety.instruction
    if session is not None:
        cash = session.portfolio.cash
        prices = _mark_prices(session, latest_bar_time)
        equity = session.portfolio.total_value(prices)
        position = session.portfolio.position_for(session.instrument.symbol)
        common["polls"] = 0
        common["consumed_bars"] = session.consumed_candles
        common["orders_submitted"] = session.orders_submitted
        common["fills"] = session.fills
        common["trades"] = session.trades
        common["skips"] = session.skipped_candles
        common["rejections"] = session.rejections
        common["cash"] = str(cash)
        common["equity"] = str(equity)
        common["open_quantity"] = position.quantity if position is not None else 0
    return AgentHeartbeat(**common)


def _mark_prices(session, fallback_time: datetime | None) -> dict[str, Decimal]:
    """Mark open positions off the latest available close (session provider)."""
    prices: dict[str, Decimal] = {}
    for symbol, position in session.portfolio.open_positions().items():
        prices[symbol] = position.average_entry_price
    try:
        bars = session.provider.get_ohlcv(session.instrument)
    except Exception:  # noqa: BLE001 - a mark failure must never crash a heartbeat
        return prices
    if not bars:
        return prices
    prices[session.instrument.symbol] = bars[-1].close
    return prices


def is_safe(safety: TradingSafety | None) -> bool:
    """True when there is no STOP fail-safe decision pending."""
    return safety is None or safety.decision is not SafetyDecision.STOP