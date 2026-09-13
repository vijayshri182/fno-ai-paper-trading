"""Tests for the agent heartbeat record (WS 7.8)."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from fno_ai_paper_trading.agent.heartbeat import (
    HEARTBEAT_DISCLAIMER,
    AgentHeartbeat,
    heartbeat_from_session,
    is_safe,
)
from fno_ai_paper_trading.agent.states import AgentState
from fno_ai_paper_trading.alerting.health import SafetyDecision, TradingSafety
from fno_ai_paper_trading.config.settings import Environment
from fno_ai_paper_trading.data.mock_provider import InMemoryMarketDataProvider, build_crossing_ohlcv, build_sample_instruments
from fno_ai_paper_trading.models.market import MarketPrice
from fno_ai_paper_trading.services.paper_session import PaperSession


def _session():
    instruments = build_sample_instruments()
    instrument = instruments[0]
    bars = build_crossing_ohlcv(instrument)
    provider = InMemoryMarketDataProvider(
        instruments=[instrument], history={instrument.symbol: bars}
    )
    session = PaperSession(
        _settings(),
        provider,
        instrument=instrument,
        clock=lambda: datetime(2026, 9, 8, 15, 0),
        interval="5m",
        allow_sandbox=True,
    )
    session.start()
    session.poll(datetime(2026, 9, 8, 15, 0))
    return session


def _settings():
    from fno_ai_paper_trading.config.settings import PaperSettings

    return PaperSettings(
        environment=Environment.TEST,
        initial_capital=Decimal("100000"),
        max_position_quantity=75,
        max_order_notional=Decimal("250000"),
        max_daily_loss=Decimal("10000"),
        commission_rate=Decimal("0.0003"),
        commission_fixed=Decimal("0"),
        slippage_rate=Decimal("0.001"),
    )


def test_default_heartbeat():
    hb = AgentHeartbeat()
    assert hb.state == "INIT"
    assert hb.disclaimer == HEARTBEAT_DISCLAIMER
    assert hb.safety_decision == "N/A"
    assert hb.cash == "0"


def test_heartbeat_round_trip():
    hb = AgentHeartbeat(
        run_id="AGENT_x",
        state="MARKET_OPEN",
        market_phase="OPEN",
        consumed_bars=7,
        orders_submitted=1,
        fills=1,
        trades=1,
        cash="99999",
        equity="99999",
    )
    restored = AgentHeartbeat.from_dict(hb.to_dict())
    assert restored.run_id == "AGENT_x"
    assert restored.state == "MARKET_OPEN"
    assert restored.consumed_bars == 7
    assert restored.cash == "99999"


def test_heartbeat_from_session_reads_counts():
    session = _session()
    hb = heartbeat_from_session(
        run_id="AGENT_TEST",
        environment=Environment.TEST,
        state=AgentState.MARKET_OPEN,
        market_phase="OPEN",
        state_since=None,
        cycle_at=None,
        session=session,
    )
    assert hb.state == "MARKET_OPEN"
    assert hb.consumed_bars == session.consumed_candles
    assert hb.orders_submitted == session.orders_submitted
    assert hb.cash != "0"


def test_heartbeat_from_session_empty():
    hb = heartbeat_from_session(
        run_id="", environment=Environment.TEST, state=AgentState.INIT,
        market_phase="", state_since=None, cycle_at=None, session=None,
    )
    assert hb.consumed_bars == 0
    assert hb.equity == "0"


def test_is_safe():
    assert is_safe(None) is True
    assert is_safe(TradingSafety(SafetyDecision.SAFE, "go", ())) is True
    stop = TradingSafety(SafetyDecision.STOP, "stop", ("x",))
    assert is_safe(stop) is False