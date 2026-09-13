"""Continuous paper-trading agent (WS 7.8, PROJECT_PLAN §17h).

The agent is an always-running orchestrator with two safe states it transitions
between without changing any safety rule:

* ``MARKET_CLOSED`` (including NSE PRE_OPEN): run booked offline jobs — replay /
  evaluation, learning-loop cycles, experience capture — and checkpoint state,
* ``MARKET_OPEN``: consume the latest available data, let the existing
  deterministic :class:`~fno_ai_paper_trading.services.paper_session.PaperSession`
  process completed candles (features, regime, advisory, deterministic risk,
  **PAPER** orders only) and record outcomes/alerts.

The paper-only boundary is absolute: the only execution path is ``PaperBroker``;
``Broker.is_live`` stays ``False``; the watchdog's fail-safe decision (STOP) gates
new cycle activity rather than guessing (§17k). Live market data never implies
live broker execution.

Every ``cycle()`` ends by checkpointing both the paper-session snapshot
(``persistence.session_store``) and the agent record
(``agent.persistence``), so a restart resumes exactly where the loop stopped.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Callable, Mapping, Sequence

from fno_ai_paper_trading.agent.heartbeat import (
    AgentHeartbeat,
    heartbeat_from_session,
    is_safe,
)
from fno_ai_paper_trading.agent.jobs import (
    ClosedJob,
    ClosedJobContext,
    JobResult,
    run_closed_jobs,
)
from fno_ai_paper_trading.agent.persistence import (
    AgentStateRecord,
    AGENT_STATE_NAME,
    load_agent_state,
    save_agent_state,
)
from fno_ai_paper_trading.agent.states import (
    AgentState,
    StateTransition,
    agent_state_for,
    session_snapshot_for,
    validate_transition,
)
from fno_ai_paper_trading.alerting.alerts import (
    PAPER_TRADING_LABEL,
    Alert,
    AlertCategory,
    AlertLevel,
)
from fno_ai_paper_trading.alerting.engine import AlertEngine
from fno_ai_paper_trading.alerting.health import SafetyDecision, TradingSafety, Watchdog
from fno_ai_paper_trading.config.settings import Environment, PaperSettings
from fno_ai_paper_trading.data.intervals import canonical_interval, interval_minutes
from fno_ai_paper_trading.data.provider import MarketDataProvider
from fno_ai_paper_trading.models.instruments import Instrument
from fno_ai_paper_trading.persistence.session_store import load_session, save_session
from fno_ai_paper_trading.services.paper_session import PaperSession
from fno_ai_paper_trading.strategies.base import Strategy
from fno_ai_paper_trading.strategies.moving_average_cross import MovingAverageCrossStrategy
from fno_ai_paper_trading.utils.logging import get_logger

AGENT_LOGGER = "paper.agent"

_MISSING = object()


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _naive(now: datetime) -> datetime:
    """Strip tzinfo so watchdog freshness checks stay naive-IST consistent."""
    return now.replace(tzinfo=None) if now.tzinfo is not None else now


@dataclass(frozen=True)
class AgentConfig:
    """Everything the continuous agent needs to run (all injectable)."""

    settings: PaperSettings
    provider: MarketDataProvider
    instrument: Instrument
    interval: str | None = None
    strategy: Strategy | None = None
    state_dir: str | Path = "paper_state"
    session_name: str | None = None
    clock: Callable[[], datetime] | None = None
    watchdog: Watchdog | None = None
    alert_engine: AlertEngine | None = None
    jobs: Sequence[ClosedJob] = ()
    quantity: int = 1
    warmup_bars: int | None = None
    sizer: object = _MISSING
    allow_sandbox: bool = False
    max_bar_age: timedelta | None = None
    job_datasets_dir: str = "datasets"
    job_out_dir: str = "reports/agent"
    job_extra: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        token = canonical_interval(self.interval or self.settings.paper_interval)
        if token is None:
            raise ValueError(
                f"unknown agent interval {self.interval or self.settings.paper_interval!r}"
            )
        minutes = interval_minutes(token)
        if minutes is None:
            raise ValueError("the agent interval must be a fixed-minute interval")
        object.__setattr__(self, "interval", token)


@dataclass(frozen=True)
class CycleResult:
    """One full agent cycle's outcome (serializable where needed)."""

    at: datetime
    state: str
    transitioned: bool
    poll_consumed: int
    jobs_ran: int
    safety_decision: str
    error: str = ""
    ran: bool = True

    def to_dict(self) -> dict[str, object]:
        return {
            "at": self.at.isoformat(timespec="seconds"),
            "state": self.state,
            "transitioned": self.transitioned,
            "poll_consumed": self.poll_consumed,
            "jobs_ran": self.jobs_ran,
            "safety_decision": self.safety_decision,
            "error": self.error,
            "ran": self.ran,
        }


class ContinuousPaperAgent:
    """The always-running, paper-only, checkpoint-safe orchestrator."""

    def __init__(
        self,
        config: AgentConfig,
        *,
        logger=None,
        run_id: str = "",
    ) -> None:
        if not isinstance(config, AgentConfig):
            raise TypeError("config must be an AgentConfig")
        self.config = config
        self.settings = config.settings
        self.interval_minutes = interval_minutes(config.interval) or 5
        self.state_dir = Path(config.state_dir)
        self.log = logger if logger is not None else get_logger(AGENT_LOGGER)

        self._session: PaperSession | None = None
        self._state = AgentState.INIT
        self._state_since = _naive(config.clock() if config.clock else datetime.now())
        self._transitions: list[StateTransition] = []
        self._cycle_count = 0
        self._jobs_last_run: dict[str, datetime] = {}
        self._jobs_run = 0
        self._last_safety: TradingSafety | None = None
        self._last_error = ""
        self._alert_engine = config.alert_engine or AlertEngine()
        self._heartbeat: AgentHeartbeat | None = None
        self.run_id = run_id

    # ------------------------------------------------------------------ clock
    def now(self) -> datetime:
        if self.config.clock is not None:
            return self.config.clock()
        return datetime.now()

    # ---------------------------------------------------------------- lifecycle
    @property
    def state(self) -> AgentState:
        return self._state

    @property
    def is_started(self) -> bool:
        return self._session is not None

    @property
    def session(self) -> PaperSession | None:
        return self._session

    @property
    def cycles_ran(self) -> int:
        return self._cycle_count

    @property
    def transitions(self) -> tuple[StateTransition, ...]:
        return tuple(self._transitions)

    @property
    def heartbeat(self) -> AgentHeartbeat:
        return self._heartbeat or heartbeat_from_session(
            run_id=self.run_id,
            environment=self.settings.environment,
            state=self._state,
            market_phase=session_snapshot_for(self.now()).phase.value,
            state_since=self._state_since,
            cycle_at=self.now(),
            session=self._session,
            safety=self._last_safety,
            jobs_run=0,
        )

    @property
    def safety(self) -> TradingSafety | None:
        return self._last_safety

    def start(self) -> None:
        """Boot the agent; enforce the paper-only environment gate and restore."""
        if self.settings.environment is not Environment.PAPER and not (
            self.config.allow_sandbox
            and self.settings.environment in (Environment.TEST, Environment.DEVELOPMENT)
        ):
            raise EnvironmentError(
                "the continuous paper agent requires Environment.PAPER "
                "(or an explicitly approved TEST/DEVELOPMENT sandbox override)"
            )
        self._restore_agent_state()
        try:
            self._restore_session()
        except (ValueError, FileNotFoundError) as exc:
            self._last_error = f"session restore skipped: {exc}"
            self.log.warning("agent start: %s", self._last_error)
        if not self.run_id:
            from fno_ai_paper_trading.utils.functions import new_id

            self.run_id = new_id("AGENT")
        if self._state is not AgentState.HALTED:
            boot_target = agent_state_for(self.now())
            if self._state is not boot_target:
                self._transitions.append(
                    StateTransition(
                        source=self._state,
                        target=boot_target,
                        at=_naive(self.now()),
                        reason="agent boot",
                    )
                )
            self._state = boot_target
            self._state_since = _naive(self.now())
        now = self.now()
        self.log.info(
            "agent %s started: env=%s state=%s (%s)",
            self.run_id,
            self.settings.environment.value,
            self._state.value,
            session_snapshot_for(now).phase.value,
        )
        self._emit(
            Alert(
                category=AlertCategory.SYSTEM,
                level=AlertLevel.INFO,
                title="continuous paper agent started",
                message=(
                    f"agent {self.run_id} state={self._state.value}; "
                    f"environment={self.settings.environment.value}; "
                    "PAPER TRADING ONLY - NO LIVE ORDER"
                ),
                source="paper-agent",
            )
        )

    def halt(self) -> None:
        self._transition(AgentState.HALTED, reason="operator halt")
        self.log.warning("agent %s halted by operator", self.run_id)

    def resume(self) -> None:
        """HALTED -> INIT so the next :meth:`cycle` recomputes from the market."""
        validate_transition(AgentState.HALTED, AgentState.INIT)
        self._state = AgentState.INIT
        self._state_since = _naive(self.now())

    # -------------------------------------------------------------- transitions
    def _transition(self, target: AgentState, reason: str = "") -> bool:
        if target is self._state or self._state is AgentState.HALTED:
            return False
        validate_transition(self._state, target)
        transition = StateTransition(
            source=self._state,
            target=target,
            at=_naive(self.now()),
            reason=reason,
        )
        self._transitions.append(transition)
        self._state = target
        self._state_since = _naive(self.now())
        self.log.info(
            "agent %s state %s -> %s (%s)",
            self.run_id,
            transition.source.value,
            target.value,
            reason or "market phase change",
        )
        if target in (AgentState.MARKET_OPEN, AgentState.MARKET_CLOSED):
            self._emit(
                Alert(
                    category=AlertCategory.SYSTEM,
                    level=AlertLevel.INFO,
                    title=f"market state change: {target.value}",
                    message=f"{transition.source.value} -> {target.value} ({reason or 'phase'})",
                    source="paper-agent",
                )
            )
        return True

    # ------------------------------------------------------------------- cycle
    def cycle(self, when: datetime | None = None) -> CycleResult:
        """Run one full agent cycle (open: poll; closed: jobs; then checkpoint)."""
        now = _naive(when if when is not None else self.now())
        if self._state is AgentState.HALTED:
            return CycleResult(
                at=now,
                state=self._state.value,
                transitioned=False,
                poll_consumed=0,
                jobs_ran=0,
                safety_decision=self._last_safety.decision.value
                if self._last_safety is not None
                else "N/A",
                ran=False,
                error="agent halted",
            )
        if not self.is_started:
            self.start()

        desired = agent_state_for(now)
        transitioned = self._transition(desired)

        poll_consumed = 0
        jobs_ran = 0
        error = self._last_error

        if self._state is AgentState.MARKET_OPEN:
            poll_consumed, jobs_ran, error = self._open_cycle(now)
        else:
            jobs_ran, error = self._closed_cycle(now)

        self._cycle_count += 1
        self._checkpoint(now)
        safety_decision = (
            self._last_safety.decision.value if self._last_safety is not None else "N/A"
        )
        return CycleResult(
            at=now,
            state=self._state.value,
            transitioned=transitioned,
            poll_consumed=poll_consumed,
            jobs_ran=jobs_ran,
            safety_decision=safety_decision,
            error=error if error else "",
        )

    # ------------------------------------------------------------ open cycle
    def _open_cycle(self, now: datetime) -> tuple[int, int, str]:
        session = self._ensure_session()
        session.start()
        poll_consumed = 0
        jobs_ran = 0
        error = ""

        latest_bar_time = self._latest_bar_time()
        components, details = self._watch_components()
        watchdog = self.config.watchdog or Watchdog(
            max_bar_age=self._default_bar_age(),
            now=lambda: now,
        )
        report = watchdog.evaluate(
            latest_bar_time=latest_bar_time,
            components=components,
            details=details,
        )
        safety = watchdog.safety(report)
        self._last_safety = safety
        for alert in watchdog.alerts_for(report, safety):
            self._emit(alert)

        if safety.decision is SafetyDecision.STOP:
            error = f"fail-safe STOP: {safety.instruction}"
            self._last_error = error
            self.log.critical("agent %s: %s", self.run_id, error)
            return poll_consumed, jobs_ran, error

        try:
            result = session.poll(now)
        except Exception as exc:  # noqa: BLE001 - a poll never kills the agent
            error = f"poll failed: {exc}"
            self._last_error = error
            self.log.warning("agent %s: %s", self.run_id, error)
            return poll_consumed, jobs_ran, error

        poll_consumed = result.consumed
        if result.provider_error:
            error = f"provider: {result.provider_error}"
            self._last_error = error
            self.log.warning("agent %s: %s", self.run_id, error)
            self._emit(
                Alert(
                    category=AlertCategory.SYSTEM,
                    level=AlertLevel.WARNING,
                    title="market data error during poll",
                    message=result.provider_error,
                    source="paper-agent",
                )
            )
        for step in result.steps:
            if step.error is not None:
                self._emit(
                    Alert(
                        category=AlertCategory.RISK,
                        level=AlertLevel.WARNING,
                        title="paper step error",
                        message=step.error,
                        source="paper-agent",
                    )
                )
            for order_result in (step.order_result, step.stop_result):
                if order_result is not None and order_result.fill is not None:
                    self._emit_open_alert(order_result, step.bar.timestamp)
        return poll_consumed, jobs_ran, error

    def _emit_open_alert(self, order_result, at: datetime) -> None:
        fill = order_result.fill
        side = fill.side.value
        if order_result.trade is not None:
            level = AlertLevel.INFO
            title = f"paper round trip closed ({side})"
            message = (
                f"{fill.quantity} @ {fill.price:f} on {at.isoformat(timespec='minutes')}; "
                f"realized {order_result.trade.realized_pnl:f}"
            )
        else:
            level = AlertLevel.INFO
            title = f"paper fill ({side})"
            message = (
                f"{fill.quantity} @ {fill.price:f} on {at.isoformat(timespec='minutes')} "
                f"(commission {fill.commission:f}, {PAPER_TRADING_LABEL})"
            )
        self._emit(
            Alert(
                category=AlertCategory.TRADING,
                level=level,
                title=title,
                message=message,
                source="paper-agent",
            )
        )

    # ----------------------------------------------------------- closed cycle
    def _closed_cycle(self, now: datetime) -> tuple[int, str]:
        """Run booked offline jobs (market closed / pre-open window)."""
        jobs_ran = 0
        error = ""
        store = None
        registry = None
        if self.config.jobs:
            try:
                from fno_ai_paper_trading.persistence.experience_store import ExperienceStore
                from fno_ai_paper_trading.promotion.registry import VersionRegistry

                store = ExperienceStore(
                    directory="experience_store", name="experiences"
                )
                registry = VersionRegistry("model_registry")
            except Exception as exc:  # noqa: BLE001 - jobs can still run without store
                error = f"agent support stores unavailable: {exc}"
                self._last_error = error

        ctx = ClosedJobContext(
            now=now,
            datasets_dir=self.config.job_datasets_dir,
            out_dir=self.config.job_out_dir,
            store=store,
            registry=registry,
            factories={},
            extra=self.config.job_extra,
        )
        results, updated = run_closed_jobs(self.config.jobs, ctx, self._jobs_last_run)
        self._jobs_last_run = updated
        jobs_ran = sum(1 for result in results if result.ran)
        self._jobs_run += sum(
            1 for result in results if result.ran and not result.error
        )
        for result in results:
            if result.ran and result.error:
                self._emit(
                    Alert(
                        category=AlertCategory.AI_LEARNING,
                        level=AlertLevel.WARNING,
                        title=f"closed-market job failed: {result.job_id}",
                        message=result.error,
                        source="paper-agent",
                    )
                )
                error = f"job {result.job_id}: {result.error}"
                self._last_error = error
            elif result.ran:
                self.log.info(
                    "agent %s job %s ran (%s)",
                    self.run_id,
                    result.job_id,
                    _summarize(result),
                )
        return jobs_ran, error

    # ------------------------------------------------------------- checkpoint
    def _checkpoint(self, now: datetime) -> None:
        """Persist session snapshot + agent record (crash recovery)."""
        self.state_dir.mkdir(parents=True, exist_ok=True)
        if self._session is not None:
            session_snapshot = self._session.snapshot()
            save_session(session_snapshot, directory=self.state_dir, name=self._session_name_safe())
        record = AgentStateRecord(
            run_id=self.run_id,
            state=self._state.value,
            state_since=self._state_since.isoformat(timespec="seconds"),
            cycle_count=self._cycle_count,
            transitions=tuple(t.to_dict() for t in self._transitions),
            jobs_last_run={
                key: value.isoformat(timespec="seconds")
                for key, value in self._jobs_last_run.items()
            },
            heartbeat=self._heartbeat_for(now),
        )
        save_agent_state(record, directory=self.state_dir)
        self._heartbeat = record.heartbeat
        self.log.info(
            "agent %s checkpointed (cycle %s, state %s)",
            self.run_id,
            self._cycle_count,
            self._state.value,
        )

    def _heartbeat_for(self, now: datetime) -> AgentHeartbeat:
        session = self._session
        session_counts: dict[str, object] = {}
        if session is not None:
            prices = _session_mark_prices(session)
            position = session.portfolio.position_for(session.instrument.symbol)
            session_counts.update({
                "consumed_bars": session.consumed_candles,
                "orders_submitted": session.orders_submitted,
                "fills": session.fills,
                "trades": session.trades,
                "skips": session.skipped_candles,
                "rejections": session.rejections,
                "cash": str(session.portfolio.cash),
                "equity": str(session.portfolio.total_value(prices)),
                "open_quantity": position.quantity if position is not None else 0,
            })
        safety = self._last_safety
        return AgentHeartbeat(
            agent_version="1.0.0",
            run_id=self.run_id,
            environment=self.settings.environment.value,
            state=self._state.value,
            market_phase=session_snapshot_for(now).phase.value,
            state_since=self._state_since.isoformat(timespec="seconds"),
            cycle_at=now.isoformat(timespec="seconds"),
            polls=1,
            jobs_run=self._jobs_run,
            last_error=self._last_error,
            safety_decision=safety.decision.value if safety is not None else "N/A",
            safety_instruction=safety.instruction if safety is not None else "",
            checkpointed_at=now.isoformat(timespec="seconds"),
            latest_bar_time=self._latest_bar_time_iso(),
            **session_counts,
        )

    # ----------------------------------------------------------------- helpers
    def _ensure_session(self) -> PaperSession:
        if self._session is not None:
            return self._session
        kwargs = {
            "instrument": self.config.instrument,
            "quantity": self.config.quantity,
            "clock": self.config.clock or datetime.now,
            "interval": self.config.interval,
            "warmup_bars": self.config.warmup_bars,
            "allow_sandbox": self.config.allow_sandbox,
        }
        if self.config.sizer is not _MISSING:
            kwargs["sizer"] = self.config.sizer
        self._session = PaperSession(
            self.settings,
            self.config.provider,
            self.config.strategy if self.config.strategy is not None else MovingAverageCrossStrategy(),
            **kwargs,
        )
        return self._session

    def _session_name_safe(self) -> str:
        base = self.config.session_name or f"{self.config.instrument.symbol}_{self.config.interval}"
        return base

    def _latest_bar_time(self):
        try:
            bars = self.config.provider.get_ohlcv(self.config.instrument)
        except Exception:  # noqa: BLE001
            return datetime.min
        return bars[-1].timestamp if bars else datetime.min

    def _latest_bar_time_iso(self) -> str:
        latest = self._latest_bar_time()
        if latest == datetime.min:
            return ""
        return latest.isoformat(timespec="seconds")

    def _watch_components(self) -> tuple[Mapping[str, bool], Mapping[str, str]]:
        components = {
            "provider": True,
            "risk_manager": True,
            "stop_loss": True,
        }
        details = {
            "provider": f"interval {self.config.interval}",
            "risk_manager": "static caps enforced",
            "stop_loss": "protective exits enforced",
        }
        return components, details

    def _default_bar_age(self) -> timedelta:
        if self.config.max_bar_age is not None:
            return self.config.max_bar_age
        return timedelta(minutes=2 * self.interval_minutes + 1)

    @property
    def default_bar_age(self) -> timedelta:
        return self._default_bar_age()

    def _restore_agent_state(self) -> None:
        candidate = self.state_dir / AGENT_STATE_NAME
        if not candidate.is_file():
            return
        try:
            stored = load_agent_state(candidate)
        except (ValueError, FileNotFoundError) as exc:
            self.log.warning("agent state not restored: %s", exc)
            return
        record = stored.record
        self.run_id = record.run_id
        self._cycle_count = record.cycle_count
        self._state = AgentState(record.state)
        if record.state_since:
            self._state_since = datetime.fromisoformat(record.state_since)
        self._transitions = [
            StateTransition(
                source=AgentState(item["source"]),
                target=AgentState(item["target"]),
                at=datetime.fromisoformat(item["at"]),
                reason=str(item.get("reason", "")),
            )
            for item in record.transitions
        ]
        self._jobs_last_run = {
            key: datetime.fromisoformat(value) for key, value in record.jobs_last_run.items()
        }
        self._jobs_run = record.heartbeat.jobs_run
        self._heartbeat = record.heartbeat
        self.log.info(
            "agent %s restored (state=%s, cycles=%s)",
            self.run_id,
            self._state.value,
            self._cycle_count,
        )

    def _restore_session(self) -> None:
        payload = self.state_dir / f"{self._session_name_safe()}.json"
        if not payload.is_file():
            return
        stored = load_session(payload)
        session = self._ensure_session()
        session.restore(stored.snapshot)
        self._session = session
        self.log.info(
            "session restored from %s (%s bars consumed)",
            payload,
            len(stored.snapshot.consumed_timestamps),
        )

    # ----------------------------------------------------------------- alerts
    def _emit(self, alert: Alert) -> None:
        try:
            self._alert_engine.emit(alert)
        except Exception:  # noqa: BLE001 - alert delivery never takes the agent down
            self.log.warning("agent alert delivery failed for %s", alert.title)


def _summarize(result: JobResult) -> str:
    return "; ".join(f"{k}={v}" for k, v in result.result.items())[:160]


def _session_mark_prices(session) -> dict[str, Decimal]:
    """Mark open positions off the latest available close (session provider)."""
    prices: dict[str, Decimal] = {}
    for symbol, position in session.portfolio.open_positions().items():
        prices[symbol] = position.average_entry_price
    try:
        bars = session.provider.get_ohlcv(session.instrument)
    except Exception:  # noqa: BLE001 - a mark failure must never crash a heartbeat
        return prices
    if bars:
        prices[session.instrument.symbol] = bars[-1].close
    return prices