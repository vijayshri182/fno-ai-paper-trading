"""End-to-end behavior tests for the continuous paper agent (WS 7.8)."""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from fno_ai_paper_trading.agent.agent import AgentConfig, ContinuousPaperAgent
from fno_ai_paper_trading.agent.states import AgentState
from fno_ai_paper_trading.agent.jobs import ClosedJob, RunPolicy
from fno_ai_paper_trading.alerting.engine import AlertEngine, CollectingAlertSink
from fno_ai_paper_trading.alerting.alerts import PAPER_TRADING_LABEL
from fno_ai_paper_trading.config.settings import Environment
from fno_ai_paper_trading.data.mock_provider import (
    InMemoryMarketDataProvider,
    build_sample_instruments,
)
from fno_ai_paper_trading.models.enums import Signal
from fno_ai_paper_trading.models.market import MarketPrice
from fno_ai_paper_trading.strategies.base import SignalResult, Strategy


def _settings(environment: Environment = Environment.TEST) -> object:
    from fno_ai_paper_trading.config.settings import PaperSettings

    return PaperSettings(
        environment=environment,
        initial_capital=Decimal("100000"),
        max_position_quantity=75,
        max_order_notional=Decimal("250000"),
        max_daily_loss=Decimal("10000"),
        commission_rate=Decimal("0.0003"),
        commission_fixed=Decimal("0"),
        slippage_rate=Decimal("0.001"),
    )


def _bars(instrument, count: int = 18, start_price: Decimal = Decimal("24000")):
    base = datetime(2026, 9, 8, 9, 20)  # Tuesday
    bars = []
    price = start_price
    for i in range(count):
        bars.append(
            MarketPrice(
                instrument=instrument,
                timestamp=base + timedelta(minutes=5 * i),
                open=price,
                high=price + 10,
                low=price - 10,
                close=price,
                volume=100,
            )
        )
        if i % 6 == 5:
            price -= Decimal("200")
        else:
            price += Decimal("40")
    return bars


def _provider(instrument, bars):
    return InMemoryMarketDataProvider(
        instruments=[instrument], history={instrument.symbol: bars}
    )


def _run(
    tmp_path,
    *,
    bars=None,
    clock_at=None,
    settings=None,
    environment=Environment.TEST,
    allow_sandbox=True,
    jobs=(),
    strategy=None,
    sizer=None,
    run_id="AGENT_TEST",
):
    instrument = build_sample_instruments()[0]
    bars = bars if bars is not None else _bars(instrument)
    provider = _provider(instrument, bars)
    sink = CollectingAlertSink()
    agent = ContinuousPaperAgent(
        AgentConfig(
            settings=settings or _settings(environment),
            provider=provider,
            instrument=instrument,
            state_dir=tmp_path,
            clock=lambda: clock_at or datetime(2026, 9, 8, 10, 50),
            alert_engine=AlertEngine(sinks=[sink]),
            jobs=jobs,
            strategy=strategy,
            sizer=sizer,
            allow_sandbox=allow_sandbox,
        ),
        run_id=run_id,
    )
    return agent, sink


class AlwaysBuy(Strategy):
    name = "always_buy"

    def analyze(self, bars: list[MarketPrice]) -> SignalResult:
        return SignalResult(
            signal=Signal.BUY,
            instrument=bars[-1].instrument,
            timestamp=bars[-1].timestamp,
            reason="test stub",
        )


def test_environment_guard_blocks_development(tmp_path):
    with pytest.raises(EnvironmentError):
        agent, _ = _run(
            tmp_path, environment=Environment.DEVELOPMENT, allow_sandbox=False
        )
        agent.cycle()


def test_sandbox_allowed(tmp_path):
    agent, _ = _run(tmp_path, environment=Environment.DEVELOPMENT, allow_sandbox=True)
    agent.start()  # must not raise
    assert agent.state is not AgentState.INIT


def test_open_cycle_consumes_bars(tmp_path):
    agent, _ = _run(tmp_path)
    res = agent.cycle()
    assert res.ran is True
    assert res.state == "MARKET_OPEN"
    assert res.poll_consumed == 18
    assert res.safety_decision == "SAFE"
    assert agent.heartbeat.consumed_bars == 18
    assert agent._state == AgentState.MARKET_OPEN


def test_open_cycle_records_boot_transition(tmp_path):
    agent, _ = _run(tmp_path)
    agent.cycle()
    assert agent.transitions[0].target == AgentState.MARKET_OPEN


def test_closed_cycle_runs_due_jobs_once(tmp_path):
    calls = []

    def job_run(ctx):
        calls.append(ctx.now)
        return {"ran": True}

    job = ClosedJob("j", "j", RunPolicy(), job_run)
    agent, _ = _run(tmp_path, clock_at=datetime(2026, 9, 8, 18, 0), jobs=(job,))
    first = agent.cycle()
    assert first.ran is True
    assert first.state == "MARKET_CLOSED"
    assert first.jobs_ran == 1
    assert len(calls) == 1

    second = agent.cycle()
    assert second.jobs_ran == 0
    assert len(calls) == 1


def test_closed_cycle_failing_job_soft(tmp_path):
    def boom(ctx):
        raise RuntimeError("statilli broke")

    job = ClosedJob("xm", "xm", RunPolicy(), boom)
    agent, sink = _run(tmp_path, clock_at=datetime(2026, 9, 8, 18, 0), jobs=(job,))
    res = agent.cycle()
    assert res.ran is True
    assert res.jobs_ran == 1
    assert "xm" in res.error  # failure surfaces, but the cycle completes
    titles = [a.title for a in sink.alerts]
    assert any("closed" in t.lower() for t in titles)


def test_cycle_respects_halted_state(tmp_path):
    agent, _ = _run(tmp_path)
    agent.start()
    agent.halt()
    assert agent.state is AgentState.HALTED
    res = agent.cycle()
    assert res.ran is False


def test_checkpoint_files_written(tmp_path):
    agent, _ = _run(tmp_path)
    agent.cycle()
    assert (tmp_path / "agent_state.json").is_file()
    assert (tmp_path / "agent_state.meta.json").is_file()
    session_files = [
        p
        for p in tmp_path.iterdir()
        if p.name.endswith(".json") and "agent_state" not in p.name
    ]
    assert session_files


def test_restore_preserves_progress(tmp_path):
    agent_a, _ = _run(tmp_path)
    agent_a.cycle()
    consumed_a = agent_a.heartbeat.consumed_bars
    assert consumed_a > 0

    agent_b, _ = _run(tmp_path, run_id="AGENT_TEST")
    agent_b.cycle()
    assert agent_b.heartbeat.consumed_bars == consumed_a
    assert agent_b.cycles_ran == agent_a.cycles_ran + 1
    assert agent_b.transitions[0].target == AgentState.MARKET_OPEN


def test_watchdog_stop_blocks_poll(tmp_path):
    instrument = build_sample_instruments()[0]
    bars = _bars(instrument)  # stale at 10:00 clock
    agent, sink = _run(tmp_path, bars=bars, clock_at=datetime(2026, 9, 8, 10, 0))
    res = agent.cycle()
    assert res.safety_decision == "STOP"
    assert res.poll_consumed == 0
    titles = [a.title for a in sink.alerts]
    assert any("fail-safe" in t.lower() for t in titles)


def test_alert_stamp_marks_paper_trading(tmp_path):
    agent, sink = _run(tmp_path)
    agent.cycle()
    assert sink.alerts
    for alert in sink.alerts:
        assert alert.environment == PAPER_TRADING_LABEL


def test_fill_path(tmp_path):
    instrument = build_sample_instruments()[0]
    bars = _bars(instrument)
    agent, sink = _run(
        tmp_path,
        bars=bars,
        strategy=AlwaysBuy(),
        sizer=None,  # fixed-quantity path so the entry is actually filled
        clock_at=datetime(2026, 9, 8, 10, 50),
    )
    res = agent.cycle()
    assert res.ran is True
    assert agent.heartbeat.orders_submitted >= 1
    assert agent.heartbeat.fills >= 1
    assert agent.heartbeat.equity != "0"


def test_heartbeat_is_safe_flag(tmp_path):
    agent, _ = _run(tmp_path)
    agent.cycle()
    assert agent.heartbeat.safety_decision == "SAFE"
    assert agent.safety is not None