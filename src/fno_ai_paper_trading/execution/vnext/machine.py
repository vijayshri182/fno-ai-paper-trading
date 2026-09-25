"""The vNext option execution state machine (fully isolated).

Synchronous, deterministic, confirmation-gated. Every transition is built from
``plan_orders`` (the pinned table), every order is submitted through the
``Broker`` protocol and every state change is gated on broker ``reconcile()``
results — the machine NEVER assumes a fill.

Failure model
-------------
Exit-side failures (close rejected/unfilled, reconciliation not flat) put the
machine into a hard SAFETY_STOP; the opposite leg is never opened. Entry-side
failures after a confirmed close leave the machine FLAT (reversal records the
failure) ONLY when the broker reconciles flat; any disagreement or unsafe
snapshot is a SAFETY_STOP — never a phantom position claim. Idempotency:
repeated identical signals while a transition is in flight do not submit
duplicate orders; a deterministic ``transition_key`` identifies each accepted
signal.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from fno_ai_paper_trading.execution.vnext.broker import Broker
from fno_ai_paper_trading.execution.vnext.contract import (
    ContractResolver,
    MarketContext,
)
from fno_ai_paper_trading.execution.vnext.enums import (
    ContractType,
    OrderAction,
    OrderStatus,
    PositionState,
    SafetyStopReason,
    SignalDirection,
    TransitionStatus,
    TxSide,
)
from fno_ai_paper_trading.execution.vnext.errors import (
    ContractResolutionError,
    InvalidOrderSemanticsError,
    ReconciliationError,
    SingleSlotViolationError,
    VNextError,
)
from fno_ai_paper_trading.execution.vnext.guards import SingleSlotGuard
from fno_ai_paper_trading.execution.vnext.mapping import OrderStep, plan_orders
from fno_ai_paper_trading.execution.vnext.order_request import (
    build_order_request,
)
from fno_ai_paper_trading.execution.vnext.state import (
    position_to_contract_type,
    reconcile_to_state,
)


@dataclass(frozen=True)
class TransitionResult:
    """Outcome of processing one signal."""

    status: TransitionStatus
    signal: SignalDirection
    state: PositionState
    order_ids: tuple[str, ...] = ()
    safety_stop_reason: SafetyStopReason | None = None
    message: str = ""


@dataclass
class TransitionInfo:
    """In-flight transition bookkeeping (for reversals and idempotency)."""

    signal: SignalDirection
    position_before: PositionState
    position_target: PositionState
    steps: tuple[OrderStep, ...]
    current_step: int = 0
    close_order_id: str | None = None
    open_order_id: str | None = None
    quantity: int = 0


@runtime_checkable
class Machine(Protocol):
    """The state machine surface used by the isolated tests."""

    @property
    def state(self) -> PositionState:
        ...

    def on_signal(self, direction: SignalDirection) -> TransitionResult:
        ...


class VNextOptionExecutionMachine:
    """Confirmation-gated single-slot option execution machine.

    ``market_context`` is optional; a real resolver uses it to pick expiry and
    strike. Quantity defaults to the resolved contract's lot size.
    """

    def __init__(
        self,
        *,
        broker: Broker,
        resolver: ContractResolver,
        guard: SingleSlotGuard,
        market_context: MarketContext | None = None,
        quantity: int | None = None,
    ) -> None:
        self._broker = broker
        self._resolver = resolver
        self._guard = guard
        self._context = market_context or MarketContext()
        self._quantity = quantity
        self._state: PositionState = PositionState.FLAT
        self._transition: TransitionInfo | None = None
        self._last_key: str | None = None

        # Reconcile against the broker immediately — never assume anything.
        self._adopt_broker_position()

    def _adopt_broker_position(self) -> None:
        try:
            decision = reconcile_to_state(self._broker.reconcile())
        except Exception as exc:
            raise ReconciliationError(
                f"initial broker snapshot unavailable/malformed: {exc!r}"
            ) from exc
        if decision.safety_stop is not None:
            raise ReconciliationError(
                f"initial broker snapshot unsafe: {decision.safety_stop.value}"
            )
        self._state = decision.position
        if decision.position is not PositionState.FLAT:
            contract_type = position_to_contract_type(decision.position)
            try:
                qty = self._broker_quantity(contract_type)
            except Exception as exc:
                raise ReconciliationError(
                    f"initial broker position quantity unavailable: {exc!r}"
                ) from exc
            self._guard.record_open(contract_type, qty)

    def _broker_quantity(self, contract_type) -> int:
        snapshot = self._broker.reconcile()
        return sum(
            d.quantity
            for d in snapshot.details
            if d.contract.contract_type is contract_type
        )

    @property
    def state(self) -> PositionState:
        return self._state

    @property
    def in_transition(self) -> bool:
        return self._transition is not None

    @property
    def transition(self) -> TransitionInfo | None:
        return self._transition

    @property
    def transition_key(self) -> str | None:
        """Deterministic idempotency identity for the last accepted signal."""
        return self._last_key

    def _compute_key(self, direction: SignalDirection) -> str:
        return f"{self._state.value}:{direction.value}"

    def on_signal(self, direction: SignalDirection) -> TransitionResult:
        # Idempotency: a repeated signal while a transition is in flight must
        # not duplicate orders — re-poll the same in-flight gates instead. A
        # NEW different signal mid-transition is refused.
        if self._transition is not None:
            if direction is self._transition.signal:
                return self._poll_in_flight()
            raise SingleSlotViolationError(
                "machine is mid-transition; new signal not accepted"
            )

        key = self._compute_key(direction)

        steps, target = plan_orders(self._state, direction)
        if not steps:
            self._last_key = key
            return TransitionResult(TransitionStatus.HELD, direction, self._state)

        self._transition = TransitionInfo(
            signal=direction,
            position_before=self._state,
            position_target=target,
            steps=tuple(steps),
        )
        self._last_key = key
        return self._run_transition()

    def _poll_in_flight(self) -> TransitionResult:
        return self._run_transition()

    def _run_transition(self) -> TransitionResult:
        """Run (or re-poll) the in-flight transition under the failure policy.

        - ``SingleSlotViolationError``: the transition never submitted an order
          (another run won the slot / a different signal was refused). The
          in-flight bookkeeping is discarded so the run can retry later, and the
          error re-raised.
        - ``VNextError``: a vNext-level failure mid-transition (resolution,
          reconciliation, broker). Converted to a hard SAFETY_STOP with
          best-effort cleanup — never auto-recovery, never a phantom position.
        - Unknown exceptions: best-effort cleanup, discard the in-flight
          transition, and re-raise so the caller sees the raw failure.
        """
        try:
            return self._advance_transition()
        except SingleSlotViolationError:
            self._transition = None
            raise
        except VNextError as exc:
            transition = self._transition
            if transition is None:
                raise
            return self._safety_abort(transition, self._reason_for(exc))
        except Exception:
            transition = self._transition
            if transition is not None:
                self._release_if_flat()
                self._transition = None
            raise

    def _reason_for(self, exc: Exception) -> SafetyStopReason:
        if isinstance(exc, ContractResolutionError) or isinstance(
            exc, InvalidOrderSemanticsError
        ):
            return SafetyStopReason.RESOLUTION_FAILED
        if isinstance(exc, ReconciliationError):
            return SafetyStopReason.RECONCILIATION_DISAGREEMENT
        return SafetyStopReason.BROKER_UNAVAILABLE

    def _release_if_flat(self) -> None:
        """Best-effort: release the slot reservation if the broker is flat.

        If the broker cannot be reconciled or holds any position, the
        reservation is KEPT (conservative — no auto-release that could allow a
        second run to open against a real position).
        """
        try:
            decision = reconcile_to_state(self._broker.reconcile())
        except Exception:
            return
        if decision.safety_stop is None and decision.position is PositionState.FLAT:
            self._guard.record_close()

    def _safety_abort(
        self,
        transition: TransitionInfo,
        reason: SafetyStopReason,
        *,
        order_ids: tuple[str, ...] | None = None,
    ) -> TransitionResult:
        """Terminate a transition in a hard safety stop.

        Slot reservation is kept unless the broker reconciles flat; the final
        state mirrors the broker when reconcile is determinate, otherwise the
        machine's last known state. No opposite leg is ever opened.
        """
        decision = None
        try:
            decision = reconcile_to_state(self._broker.reconcile())
        except Exception:
            decision = None
        self._transition = None
        if decision is not None:
            if (
                decision.safety_stop is None
                and decision.position is PositionState.FLAT
            ):
                self._guard.record_close()
                self._state = PositionState.FLAT
            elif decision.safety_stop is None:
                self._state = decision.position
        if order_ids is None:
            order_ids = tuple(
                order_id
                for order_id in (
                    transition.close_order_id,
                    transition.open_order_id,
                )
                if order_id is not None
            )
        return TransitionResult(
            TransitionStatus.SAFETY_STOP,
            transition.signal,
            self._state,
            order_ids=order_ids,
            safety_stop_reason=reason,
            message=f"mid-transition failure ({reason.value}); no auto-recovery",
        )

    def _advance_transition(self) -> TransitionResult:
        transition = self._transition
        assert transition is not None

        if transition.close_order_id is not None:
            return self._verify_close(transition, transition.close_order_id)
        if transition.open_order_id is not None:
            return self._verify_open(transition, transition.open_order_id)

        step = transition.steps[transition.current_step]
        result = self._submit_step(transition, step)
        if result is not None:
            return result
        return self._advance_transition()

    def _submit_step(
        self, transition: TransitionInfo, step: OrderStep
    ) -> TransitionResult | None:
        contract = self._resolver.resolve(step.option_leg, self._context)
        if (
            contract.option_leg is not step.option_leg
            or contract.contract_type is not step.contract_type
        ):
            raise ContractResolutionError(
                "resolved contract "
                f"{contract.option_leg.value}/{contract.contract_type.value} "
                "does not match the step "
                f"{step.option_leg.value}/{step.contract_type.value}"
            )
        if self._context.as_of is not None and contract.expiry < self._context.as_of:
            raise ContractResolutionError(
                "resolved contract already expired relative to context "
                f"({contract.expiry.isoformat()} < "
                f"{self._context.as_of.isoformat()})"
            )
        quantity = self._step_quantity(step, contract)
        transition.quantity = quantity
        request = build_order_request(step, contract, quantity)

        if step.order_action is OrderAction.OPEN:
            self._guard.check_can_open()
            self._guard.record_open(step.contract_type, quantity)
            # Pre-entry reconciliation: never open INTO a broker that already
            # holds a position (or snaps an unsafe/ambiguous state). Mirrors the
            # close-side guard (a SELL is only sized from the broker-held
            # quantity) — a BUY is only placed from a broker-confirmed FLAT.
            pre = reconcile_to_state(self._broker.reconcile())
            if pre.safety_stop is not None:
                raise ReconciliationError(
                    "cannot open into an unsafe broker snapshot: "
                    f"{pre.safety_stop.value}"
                )
            if pre.position is not PositionState.FLAT:
                raise ReconciliationError(
                    "cannot open while the broker holds a position "
                    f"({pre.position.value})"
                )
        ticket = self._broker.submit_order(request)

        if step.order_action is OrderAction.OPEN:
            transition.open_order_id = ticket.order_id
            return self._after_open_submit(transition, ticket.status)
        transition.close_order_id = ticket.order_id
        return self._after_close_submit(transition, ticket.status)

    def _step_quantity(self, step: OrderStep, contract) -> int:
        """Size an order against broker-confirmed reality.

        Open steps use the configured quantity or the resolved lot size. Close
        steps use the quantity the broker actually reports for the held leg —
        never a hardcoded assumption — so a partial position (e.g. after a
        restart or a partial fill) is closed exactly and can never over-sell
        into a short.
        """
        if step.order_action is not OrderAction.CLOSE:
            return self._quantity or max(1, contract.lot_size)
        snapshot = self._broker.reconcile()
        decision = reconcile_to_state(snapshot)
        if decision.safety_stop is not None:
            raise ReconciliationError(
                "cannot size a close against an unsafe snapshot: "
                f"{decision.safety_stop.value}"
            )
        held = sum(
            detail.quantity
            for detail in snapshot.details
            if detail.contract.contract_type is step.contract_type
        )
        if held <= 0:
            raise ReconciliationError(
                f"close planned for {step.contract_type.value} but broker "
                "reports no held quantity"
            )
        return held

    def _after_close_submit(
        self, transition: TransitionInfo, status: OrderStatus
    ) -> TransitionResult | None:
        if status is OrderStatus.FILLED:
            return self._verify_close(transition, transition.close_order_id)
        if status in (OrderStatus.REJECTED, OrderStatus.CANCELLED):
            return self._fail_exit(transition, SafetyStopReason.CLOSE_REJECTED)
        if status is OrderStatus.UNFILLED:
            return self._fail_exit(transition, SafetyStopReason.CLOSE_UNFILLED)
        return None  # PENDING/PARTIALLY_FILLED: keep polling

    def _after_open_submit(
        self, transition: TransitionInfo, status: OrderStatus
    ) -> TransitionResult | None:
        if status is OrderStatus.FILLED:
            return self._verify_open(transition, transition.open_order_id)
        if status in (OrderStatus.REJECTED, OrderStatus.CANCELLED):
            return self._fail_entry(transition, SafetyStopReason.ENTRY_REJECTED)
        if status is OrderStatus.UNFILLED:
            return self._fail_entry(transition, SafetyStopReason.ENTRY_UNFILLED)
        return None

    def _verify_close(
        self, transition: TransitionInfo, order_id: str
    ) -> TransitionResult:
        record = self._broker.get_order_status(order_id)
        if record.status is not OrderStatus.FILLED:
            if record.status in (OrderStatus.REJECTED, OrderStatus.CANCELLED):
                return self._fail_exit(transition, SafetyStopReason.CLOSE_REJECTED)
            if record.status is OrderStatus.UNFILLED:
                return self._fail_exit(transition, SafetyStopReason.CLOSE_UNFILLED)
            # PENDING/PARTIALLY_FILLED: still in flight — machine stops for
            # this cycle and will NOT open the opposite leg.
            return TransitionResult(
                TransitionStatus.STOPPED,
                transition.signal,
                self._state,
                order_ids=(order_id,),
                message="close not yet confirmed; opposite leg NOT opened",
            )

        # Close FILLED: reconcile the BROKER position (never assume).
        decision = reconcile_to_state(self._broker.reconcile())
        if decision.safety_stop is not None:
            self._transition = None
            return TransitionResult(
                TransitionStatus.SAFETY_STOP,
                transition.signal,
                self._state,
                order_ids=(order_id,),
                safety_stop_reason=decision.safety_stop,
                message=f"post-close reconcile unsafe: {decision.safety_stop.value}",
            )
        if decision.position is not PositionState.FLAT:
            # The fill report and reconciliation disagree: hard stop, no
            # phantom flat claim, opposite leg NOT opened.
            self._state = decision.position
            self._transition = None
            return TransitionResult(
                TransitionStatus.SAFETY_STOP,
                transition.signal,
                self._state,
                order_ids=(order_id,),
                safety_stop_reason=SafetyStopReason.RECONCILIATION_NOT_FLAT,
                message="close filled but broker position not flat; "
                "opposite leg NOT opened",
            )

        self._state = PositionState.FLAT
        if transition.current_step + 1 >= len(transition.steps):
            # Final close: the slot is released only when the position closes.
            self._guard.record_close()
        # During a reversal the reservation is carried (not released) so the
        # holder keeps the slot continuously until the opposite leg is opened
        # or the reversal fails — no TOCTOU gap for another run.
        transition.close_order_id = None
        transition.current_step += 1

        if transition.current_step < len(transition.steps):
            return self._advance_transition()  # reversal: open opposite leg
        self._transition = None
        return TransitionResult(
            TransitionStatus.COMPLETED, transition.signal, PositionState.FLAT
        )

    def _verify_open(
        self, transition: TransitionInfo, order_id: str
    ) -> TransitionResult:
        record = self._broker.get_order_status(order_id)
        if record.status is not OrderStatus.FILLED:
            if record.status in (OrderStatus.REJECTED, OrderStatus.CANCELLED):
                return self._fail_entry(transition, SafetyStopReason.ENTRY_REJECTED)
            if record.status is OrderStatus.UNFILLED:
                return self._fail_entry(transition, SafetyStopReason.ENTRY_UNFILLED)
            return TransitionResult(
                TransitionStatus.STOPPED,
                transition.signal,
                self._state,
                order_ids=(order_id,),
                message="entry not yet confirmed",
            )

        # Entry FILLED: confirm the intended position actually materialised.
        step = transition.steps[transition.current_step]
        expected = (
            PositionState.LONG_CALL
            if step.contract_type is ContractType.CE
            else PositionState.LONG_PUT
        )
        decision = reconcile_to_state(self._broker.reconcile())
        if decision.safety_stop is not None:
            self._transition = None
            return TransitionResult(
                TransitionStatus.SAFETY_STOP,
                transition.signal,
                self._state,
                order_ids=(order_id,),
                safety_stop_reason=decision.safety_stop,
                message=f"post-entry reconcile unsafe: {decision.safety_stop.value}",
            )
        if decision.position is expected:
            self._state = expected
            self._transition = None
            return TransitionResult(
                TransitionStatus.COMPLETED,
                transition.signal,
                expected,
                order_ids=(order_id,),
            )

        # Order reported FILLED but the broker position disagrees (phantom
        # fill, wrong leg, or flat). Hard safety stop: no position is claimed
        # and the slot reservation is kept until an explicit decision.
        self._state = decision.position
        self._transition = None
        return TransitionResult(
            TransitionStatus.SAFETY_STOP,
            transition.signal,
            self._state,
            order_ids=(order_id,),
            safety_stop_reason=SafetyStopReason.RECONCILIATION_DISAGREEMENT,
            message="entry reported FILLED but reconciliation disagrees; "
            "no position claimed",
        )

    def _fail_exit(
        self, transition: TransitionInfo, reason: SafetyStopReason
    ) -> TransitionResult:
        """Exit-side failure — always a hard safety stop, never the opposite leg.

        The final state is derived from a fresh broker reconciliation: if the
        broker now reconciles flat the slot is released and the state is FLAT;
        if it still holds a position the state mirrors the position and the
        reservation is kept. No phantom hold and no phantom flat.
        """
        decision = reconcile_to_state(self._broker.reconcile())
        self._transition = None
        if decision.safety_stop is not None:
            return TransitionResult(
                TransitionStatus.SAFETY_STOP,
                transition.signal,
                self._state,
                safety_stop_reason=decision.safety_stop,
                message=f"exit failed ({reason.value}); reconcile unsafe: "
                f"{decision.safety_stop.value}",
            )
        if decision.position is PositionState.FLAT:
            self._guard.record_close()
            self._state = PositionState.FLAT
            return TransitionResult(
                TransitionStatus.SAFETY_STOP,
                transition.signal,
                PositionState.FLAT,
                safety_stop_reason=reason,
                message=f"exit failed ({reason.value}); reconcile confirms "
                "flat — final state FLAT, slot released",
            )
        stop_reason = (
            SafetyStopReason.RECONCILIATION_DISAGREEMENT
            if decision.position is not transition.position_before
            else reason
        )
        self._state = decision.position
        return TransitionResult(
            TransitionStatus.SAFETY_STOP,
            transition.signal,
            self._state,
            safety_stop_reason=stop_reason,
            message=f"exit failed ({reason.value}); position retained",
        )

    def _fail_entry(
        self, transition: TransitionInfo, reason: SafetyStopReason
    ) -> TransitionResult:
        """Entry failure after a confirmed close.

        Reconcile first: only a broker-confirmed FLAT yields ENTRY_FAILED_FLAT
        (slot released). If the broker holds any position despite the rejected
        entry, or the snapshot is unsafe, the machine hard-stops and keeps the
        reservation — no phantom flat claim.
        """
        decision = reconcile_to_state(self._broker.reconcile())
        self._transition = None
        if decision.safety_stop is not None:
            return TransitionResult(
                TransitionStatus.SAFETY_STOP,
                transition.signal,
                self._state,
                safety_stop_reason=decision.safety_stop,
                message=f"entry failed ({reason.value}); reconcile unsafe: "
                f"{decision.safety_stop.value}",
            )
        if decision.position is not PositionState.FLAT:
            self._state = decision.position
            return TransitionResult(
                TransitionStatus.SAFETY_STOP,
                transition.signal,
                self._state,
                safety_stop_reason=SafetyStopReason.RECONCILIATION_DISAGREEMENT,
                message="entry failed but broker holds a position; "
                "no phantom flat claim",
            )
        self._guard.record_close()
        self._state = PositionState.FLAT
        return TransitionResult(
            TransitionStatus.ENTRY_FAILED_FLAT,
            transition.signal,
            PositionState.FLAT,
            safety_stop_reason=reason,
            message=f"entry failed after confirmed close ({reason.value}); "
            "final state FLAT",
        )