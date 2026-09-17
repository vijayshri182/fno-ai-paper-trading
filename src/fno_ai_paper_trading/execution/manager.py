"""Controlled live execution test orchestrator (WS 7.9).

:class:`LiveExecutionTestManager` implements tomorrow's single real F&O
experiment end-to-end along the required chain:

    signal (existing MA5/21 trend) -> RiskPreflight (RiskManager + Watchdog +
    market session + margin + overnight guard) -> LIVE_EXECUTION_TEST gate ->
    Upstox ExecutionAdapter -> order ack -> fill confirmation -> 5-minute hold
    -> exit -> position reconciliation -> audit/alerts -> HALT/PAPER.

It mechanically refuses to place any order when a preflight fails, when the
gate is closed on a real-send path, or when the operator has not explicitly
confirmed live enablement. It never trades the paper session and never touches
``PaperBroker``.

Two independent outcomes are reported (never conflated):

* **Outcome A** — execution integration test PASS/FAIL (broker plumbing), and
* **Outcome B** — Algorithm Health, which stays RED/NO and is never changed by
  this test.

All stages, acknowledgements and errors are recorded by a secret-free audit.
"""
from __future__ import annotations

import time as _time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Callable, Sequence

from fno_ai_paper_trading.alerting.alerts import (
    PAPER_TRADING_LABEL,
    Alert,
    AlertCategory,
    AlertLevel,
)
from fno_ai_paper_trading.alerting.engine import AlertEngine
from fno_ai_paper_trading.config.settings import LiveExecutionTestSettings, PaperSettings
from fno_ai_paper_trading.data.market_hours import NSE_TZ, market_session
from fno_ai_paper_trading.execution.audit import ExecutionAudit, new_run_id
from fno_ai_paper_trading.execution.gate import (
    ExecutionMode,
    GateDecision,
    LiveExecutionTestGate,
)
from fno_ai_paper_trading.execution.instrument import estimate_required_margin
from fno_ai_paper_trading.execution.risk import RiskPreflight
from fno_ai_paper_trading.execution.signal import CallPutDecision, CallPutSignal, decide_call_put
from fno_ai_paper_trading.execution.state import (
    ExecutionTestState,
    ExecutionTestStateMachine,
)
from fno_ai_paper_trading.execution.upstox import ExecutionAck, ExecutionAdapter
from fno_ai_paper_trading.models.enums import OrderSide, OrderStatus
from fno_ai_paper_trading.models.order import Order
from fno_ai_paper_trading.strategies.base import Strategy
from fno_ai_paper_trading.utils.functions import new_id, positive_int

#: Fixed, explicit note bound to Outcome A so it is never mistaken for profit.
ALGORITHM_HEALTH_NOTE = (
    "UNCHANGED — Algorithm Health remains RED / ALGO READY=NO as attested by the "
    "Algorithm Health monitor. This execution integration test rates broker "
    "plumbing only (Outcome A); it never rates the algorithm (Outcome B)."
)


def _naive_ist_now() -> datetime:
    return datetime.now(NSE_TZ).replace(tzinfo=None)


@dataclass(frozen=True)
class ExecutionTestResult:
    """Complete result of one controlled live execution test run."""

    run_id: str
    mode: ExecutionMode
    dry_run: bool
    outcome: str  # PASS | FAIL
    stage: ExecutionTestState
    reasons: tuple[str, ...] = field(default_factory=tuple)
    entry_order_id: str | None = None
    entry_provider_order_id: str | None = None
    entry_fill_price: Decimal | None = None
    exit_order_id: str | None = None
    exit_provider_order_id: str | None = None
    exit_fill_price: Decimal | None = None
    position_flat: bool = False
    started_at: str = ""
    ended_at: str = ""
    algorithm_health_note: str = ALGORITHM_HEALTH_NOTE

    @property
    def passed(self) -> bool:
        return self.outcome == "PASS"

    def to_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "mode": self.mode.value,
            "dry_run": self.dry_run,
            "outcome": self.outcome,
            "stage": self.stage.value,
            "reasons": list(self.reasons),
            "entry_order_id": self.entry_order_id,
            "entry_provider_order_id": self.entry_provider_order_id,
            "entry_fill_price": str(self.entry_fill_price) if self.entry_fill_price is not None else None,
            "exit_order_id": self.exit_order_id,
            "exit_provider_order_id": self.exit_provider_order_id,
            "exit_fill_price": str(self.exit_fill_price) if self.exit_fill_price is not None else None,
            "position_flat": self.position_flat,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "algorithm_health_note": self.algorithm_health_note,
            "papertrading_label": PAPER_TRADING_LABEL,
        }


class LiveExecutionTestManager:
    """Runs the one-entry/one-exit live execution test with every safeguard."""

    def __init__(
        self,
        *,
        settings: PaperSettings,
        live_settings: LiveExecutionTestSettings,
        gate: LiveExecutionTestGate,
        adapter: ExecutionAdapter,
        preflight: RiskPreflight,
        signal_strategy: Strategy | None = None,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] = _time.sleep,
        audit: ExecutionAudit | None = None,
        alert_engine: AlertEngine | None = None,
        confirm_live_enablement: bool = False,
        hold_seconds: float | None = None,
        poll_seconds: float | None = None,
        order_timeout_seconds: float | None = None,
        force_one_lot_round_trip: bool = False,
    ) -> None:
        if not isinstance(adapter, ExecutionAdapter):
            raise TypeError("adapter must implement ExecutionAdapter")
        self.settings = settings
        self.live_settings = live_settings
        self.gate = gate
        self.adapter = adapter
        self.preflight = preflight
        self.signal_strategy = signal_strategy
        self.clock = clock if clock is not None else _naive_ist_now
        self.sleep = sleep
        self.confirm_live_enablement = bool(confirm_live_enablement)
        self.force_one_lot_round_trip = bool(force_one_lot_round_trip)
        self.entry_fill_quantity: int | None = None
        self.hold_seconds = (
            float(hold_seconds) if hold_seconds is not None else float(live_settings.hold_seconds)
        )
        self.poll_seconds = (
            float(poll_seconds) if poll_seconds is not None else float(live_settings.poll_seconds)
        )
        self.order_timeout_seconds = (
            float(order_timeout_seconds)
            if order_timeout_seconds is not None
            else float(live_settings.order_timeout_seconds)
        )
        self.audit = audit
        self.alert_engine = alert_engine
        self._audit_enabled = audit is not None
        # Internal mutable run state (reset per run).
        self.state = ExecutionTestStateMachine()
        self.entry_ack: ExecutionAck | None = None
        self.exit_ack: ExecutionAck | None = None
        self.entry_fill_price: Decimal | None = None
        self.exit_fill_price: Decimal | None = None
        self.entry_side: OrderSide | None = None
        self._succeeded = False
        self.alerts: list[Alert] = []

    # ------------------------------------------------------------ run

    def run(
        self,
        instrument,
        *,
        signal_bars: Sequence | None = None,
        side_preference: CallPutSignal | None = None,
    ) -> ExecutionTestResult:
        """Execute one controlled live test against ``instrument``.

        ``signal_bars`` is the chronological underlying series for the trend
        signal (None -> HOLD, and therefore no trade). ``side_preference``
        lets the operator pin CALL/PUT; when None the strategy decides.
        """
        if instrument is None:
            raise TypeError("instrument is required")
        run_id = new_run_id()
        if self.audit is not None:
            # Re-point the audit at the actual run id so every audit line is
            # traceable; the audit carries no secrets either way.
            self.audit.run_id = run_id
        self.state = ExecutionTestStateMachine()
        self.entry_ack = None
        self.exit_ack = None
        self.entry_fill_price = None
        self.exit_fill_price = None
        self.entry_side = None
        self._succeeded = False
        started = self.clock()
        started_iso = started.isoformat(timespec="seconds")

        gate_decision = self.gate.decision()
        real_send = not self.adapter.dry_run

        self._emit_alert(
            level=AlertLevel.INFO,
            category=AlertCategory.SYSTEM,
            title="live execution test started",
            message=(
                f"run {run_id} instrument={instrument.exchange_token} "
                f"dry_run={self.adapter.dry_run} mode={gate_decision.mode.value}"
            ),
        )
        self._record("run_started", run_id=run_id, mode=gate_decision.mode.value,
                     dry_run=self.adapter.dry_run, symbol=instrument.exchange_token,
                     outcome_expected="A=execution integration PASS/FAIL only; B=algorithm health unchanged")

        reasons: list[str] = []
        try:
            self._run_impl(run_id, instrument, gate_decision, signal_bars, side_preference, reasons, started)
        except Exception as exc:  # noqa: BLE001 - any unexpected failure ends the run safely
            reasons.append(f"unexpected failure: {exc}")
            self._try_emergency_flatten(instrument, gate_decision.mode)
            self._to_failed(reasons)

        ended = self.clock().isoformat(timespec="seconds")
        outcome = "PASS" if self._succeeded else "FAIL"
        stage = ExecutionTestState.COMPLETE if self._succeeded else self.state.state
        result = ExecutionTestResult(
            run_id=run_id,
            mode=gate_decision.mode,
            dry_run=self.adapter.dry_run,
            outcome=outcome,
            stage=stage,
            reasons=tuple(reasons),
            entry_order_id=self.entry_ack.order_id if self.entry_ack else None,
            entry_provider_order_id=self.entry_ack.provider_order_id if self.entry_ack else None,
            entry_fill_price=self.entry_fill_price,
            exit_order_id=self.exit_ack.order_id if self.exit_ack else None,
            exit_provider_order_id=self.exit_ack.provider_order_id if self.exit_ack else None,
            exit_fill_price=self.exit_fill_price,
            position_flat=self._is_flat_result(instrument),
            started_at=started_iso,
            ended_at=ended,
        )
        self._record(
            "run_finished",
            outcome=result.outcome,
            stage=self.state.state.value,
            reasons=list(result.reasons),
            position_flat=result.position_flat,
            mode=gate_decision.mode.value,
            dry_run=self.adapter.dry_run,
            note="Outcome A recorded; Outcome B (algorithm health) is unchanged.",
        )
        self._emit_alert(
            level=AlertLevel.WARNING if result.outcome == "FAIL" else AlertLevel.INFO,
            category=AlertCategory.RISK if not result.position_flat else AlertCategory.SYSTEM,
            title=f"live execution test finished: {result.outcome}",
            message=f"run {run_id} stage={result.stage.value} flat={result.position_flat}",
        )
        return result

    # -------------------------------------------------------------- impl

    def _run_impl(
        self,
        run_id: str,
        instrument,
        gate_decision: GateDecision,
        signal_bars: Sequence | None,
        side_preference: CallPutSignal | None,
        reasons: list[str],
        started: datetime,
    ) -> None:
        del run_id, started
        # --- 1. authenticate (any adapter; memory is offline-immediate) ------
        self.state.transition(ExecutionTestState.AUTHENTICATING)
        try:
            account = self.adapter.get_account_id()
        except Exception as exc:  # noqa: BLE001 - auth failure => no order
            reasons.append(f"authentication failed: {exc}")
            self.state.transition(ExecutionTestState.FAILED)
            self._record("auth_failed", error=str(exc))
            return
        self._record("authenticated", account=account)

        # --- 2. instrument re-validation ------------------------------------
        if not instrument.is_option():
            reasons.append("instrument is not an option (CE/PE required)")
            self.state.transition(ExecutionTestState.FAILED)
            return
        if instrument.expiry is None or instrument.lot_size <= 0:
            reasons.append("instrument has no expiry or non-positive lot size")
            self.state.transition(ExecutionTestState.FAILED)
            return
        self.state.transition(ExecutionTestState.INSTRUMENT_VALIDATED)

        # --- 3. trend signal -> CALL/PUT ------------------------------------
        decision = decide_call_put(signal_bars or [], self.signal_strategy)
        # Forced one-lot round trip (WS 7.24B OPTION B): the operator pinned a
        # CALL/PUT leg AND explicitly invoked --force-one-lot-round-trip. We
        # only bypass the HOLD *decision* by using that operator-pinned leg —
        # never by weakening the gate: the gate/* confirm_live_enablement checks
        # below remain mandatory and unchanged.
        forced_leg = (
            side_preference
            if self.force_one_lot_round_trip
            and side_preference is not None
            and side_preference is not CallPutSignal.NONE
            else None
        )
        if decision.leg is CallPutSignal.NONE and forced_leg is None:
            reasons.append(
                "strategy signal is HOLD — no CALL or PUT leg; refusing to trade "
                f"(pinned side would be {side_preference.value if side_preference is not None else 'none'}, "
                f"auto leg {decision.leg.value})"
            )
            self.state.abort()
            self._record("signal_hold", auto_leg=decision.leg.value,
                         pinned=side_preference.value if side_preference else "none",
                         reason="no trade")
            return
        leg = forced_leg if forced_leg is not None else (
            side_preference
            if side_preference is not None and side_preference is not CallPutSignal.NONE
            else decision.leg
        )
        side = leg.order_side
        quantity = positive_int(instrument.lot_size, "quantity")
        self._record("signal_decided", leg=leg.value, side=side.value,
                     quantity=quantity, auto_leg=decision.leg.value,
                     signal_reason=decision.signal_result.reason)

        # --- 4. reference price (option premium) -----------------------------
        try:
            premium = self.adapter.quote(instrument.exchange_token)
        except Exception as exc:  # noqa: BLE001 - no quote => no order
            reasons.append(f"no reference price: {exc}")
            self.state.transition(ExecutionTestState.FAILED)
            return

        # --- 5. preflight (RiskManager + Watchdog + market + margin + night) --
        now = self.clock()
        latest_bar = signal_bars[-1].timestamp if signal_bars else now
        session = market_session(now, tz=NSE_TZ)
        components = {
            "data_provider": bool(signal_bars),
            "broker_credentials": True,
            "execution_adapter": True,
        }
        preflight = self.preflight.evaluate(
            instrument=instrument,
            side=side,
            quantity=quantity,
            reference_price=premium,
            premium=premium,
            margin_fn=lambda p: estimate_required_margin(instrument, p),
            session=session,
            latest_bar_time=latest_bar,
            components=components,
            now=now,
            hold_seconds=self.hold_seconds,
        )
        if not preflight.ok:
            reasons.extend(preflight.reasons)
            self.state.abort()
            self._record("preflight_rejected", reasons=list(preflight.reasons))
            return

        # --- 6. real-send gate (only when this adapter writes for real) -------
        if not self.adapter.dry_run:
            if not self.confirm_live_enablement:
                reasons.append(
                    "LIVE_EXECUTION_TEST real send requires explicit --confirm-live-enablement"
                )
                self.state.abort()
                return
            if not gate_decision.ok:
                reasons.extend(
                    f"gate closed: {r}" for r in gate_decision.reasons
                )
                self.state.abort()
                return

        # --- 7. entry (exactly one) ------------------------------------------
        entry_order = Order(
            instrument=instrument, side=side, quantity=quantity, order_id=new_id("ORD")
        )
        try:
            self.entry_ack = self.adapter.place_order(entry_order, mode=gate_decision.mode)
        except Exception as exc:  # noqa: BLE001 - a failed entry => no position
            reasons.append(f"entry order failed: {exc}")
            self.state.transition(ExecutionTestState.FAILED)
            return
        self.state.give_entry(self.entry_ack.order_id)
        self.entry_side = side
        self.state.transition(ExecutionTestState.ENTRY_ACKNOWLEDGED)
        self._record("entry_ack", order_id=self.entry_ack.order_id,
                     provider_order_id=self.entry_ack.provider_order_id,
                     dry_run=self.entry_ack.dry_run, side=side.value)

        # --- 8. entry fill (with timeout; cancel on timeout) ------------------
        entry_status, entry_detail = self._wait_for_fill(
            self.entry_ack, quantity, "entry", reasons
        )
        if entry_status is not OrderStatus.FILLED:
            self._record("entry_not_filled", status=entry_status.value)
            return
        self.entry_fill_price = self._fill_price(entry_detail, premium)
        self.entry_fill_quantity = self._filled_quantity(entry_detail, quantity)
        self.state.transition(ExecutionTestState.ENTRY_FILLED)
        self.state.transition(ExecutionTestState.HOLDING)
        self._record("entry_filled", fill_price=str(self.entry_fill_price))

        # --- 9. mandatory 5-minute hold ---------------------------------------
        self.sleep(self.hold_seconds)
        self._record("hold_completed", hold_seconds=self.hold_seconds)

        # --- 10. exit (exactly one) -------------------------------------------
        # WS 7.24B OPTION B (--force-one-lot-round-trip): SELL exactly the
        # *actual* quantity that the broker filled on entry (may be a partial
        # fill); never the nominal lot when a forced round trip is in effect.
        exit_quantity = (
            self.entry_fill_quantity
            if self.force_one_lot_round_trip and self.entry_fill_quantity is not None
            else quantity
        )
        exit_side = OrderSide.SELL if side is OrderSide.BUY else OrderSide.BUY
        exit_order = Order(
            instrument=instrument, side=exit_side, quantity=exit_quantity, order_id=new_id("ORD")
        )
        try:
            self.exit_ack = self.adapter.place_order(exit_order, mode=gate_decision.mode)
        except Exception as exc:  # noqa: BLE001 - failed exit -> mark failed
            reasons.append(f"exit order failed: {exc}")
            self.state.transition(ExecutionTestState.FAILED)
            return
        self.state.give_exit(self.exit_ack.order_id)
        self.state.transition(ExecutionTestState.EXIT_ACKNOWLEDGED)
        self._record("exit_ack", order_id=self.exit_ack.order_id,
                     provider_order_id=self.exit_ack.provider_order_id,
                     dry_run=self.exit_ack.dry_run, side=exit_side.value)

        exit_status, exit_detail = self._wait_for_fill(
            self.exit_ack, exit_quantity, "exit", reasons
        )
        if exit_status is not OrderStatus.FILLED:
            self._record("exit_not_filled", status=exit_status.value)
            return
        self.exit_fill_price = self._fill_price(exit_detail, premium)
        self.state.transition(ExecutionTestState.EXIT_FILLED)
        self._record("exit_filled", fill_price=str(self.exit_fill_price))

        # --- 11. reconciliation: final position must be FLAT ------------------
        if not self._is_flat_result(instrument):
            reasons.append(
                "reconciliation failed: broker positions are not FLAT after exit"
            )
            self.state.transition(ExecutionTestState.FAILED)
            self._emit_alert(
                level=AlertLevel.CRITICAL,
                category=AlertCategory.RISK,
                title="reconciliation: position is NOT FLAT",
                message=f"exit completed but {instrument.exchange_token} is still open; "
                        "manual broker-side investigation required",
            )
            return
        self.state.transition(ExecutionTestState.FLAT_RECONCILED)
        self.state.transition(ExecutionTestState.COMPLETE)
        self._succeeded = True
        self._record("reconciled", flat=True)

    # -------------------------------------------------------------- helpers

    def _wait_for_fill(
        self,
        ack: ExecutionAck,
        quantity: int,
        leg: str,
        reasons: list[str],
    ) -> tuple[OrderStatus, dict[str, object]]:
        # Dry-run adapters fabricate the ack only; synthesize the fill locally.
        if self.adapter.dry_run:
            self._record(f"{leg}_dry_fill", order_id=ack.order_id)
            return OrderStatus.FILLED, {
                "status": OrderStatus.FILLED.value,
                "filled_quantity": quantity,
                "average_price": None,
            }

        deadline = self.clock() + timedelta(seconds=self.order_timeout_seconds)
        last_status = OrderStatus.SUBMITTED
        last_detail: dict[str, object] = {}
        while True:
            try:
                last_status, last_detail = self.adapter.get_order_status(
                    ack.order_id, quantity
                )
            except Exception as exc:  # noqa: BLE001 - poll failure => treat as unfilled
                last_status = OrderStatus.SUBMITTED
                last_detail = {"error": str(exc)}
            if last_status is OrderStatus.FILLED:
                return last_status, last_detail
            if last_status in (OrderStatus.REJECTED, OrderStatus.CANCELLED):
                reasons.append(f"{leg} order {ack.order_id} ended {last_status.value}: {last_detail}")
                self.state.transition(ExecutionTestState.FAILED)
                return last_status, last_detail
            if self.clock() > deadline:
                reasons.append(f"{leg} order {ack.order_id} fill timed out")
                cancelled = False
                try:
                    cancelled = self.adapter.cancel_order(ack.order_id)
                except Exception:  # noqa: BLE001 - best-effort cancel
                    cancelled = False
                self._record(f"{leg}_fill_timeout", order_id=ack.order_id,
                             cancel_confirmed=cancelled)
                self.state.transition(ExecutionTestState.FAILED)
                return last_status, last_detail
            self.sleep(self.poll_seconds)

    @staticmethod
    def _fill_price(detail: dict[str, object], fallback: Decimal) -> Decimal:
        try:
            value = detail.get("average_price")
            return Decimal(str(value)) if value is not None else fallback
        except Exception:  # noqa: BLE001 - malformed price never blocks a run
            return fallback

    def _filled_quantity(self, detail: dict[str, object], fallback: int) -> int:
        try:
            value = detail.get("filled_quantity")
            return int(str(value)) if value is not None else fallback
        except Exception:  # noqa: BLE001 - malformed fill never blocks a run
            return fallback

    def _try_emergency_flatten(self, instrument, mode: ExecutionMode) -> None:
        """Best-effort exit if the run broke after an entry; never re-entries."""
        if self.entry_ack is None or self.exit_ack is not None:
            return
        if self.state.state not in (
            ExecutionTestState.ENTRY_FILLED,
            ExecutionTestState.HOLDING,
            ExecutionTestState.EXIT_REQUESTED,
        ):
            return
        if self.entry_side is None:
            return
        try:
            exit_side = (
                OrderSide.SELL if self.entry_side is OrderSide.BUY else OrderSide.BUY
            )
            self.exit_ack = self.adapter.place_order(
                Order(instrument=instrument, side=exit_side, quantity=instrument.lot_size),
                mode=mode,
            )
            self._record("emergency_exit_attempted",
                         order_id=self.exit_ack.order_id, ok=True)
        except Exception as exc:  # noqa: BLE001 - best effort only
            self._record("emergency_exit_failed", error=str(exc))

    def _to_failed(self, reasons: list[str]) -> None:
        if not self.state.is_terminal:
            try:
                self.state.fail()
            except ValueError:
                pass

    def _is_flat_result(self, instrument) -> bool:
        try:
            return self.adapter.is_flat(instrument.exchange_token)
        except Exception:  # noqa: BLE001 - unknown position state is not flat
            return False

    # ------------------------------------------------------------ plumbing

    def _record(self, kind: str, **fields: object) -> None:
        if self.audit is not None:
            self.audit.record(kind, **fields)

    def _emit_alert(
        self,
        *,
        level: AlertLevel,
        category: AlertCategory,
        title: str,
        message: str,
    ) -> None:
        if self.alert_engine is None:
            return
        alert = Alert(
            category=category,
            level=level,
            title=title,
            message=message,
            source="execution-test",
            environment=PAPER_TRADING_LABEL,
        )
        self.alert_engine.emit(alert)
        self.alerts.append(alert)