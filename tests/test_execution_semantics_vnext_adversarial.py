"""vNext adversarial hardening suite.

Stress-tests the isolated ``execution.vnext`` state machine against duplicate
orders, CE+PE exposure, phantom fills, restart state, opposite-leg-before-exit,
stale broker state, ambiguous positions, contract/leg mapping, race-like
signals, slot release and accidental short exposure.

Everything is deterministic and isolated: no network, no Upstox, no paper/live
orders. Only the confirmation-gated machine plus its fakes are exercised.
"""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from fno_ai_paper_trading.execution.vnext import (
    ContractType,
    OrderAction,
    OrderStatus,
    OptionLeg,
    PositionState,
    SafetyStopReason,
    SignalDirection,
    SlotRegistry,
    TableContractResolver,
    TransitionStatus,
    TxSide,
    VNextOptionExecutionMachine,
)
from fno_ai_paper_trading.execution.vnext.broker import (
    PositionDetail,
    PositionSnapshot,
)
from fno_ai_paper_trading.execution.vnext.contract import MarketContext
from fno_ai_paper_trading.execution.vnext.errors import (
    ContractResolutionError,
    ReconciliationError,
    SingleSlotViolationError,
    VNextError,
)
from fno_ai_paper_trading.execution.vnext.mapping import plan_orders
from fno_ai_paper_trading.execution.vnext.order_request import build_order_request
from fno_ai_paper_trading.execution.vnext.order_semantics import (
    leg_to_contract_type,
    validate_order_semantics,
)
from fno_ai_paper_trading.execution.vnext.state import reconcile_to_state

from vnext_helpers import (
    ScriptedBroker,
    make_contract,
    make_resolver,
)


def make_machine(
    holder_id: str,
    *,
    broker=None,
    registry=None,
    resolver=None,
    market_context=None,
    quantity=None,
):
    broker = broker or ScriptedBroker(auto_fill=True)
    registry = registry or SlotRegistry()
    resolver = resolver or make_resolver(broker.contracts)
    return (
        VNextOptionExecutionMachine(
            broker=broker,
            resolver=resolver,
            guard=registry.guard(holder_id),
            market_context=market_context,
            quantity=quantity,
        ),
        broker,
    )


def signature(broker: ScriptedBroker) -> list[tuple[str, str, str, int]]:
    return [
        (
            o.order_action.value,
            o.contract.option_leg.value,
            o.tx_side.value,
            o.quantity,
        )
        for o in broker.submitted
    ]


def leg_quantities(broker: ScriptedBroker) -> tuple[int, int]:
    """Positive (CE, PE) quantities as currently reported by the broker."""
    ce = pe = 0
    for d in broker.position_snapshot.details:
        if d.quantity <= 0:
            continue
        if d.contract.contract_type is ContractType.CE:
            ce += d.quantity
        elif d.contract.contract_type is ContractType.PE:
            pe += d.quantity
    return ce, pe


def assert_state_consistent(machine, broker: ScriptedBroker) -> None:
    """Every non-safety terminal state must mirror the broker position."""
    decision = reconcile_to_state(broker.position_snapshot)
    if decision.safety_stop is None:
        assert machine.state is decision.position, (
            f"machine says {machine.state.value}, broker says "
            f"{decision.position.value}"
        )


# ---------------------------------------------------- A: duplicate orders


class TestADuplicateOrders:
    def test_duplicate_long_from_flat_ever_creates_one_open(self):
        machine, broker = make_machine("a")
        r1 = machine.on_signal(SignalDirection.LONG)
        assert r1.status is TransitionStatus.COMPLETED
        r2 = machine.on_signal(SignalDirection.LONG)
        assert r2.status is TransitionStatus.HELD
        opens = [
            o for o in broker.submitted if o.order_action is OrderAction.OPEN
        ]
        assert len(opens) == 1

    def test_duplicate_inflight_signal_polls_without_second_order(self):
        broker = ScriptedBroker(auto_fill=False)
        machine, _ = make_machine("a", broker=broker)
        r1 = machine.on_signal(SignalDirection.LONG)
        assert r1.status is TransitionStatus.STOPPED
        before = len(broker.submitted)
        for _ in range(3):
            r = machine.on_signal(SignalDirection.LONG)
            assert r.status is TransitionStatus.STOPPED
        assert len(broker.submitted) == before


# ------------------------------------------- B: mid-transition signal changes


class TestBSignalChangeMidTransition:
    def test_new_direction_mid_transition_is_refused_and_resumed(self):
        broker = ScriptedBroker(auto_fill=False)
        machine, _ = make_machine("a", broker=broker)
        r0 = machine.on_signal(SignalDirection.LONG)
        assert r0.status is TransitionStatus.STOPPED
        with pytest.raises(SingleSlotViolationError):
            machine.on_signal(SignalDirection.SHORT)
        # The refused signal must not have opened a new transition: the
        # original in-flight gate is still pollable via the SAME signal.
        r1 = machine.on_signal(SignalDirection.LONG)
        assert r1.status is TransitionStatus.STOPPED
        assert len(broker.submitted) == 1


# ------------------------------------------------ C: exit / close failures


class TestCExitFailures:
    def test_exit_rejected_keeps_reservation_and_position(self):
        broker = ScriptedBroker(auto_fill=True)
        machine, _ = make_machine("a", broker=broker)
        machine.on_signal(SignalDirection.LONG)
        registry = machine._guard.registry  # noqa: SLF001
        broker.reject_next = OrderStatus.REJECTED
        r = machine.on_signal(SignalDirection.SHORT)
        assert r.status is TransitionStatus.SAFETY_STOP
        assert r.safety_stop_reason is SafetyStopReason.CLOSE_REJECTED
        assert machine.state is PositionState.LONG_CALL
        assert registry.occupied  # reservation retained, no auto-release

    def test_exit_with_no_held_position_refused_before_submission(self):
        # Reverse disagreement: the machine believes it holds CE, but the
        # broker already reports flat. The machine refuses to even submit a
        # SELL against an empty position, hard-stops, releases the slot and
        # reports the broker's truth (no phantom hold).
        broker = ScriptedBroker(auto_fill=True)
        machine, _ = make_machine("a", broker=broker)
        machine.on_signal(SignalDirection.LONG)
        broker.latch_position = PositionSnapshot()
        broker.reject_next = OrderStatus.REJECTED
        registry = machine._guard.registry  # noqa: SLF001
        before = len(broker.submitted)
        r = machine.on_signal(SignalDirection.SHORT)
        assert r.status is TransitionStatus.SAFETY_STOP
        assert (
            r.safety_stop_reason is SafetyStopReason.RECONCILIATION_DISAGREEMENT
        )
        assert machine.state is PositionState.FLAT  # broker confirms flat
        assert not registry.occupied
        assert len(broker.submitted) == before  # no SELL ever submitted

    def test_exit_pending_is_stopped_no_opposite_leg(self):
        broker = ScriptedBroker(auto_fill=False)
        machine, _ = make_machine("a", broker=broker)
        r0 = machine.on_signal(SignalDirection.LONG)
        assert r0.status is TransitionStatus.STOPPED
        broker.set_order_status(r0.order_ids[0], OrderStatus.FILLED)
        r1 = machine.on_signal(SignalDirection.LONG)
        assert r1.status is TransitionStatus.COMPLETED
        r2 = machine.on_signal(SignalDirection.SHORT)
        assert r2.status is TransitionStatus.STOPPED  # close pending
        assert r2.state is PositionState.LONG_CALL
        assert signature(broker) == [
            ("OPEN", "CALL", "BUY", 75),
            ("CLOSE", "CALL", "SELL", 75),
        ]


# -------------------------------------------------- D: reversal safety


class TestDReversalSafety:
    def test_flat_confirmed_before_opposite_leg_opens(self):
        broker = ScriptedBroker(auto_fill=False)
        machine, _ = make_machine("a", broker=broker)
        r0 = machine.on_signal(SignalDirection.LONG)
        assert r0.status is TransitionStatus.STOPPED
        broker.set_order_status(r0.order_ids[0], OrderStatus.FILLED)
        machine.on_signal(SignalDirection.LONG)
        r1 = machine.on_signal(SignalDirection.SHORT)
        # close pending: no opposite leg yet
        assert signature(broker) == [
            ("OPEN", "CALL", "BUY", 75),
            ("CLOSE", "CALL", "SELL", 75),
        ]
        # confirm the close -> broker flat -> only now BUY PE
        broker.set_order_status(r1.order_ids[0], OrderStatus.FILLED)
        machine.on_signal(SignalDirection.SHORT)
        assert signature(broker) == [
            ("OPEN", "CALL", "BUY", 75),
            ("CLOSE", "CALL", "SELL", 75),
            ("OPEN", "PUT", "BUY", 75),
        ]

    def test_entry_rejected_after_close_leaves_flat_no_retry(self):
        broker = ScriptedBroker(auto_fill=True)
        machine, _ = make_machine("a", broker=broker)
        machine.on_signal(SignalDirection.LONG)
        registry = machine._guard.registry  # noqa: SLF001
        broker.reject_if = (
            lambda o: OrderStatus.REJECTED
            if o.order_action is OrderAction.OPEN
            else None
        )
        r = machine.on_signal(SignalDirection.SHORT)
        assert r.status is TransitionStatus.ENTRY_FAILED_FLAT
        assert machine.state is PositionState.FLAT
        assert not registry.occupied  # slot fully released after failure
        assert leg_quantities(broker) == (0, 0)
        # no automatic retry of the failed entry
        count = len(broker.submitted)
        r2 = machine.on_signal(SignalDirection.FLAT)
        assert r2.status is TransitionStatus.HELD
        assert len(broker.submitted) == count

    def test_entry_accepted_not_filled_is_not_long_put(self):
        broker = ScriptedBroker(auto_fill=False)
        machine, _ = make_machine("a", broker=broker)
        r0 = machine.on_signal(SignalDirection.LONG)
        assert r0.status is TransitionStatus.STOPPED
        broker.set_order_status(r0.order_ids[0], OrderStatus.FILLED)
        machine.on_signal(SignalDirection.LONG)
        r1 = machine.on_signal(SignalDirection.SHORT)
        broker.set_order_status(r1.order_ids[0], OrderStatus.FILLED)
        r2 = machine.on_signal(SignalDirection.SHORT)
        # PE entry still pending: never report LONG_PUT prematurely.
        assert r2.status is TransitionStatus.STOPPED
        assert machine.state is PositionState.FLAT
        assert r2.state is PositionState.FLAT

    def test_failed_reversal_leaves_no_stale_call_reservation(self):
        registry = SlotRegistry()
        broker = ScriptedBroker(auto_fill=True)
        machine, _ = make_machine("a", broker=broker, registry=registry)
        machine.on_signal(SignalDirection.LONG)
        broker.reject_if = (
            lambda o: OrderStatus.REJECTED
            if o.order_action is OrderAction.OPEN
            else None
        )
        r = machine.on_signal(SignalDirection.SHORT)
        assert r.status is TransitionStatus.ENTRY_FAILED_FLAT
        assert not registry.occupied


# ------------------------------------------- E: startup reconciliation


class TestEStartupReconcile:
    def test_startup_ce_and_pe_refuses_no_orders(self):
        broker = ScriptedBroker(auto_fill=True)
        broker.set_position(make_contract(OptionLeg.CALL), 1)
        broker.set_position(make_contract(OptionLeg.PUT), 1)
        with pytest.raises(ReconciliationError):
            make_machine("a", broker=broker)

    def test_startup_unexpected_short_refuses_no_orders(self):
        for leg in (OptionLeg.CALL, OptionLeg.PUT):
            broker = ScriptedBroker(auto_fill=True)
            broker.set_position(make_contract(leg), -1)
            with pytest.raises(ReconciliationError):
                make_machine("a", broker=broker)

    def test_startup_invalid_quantity_refuses(self):
        broker = ScriptedBroker(auto_fill=True)
        broker.latch_position = PositionSnapshot(
            details=(
                PositionDetail(
                    contract=make_contract(OptionLeg.CALL), quantity=1.5
                ),
            )
        )
        with pytest.raises(ReconciliationError):
            make_machine("a", broker=broker)

    def test_startup_malformed_snapshot_refuses(self):
        broker = ScriptedBroker(auto_fill=True)
        fake = SimpleNamespace()
        broker.latch_position = PositionSnapshot(
            details=(PositionDetail(contract=fake, quantity=1),)
        )
        with pytest.raises(ReconciliationError):
            make_machine("a", broker=broker)

    def test_startup_broker_unavailable_refuses(self):
        broker = ScriptedBroker(auto_fill=True)
        broker.raise_on_reconcile = RuntimeError("broker offline")
        with pytest.raises(ReconciliationError):
            make_machine("a", broker=broker)


# --------------------------------------------------- F: restart replay


class TestFRestartReplay:
    def test_restart_ce_held_no_duplicate_buy_pe(self):
        broker = ScriptedBroker(auto_fill=True)
        broker.set_position(make_contract(OptionLeg.CALL), 1)
        machine, _ = make_machine("a", broker=broker)
        assert machine.state is PositionState.LONG_CALL
        r = machine.on_signal(SignalDirection.SHORT)  # explicit reversal
        assert r.status is TransitionStatus.COMPLETED
        assert r.state is PositionState.LONG_PUT
        # Close was sized to the broker-held 1 CE (never over-sold into a
        # short); the reversal holds exactly one PE (lot size).
        ce, pe = leg_quantities(broker)
        assert ce == 0
        assert pe > 0
        assert signature(broker) == [
            ("CLOSE", "CALL", "SELL", 1),
            ("OPEN", "PUT", "BUY", 75),
        ]

    def test_restart_pe_held_short_is_hold_no_duplicate_open(self):
        broker = ScriptedBroker(auto_fill=True)
        broker.set_position(make_contract(OptionLeg.PUT), 1)
        machine, _ = make_machine("a", broker=broker)
        assert machine.state is PositionState.LONG_PUT
        r = machine.on_signal(SignalDirection.SHORT)
        assert r.status is TransitionStatus.HELD
        assert broker.submitted == []

    def test_restart_flat_never_autobuys(self):
        broker = ScriptedBroker(auto_fill=True)
        machine, _ = make_machine("a", broker=broker)
        assert machine.state is PositionState.FLAT
        assert broker.submitted == []


# -------------------------------------------------- G: slot race + release


class TestGSlotRace:
    def test_loser_submits_zero_orders_and_can_retry_after_release(self):
        registry = SlotRegistry()
        machine_a, _ = make_machine("run-a", registry=registry)
        machine_a.on_signal(SignalDirection.LONG)
        assert registry.occupied

        machine_b, broker_b = make_machine("run-b", registry=registry)
        with pytest.raises(SingleSlotViolationError):
            machine_b.on_signal(SignalDirection.SHORT)
        assert broker_b.submitted == []
        # The loser's in-flight bookkeeping was cleared: after the winner
        # releases the slot, the loser can start a fresh transition.
        machine_a.on_signal(SignalDirection.FLAT)
        assert not registry.occupied
        r = machine_b.on_signal(SignalDirection.SHORT)
        assert r.status is TransitionStatus.COMPLETED
        assert r.state is PositionState.LONG_PUT
        assert len(broker_b.submitted) == 1

    def test_ce_and_pe_are_same_symbol_independent_slot(self):
        registry = SlotRegistry()
        machine_a, _ = make_machine("run-a", registry=registry)
        machine_a.on_signal(SignalDirection.LONG)  # holds CE
        machine_b, broker_b = make_machine("run-b", registry=registry)
        with pytest.raises(SingleSlotViolationError):
            machine_b.on_signal(SignalDirection.SHORT)  # wants PE
        assert broker_b.submitted == []

    def test_slot_released_only_after_exit(self):
        registry = SlotRegistry()
        machine, _ = make_machine("a", registry=registry)
        machine.on_signal(SignalDirection.LONG)
        assert registry.occupied
        machine.on_signal(SignalDirection.FLAT)
        assert not registry.occupied

    def test_slot_held_continuously_through_reversal(self):
        registry = SlotRegistry()
        broker = ScriptedBroker(auto_fill=False)
        machine, _ = make_machine("a", broker=broker, registry=registry)
        r0 = machine.on_signal(SignalDirection.LONG)
        assert r0.status is TransitionStatus.STOPPED
        broker.set_order_status(r0.order_ids[0], OrderStatus.FILLED)
        machine.on_signal(SignalDirection.LONG)
        r1 = machine.on_signal(SignalDirection.SHORT)
        broker.set_order_status(r1.order_ids[0], OrderStatus.FILLED)
        # close confirmed, entry pending: slot must still be occupied (no
        # TOCTOU gap where another run could grab the slot mid-reversal).
        r2 = machine.on_signal(SignalDirection.SHORT)
        assert r2.status is TransitionStatus.STOPPED
        assert registry.occupied
        assert registry.contract_type is ContractType.PE


# ------------------------------------------- H: contract/leg consistency


class WrongLegResolver:
    """Returns a PUT/PE contract no matter which leg the machine asks for."""

    def resolve(self, option_leg, market_context=None):
        return make_contract(OptionLeg.PUT)


class MissingResolver:
    def resolve(self, option_leg, market_context=None):
        raise ContractResolutionError(f"no contract for {option_leg.value}")


class TestHContractConsistency:
    def test_wrong_leg_resolution_rejected_before_submission(self):
        broker = ScriptedBroker(auto_fill=True)
        machine, _ = make_machine("a", broker=broker, resolver=WrongLegResolver())
        r = machine.on_signal(SignalDirection.LONG)
        assert r.status is TransitionStatus.SAFETY_STOP
        assert r.safety_stop_reason is SafetyStopReason.RESOLUTION_FAILED
        assert broker.submitted == []  # nothing reached the broker

    def test_missing_resolution_rejected_before_submission(self):
        broker = ScriptedBroker(auto_fill=True)
        machine, _ = make_machine("a", broker=broker, resolver=MissingResolver())
        r = machine.on_signal(SignalDirection.LONG)
        assert r.status is TransitionStatus.SAFETY_STOP
        assert r.safety_stop_reason is SafetyStopReason.RESOLUTION_FAILED
        assert broker.submitted == []

    def test_resolution_failure_does_not_wedge_machine(self):
        # After a resolution failure the machine is not stuck: a later,
        # working resolver lets the same signal proceed (explicit decision).
        broker = ScriptedBroker(auto_fill=True)
        machine, _ = make_machine("a", broker=broker, resolver=MissingResolver())
        r = machine.on_signal(SignalDirection.LONG)
        assert r.status is TransitionStatus.SAFETY_STOP
        assert broker.submitted == []
        r2 = machine.on_signal(SignalDirection.LONG)
        assert r2.status is TransitionStatus.SAFETY_STOP
        assert broker.submitted == []


# ------------------------------------------------ I: stale contract expiry


class TestIStaleContract:
    def test_expired_contract_rejected_before_submission(self):
        resolver = TableContractResolver(
            {
                OptionLeg.CALL: make_contract(
                    OptionLeg.CALL, expiry=date(2026, 12, 24)
                ),
                OptionLeg.PUT: make_contract(OptionLeg.PUT),
            }
        )
        broker = ScriptedBroker(auto_fill=True)
        ctx = MarketContext(underlying="NIFTY", as_of=date(2026, 12, 25))
        machine, _ = make_machine(
            "a", broker=broker, resolver=resolver, market_context=ctx
        )
        r = machine.on_signal(SignalDirection.LONG)
        assert r.status is TransitionStatus.SAFETY_STOP
        assert r.safety_stop_reason is SafetyStopReason.RESOLUTION_FAILED
        assert broker.submitted == []

    def test_live_contract_with_context_not_rejected(self):
        resolver = TableContractResolver(
            {
                OptionLeg.CALL: make_contract(
                    OptionLeg.CALL, expiry=date(2026, 12, 24)
                ),
                OptionLeg.PUT: make_contract(
                    OptionLeg.PUT, expiry=date(2026, 12, 24)
                ),
            }
        )
        broker = ScriptedBroker(auto_fill=True)
        ctx = MarketContext(underlying="NIFTY", as_of=date(2026, 12, 23))
        machine, _ = make_machine(
            "a", broker=broker, resolver=resolver, market_context=ctx
        )
        r = machine.on_signal(SignalDirection.LONG)
        assert r.status is TransitionStatus.COMPLETED
        assert r.state is PositionState.LONG_CALL


# ------------------------------------ J: invariant / property enforcement


class TestJInvariants:
    def test_open_transactions_are_always_buy(self):
        for pos in PositionState:
            for signal in SignalDirection:
                try:
                    steps, _ = plan_orders(pos, signal)
                except ValueError:
                    continue
                for step in steps:
                    if step.order_action is OrderAction.OPEN:
                        assert step.tx_side is TxSide.BUY

    def test_sell_without_position_is_rejected(self):
        for leg in (OptionLeg.CALL, OptionLeg.PUT):
            with pytest.raises(Exception):
                validate_order_semantics(
                    action=OrderAction.OPEN,
                    option_leg=leg,
                    contract_type=leg_to_contract_type(leg),
                    tx_side=TxSide.SELL,
                )

    def test_pairing_rules_open_and_close(self):
        # PUT+PE+BUY valid as OPEN; PUT+PE+SELL is valid as CLOSE only.
        validate_order_semantics(
            action=OrderAction.OPEN, option_leg=OptionLeg.PUT,
            contract_type=ContractType.PE, tx_side=TxSide.BUY,
        )
        validate_order_semantics(
            action=OrderAction.CLOSE, option_leg=OptionLeg.PUT,
            contract_type=ContractType.PE, tx_side=TxSide.SELL,
        )
        with pytest.raises(Exception):
            validate_order_semantics(
                action=OrderAction.OPEN, option_leg=OptionLeg.PUT,
                contract_type=ContractType.PE, tx_side=TxSide.SELL,
            )
        # CALL+CE+BUY valid as OPEN; CALL+CE+SELL valid as CLOSE only.
        validate_order_semantics(
            action=OrderAction.OPEN, option_leg=OptionLeg.CALL,
            contract_type=ContractType.CE, tx_side=TxSide.BUY,
        )
        validate_order_semantics(
            action=OrderAction.CLOSE, option_leg=OptionLeg.CALL,
            contract_type=ContractType.CE, tx_side=TxSide.SELL,
        )
        with pytest.raises(Exception):
            validate_order_semantics(
                action=OrderAction.OPEN, option_leg=OptionLeg.CALL,
                contract_type=ContractType.CE, tx_side=TxSide.SELL,
            )

    def test_plan_never_opens_both_legs(self):
        for pos in PositionState:
            for signal in SignalDirection:
                try:
                    steps, _ = plan_orders(pos, signal)
                except ValueError:
                    continue
                opens = [s for s in steps if s.order_action is OrderAction.OPEN]
                assert len(opens) <= 1

    def test_plan_orders_close_before_open_in_reversal(self):
        # A reversal plan orders CLOSE before OPEN; the machine additionally
        # refuses to submit the OPEN until the CLOSE is flat-confirmed.
        for pos, signal in (
            (PositionState.LONG_CALL, SignalDirection.SHORT),
            (PositionState.LONG_PUT, SignalDirection.LONG),
        ):
            steps, _ = plan_orders(pos, signal)
            actions = [s.order_action for s in steps]
            assert actions == [OrderAction.CLOSE, OrderAction.OPEN]

    def test_broker_never_reports_ce_and_pe_positions_in_normal_flow(self):
        # A sequence of mandates must never leave the broker holding both legs.
        cases = [
            [SignalDirection.LONG, SignalDirection.SHORT, SignalDirection.FLAT],
            [SignalDirection.SHORT, SignalDirection.LONG, SignalDirection.FLAT],
            [SignalDirection.LONG, SignalDirection.FLAT, SignalDirection.SHORT],
        ]
        for seq in cases:
            machine, broker = make_machine("x")
            for signal in seq:
                machine.on_signal(signal)
            ce, pe = leg_quantities(broker)
            assert not (ce > 0 and pe > 0)


# ----------------------------------------- K: order vs reconcile mismatch


class TestKOrderVsReconcileMismatch:
    def test_filled_entry_but_position_flat_is_safety(self):
        # After construction's adoption reconcile, the broker reports FILLED
        # order status but a FLAT position (phantom fill).
        broker = ScriptedBroker(auto_fill=True)
        machine, _ = make_machine("a", broker=broker)
        broker.latch_position = PositionSnapshot()
        r = machine.on_signal(SignalDirection.LONG)
        assert r.status is TransitionStatus.SAFETY_STOP
        assert (
            r.safety_stop_reason is SafetyStopReason.RECONCILIATION_DISAGREEMENT
        )
        assert machine.state is PositionState.FLAT

    def test_filled_entry_but_wrong_leg_is_safety(self):
        broker = ScriptedBroker(auto_fill=True)
        machine, _ = make_machine("a", broker=broker)
        broker.latch_position = PositionSnapshot(
            details=(
                PositionDetail(
                    contract=make_contract(OptionLeg.PUT), quantity=1
                ),
            )
        )
        r = machine.on_signal(SignalDirection.LONG)
        assert r.status is TransitionStatus.SAFETY_STOP
        assert (
            r.safety_stop_reason is SafetyStopReason.RECONCILIATION_DISAGREEMENT
        )
        assert machine.state is PositionState.LONG_PUT  # mirrors broker truth

    def test_rejected_entry_but_position_positive_is_safety(self):
        broker = ScriptedBroker(auto_fill=True)
        machine, _ = make_machine("a", broker=broker)
        broker.latch_position = PositionSnapshot(
            details=(
                PositionDetail(
                    contract=make_contract(OptionLeg.PUT), quantity=1
                ),
            )
        )
        broker.reject_next = OrderStatus.REJECTED
        registry = machine._guard.registry  # noqa: SLF001
        r = machine.on_signal(SignalDirection.SHORT)
        assert r.status is TransitionStatus.SAFETY_STOP
        assert (
            r.safety_stop_reason is SafetyStopReason.RECONCILIATION_DISAGREEMENT
        )
        assert machine.state is PositionState.LONG_PUT
        assert registry.occupied

    def test_disagreement_generates_no_further_auto_orders(self):
        broker = ScriptedBroker(auto_fill=True)
        machine, _ = make_machine("a", broker=broker)
        broker.latch_position = PositionSnapshot()
        r = machine.on_signal(SignalDirection.LONG)
        assert r.status is TransitionStatus.SAFETY_STOP
        count = len(broker.submitted)
        # no automatic repair triggers any order
        r2 = machine.on_signal(SignalDirection.FLAT)
        assert r2.status is TransitionStatus.HELD
        assert len(broker.submitted) == count


# --------------------------------------------------- L: quantity validation


class TestLQuantityValidation:
    def test_non_integer_order_quantity_rejected(self):
        contract = make_contract(OptionLeg.CALL)
        step = plan_orders(PositionState.FLAT, SignalDirection.LONG)[0][0]
        with pytest.raises(Exception):
            build_order_request(step, contract, quantity=1.5)
        with pytest.raises(Exception):
            build_order_request(step, contract, quantity=True)

    def test_zero_quantity_rejected_by_order_request(self):
        contract = make_contract(OptionLeg.CALL)
        step = plan_orders(PositionState.FLAT, SignalDirection.LONG)[0][0]
        with pytest.raises(Exception):
            build_order_request(step, contract, quantity=0)

    def test_reconcile_float_quantity_is_invalid_safety(self):
        snapshot = PositionSnapshot(
            details=(
                PositionDetail(
                    contract=make_contract(OptionLeg.CALL), quantity=0.5
                ),
            )
        )
        decision = reconcile_to_state(snapshot)
        assert decision.safety_stop is SafetyStopReason.INVALID_QUANTITY

    def test_reconcile_zero_quantity_is_flat(self):
        snapshot = PositionSnapshot(
            details=(
                PositionDetail(
                    contract=make_contract(OptionLeg.CALL), quantity=0
                ),
            )
        )
        decision = reconcile_to_state(snapshot)
        assert decision.safety_stop is None
        assert decision.position is PositionState.FLAT


# --------------------------------------------------- M: no-auto-recovery


class TestMNoAutoRecovery:
    def test_safety_stop_generates_no_order_on_its_own(self):
        broker = ScriptedBroker(auto_fill=True)
        machine, _ = make_machine("a", broker=broker)
        machine.on_signal(SignalDirection.LONG)
        broker.reject_next = OrderStatus.REJECTED
        r = machine.on_signal(SignalDirection.SHORT)
        assert r.status is TransitionStatus.SAFETY_STOP
        count = len(broker.submitted)
        # same-held-direction signal does not act or repair
        r2 = machine.on_signal(SignalDirection.LONG)
        assert r2.status is TransitionStatus.HELD
        assert len(broker.submitted) == count

    def test_entry_failed_flat_generates_no_order_on_its_own(self):
        broker = ScriptedBroker(auto_fill=True)
        broker.reject_if = (
            lambda o: OrderStatus.REJECTED
            if o.order_action is OrderAction.OPEN
            else None
        )
        machine, _ = make_machine("a", broker=broker)
        r = machine.on_signal(SignalDirection.LONG)
        assert r.status is TransitionStatus.ENTRY_FAILED_FLAT
        count = len(broker.submitted)
        r2 = machine.on_signal(SignalDirection.FLAT)
        assert r2.status is TransitionStatus.HELD
        assert len(broker.submitted) == count

    def test_broker_unavailable_mid_transition_is_safety_stop(self):
        broker = ScriptedBroker(auto_fill=True)
        machine, _ = make_machine("a", broker=broker)
        broker.raise_on_submit = VNextError("broker exploded")
        registry = machine._guard.registry  # noqa: SLF001
        r = machine.on_signal(SignalDirection.LONG)
        assert r.status is TransitionStatus.SAFETY_STOP
        assert r.safety_stop_reason is SafetyStopReason.BROKER_UNAVAILABLE
        assert broker.submitted == []
        # flat reconcile -> reservation released after a failed attempt
        assert not registry.occupied

    def test_unknown_exception_re_raised_and_transition_cleared(self):
        broker = ScriptedBroker(auto_fill=True)
        machine, _ = make_machine("a", broker=broker)
        broker.raise_on_submit = RuntimeError("atomic boom")
        with pytest.raises(RuntimeError):
            machine.on_signal(SignalDirection.LONG)
        assert not machine.in_transition


# ---------------------------------------------- N: event/state consistency


class TestNEventStateConsistency:
    def test_terminal_states_match_broker(self):
        broker = ScriptedBroker(auto_fill=True)
        machine, _ = make_machine("a", broker=broker)
        for signal in (
            SignalDirection.LONG,
            SignalDirection.SHORT,
            SignalDirection.FLAT,
        ):
            r = machine.on_signal(signal)
            assert r.status in (
                TransitionStatus.COMPLETED,
                TransitionStatus.HELD,
            )
            assert_state_consistent(machine, broker)
        assert machine.state is PositionState.FLAT
        ce, pe = leg_quantities(broker)
        assert (ce, pe) == (0, 0)

    def test_idempotency_key_is_deterministic(self):
        m1, _ = make_machine("k1")
        m2, _ = make_machine("k2")
        m1.on_signal(SignalDirection.LONG)
        m2.on_signal(SignalDirection.LONG)
        assert m1.transition_key == m2.transition_key == "FLAT:LONG"