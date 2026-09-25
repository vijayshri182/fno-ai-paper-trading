"""vNext option execution state machine — implementation test-suite.

Isolated end-to-end tests over the real ``execution.vnext`` package using only
deterministic fakes (``vnext_helpers.ScriptedBroker`` + ``TableContractResolver``).
No network, no Upstox, no paper/live orders. Mirrors the acceptance spec in
``tests/test_execution_semantics_vnext_spec.py`` (sections A-F) and adds the
implementation-only sections G (restart), H (idempotency), I (determinism) and
regression guards that the vNext model never emits PUT=SELL openings, a
bearish SELL CE, or a bearish SELL PE.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

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
    TransitionStatus,
    TxSide,
    VNextOptionExecutionMachine,
)
from fno_ai_paper_trading.execution.vnext.errors import (
    ReconciliationError,
    SingleSlotViolationError,
)
from fno_ai_paper_trading.execution.vnext.mapping import plan_orders
from fno_ai_paper_trading.execution.vnext.state import (
    position_to_contract_type,
    reconcile_to_state,
)
from fno_ai_paper_trading.execution.vnext.broker import OrderRequest
from fno_ai_paper_trading.execution.vnext.order_semantics import (
    validate_order_semantics,
)

from vnext_helpers import ScriptedBroker, make_contract, make_resolver

DEC_2026 = date(2026, 12, 24)


def make_machine(holder_id: str, *, broker=None, registry=None):
    broker = broker or ScriptedBroker(auto_fill=True)
    registry = registry or SlotRegistry()
    return VNextOptionExecutionMachine(
        broker=broker,
        resolver=make_resolver(broker.contracts),
        guard=registry.guard(holder_id),
    ), broker


def submitted_signature(broker: ScriptedBroker) -> list[tuple[str, str, str]]:
    """Human-readable order signature for ordering assertions."""
    return [
        (
            o.order_action.value,
            o.contract.option_leg.value,
            o.tx_side.value,
        )
        for o in broker.submitted
    ]


# --------------------------------------------------------- A: signal mapping


class TestASignalMapping:
    def test_long_maps_to_buy_call(self):
        machine, broker = make_machine("a")
        result = machine.on_signal(SignalDirection.LONG)
        assert result.status is TransitionStatus.COMPLETED
        assert result.state is PositionState.LONG_CALL
        assert submitted_signature(broker) == [("OPEN", "CALL", "BUY")]

    def test_short_maps_to_buy_put(self):
        machine, broker = make_machine("a")
        result = machine.on_signal(SignalDirection.SHORT)
        assert result.status is TransitionStatus.COMPLETED
        assert result.state is PositionState.LONG_PUT
        assert submitted_signature(broker) == [("OPEN", "PUT", "BUY")]

    def test_flat_from_flat_places_no_order(self):
        machine, broker = make_machine("a")
        result = machine.on_signal(SignalDirection.FLAT)
        assert result.status is TransitionStatus.HELD
        assert result.state is PositionState.FLAT
        assert broker.submitted == []

    def test_short_entry_is_buy_never_sell_put(self):
        # Core invariant: PUT does NOT mean SELL.
        machine, broker = make_machine("a")
        machine.on_signal(SignalDirection.SHORT)
        assert broker.submitted[0].contract.contract_type is ContractType.PE
        assert broker.submitted[0].tx_side is TxSide.BUY
        assert broker.submitted[0].order_action is OrderAction.OPEN


# --------------------------------------------------------- B: closing


class TestBClosing:
    def test_long_call_close_sells_call(self):
        machine, broker = make_machine("a")
        machine.on_signal(SignalDirection.LONG)
        result = machine.on_signal(SignalDirection.FLAT)
        assert result.status is TransitionStatus.COMPLETED
        assert result.state is PositionState.FLAT
        assert submitted_signature(broker) == [
            ("OPEN", "CALL", "BUY"),
            ("CLOSE", "CALL", "SELL"),
        ]

    def test_long_put_close_sells_put(self):
        machine, broker = make_machine("a")
        machine.on_signal(SignalDirection.SHORT)
        result = machine.on_signal(SignalDirection.FLAT)
        assert result.status is TransitionStatus.COMPLETED
        assert result.state is PositionState.FLAT
        assert submitted_signature(broker) == [
            ("OPEN", "PUT", "BUY"),
            ("CLOSE", "PUT", "SELL"),
        ]

    def test_same_side_is_hold(self):
        machine_a, broker_a = make_machine("a")
        machine_a.on_signal(SignalDirection.LONG)
        assert len(broker_a.submitted) == 1
        result = machine_a.on_signal(SignalDirection.LONG)
        assert result.status is TransitionStatus.HELD
        assert result.state is PositionState.LONG_CALL
        assert len(broker_a.submitted) == 1  # no second order

        machine_b, broker_b = make_machine("b")
        machine_b.on_signal(SignalDirection.SHORT)
        assert len(broker_b.submitted) == 1
        result = machine_b.on_signal(SignalDirection.SHORT)
        assert result.status is TransitionStatus.HELD
        assert result.state is PositionState.LONG_PUT
        assert len(broker_b.submitted) == 1  # no second order


# --------------------------------------------------------- C: reversal


class TestCReversal:
    def test_reversal_call_to_put_closes_first_then_buys_put(self):
        machine, broker = make_machine("a")
        machine.on_signal(SignalDirection.LONG)
        result = machine.on_signal(SignalDirection.SHORT)
        assert result.status is TransitionStatus.COMPLETED
        assert result.state is PositionState.LONG_PUT
        assert submitted_signature(broker) == [
            ("OPEN", "CALL", "BUY"),
            ("CLOSE", "CALL", "SELL"),
            ("OPEN", "PUT", "BUY"),
        ]

    def test_reversal_put_to_call_closes_first_then_buys_call(self):
        machine, broker = make_machine("a")
        machine.on_signal(SignalDirection.SHORT)
        result = machine.on_signal(SignalDirection.LONG)
        assert result.status is TransitionStatus.COMPLETED
        assert result.state is PositionState.LONG_CALL
        assert submitted_signature(broker) == [
            ("OPEN", "PUT", "BUY"),
            ("CLOSE", "PUT", "SELL"),
            ("OPEN", "CALL", "BUY"),
        ]

    def test_close_not_confirmed_blocks_opposite_leg(self):
        broker = ScriptedBroker(auto_fill=False)
        machine, _ = make_machine("a", broker=broker)
        # Drive the LONG entry to FILLED manually (get order id from result).
        r0 = machine.on_signal(SignalDirection.LONG)
        assert r0.status is TransitionStatus.STOPPED  # entry pending
        broker.set_order_status(r0.order_ids[0], OrderStatus.FILLED)
        r1 = machine.on_signal(SignalDirection.LONG)
        assert r1.status is TransitionStatus.COMPLETED
        assert r1.state is PositionState.LONG_CALL

        before = submitted_signature(broker)
        r2 = machine.on_signal(SignalDirection.SHORT)
        assert r2.status is TransitionStatus.STOPPED  # close not confirmed
        assert r2.state is PositionState.LONG_CALL
        # Close submitted, but no opposite OPEN yet.
        assert submitted_signature(broker) == before + [("CLOSE", "CALL", "SELL")]

    def _open_long_call(self, broker: ScriptedBroker, holder: str):
        machine, _ = make_machine("a", broker=broker)
        r = machine.on_signal(SignalDirection.LONG)
        assert r.status is TransitionStatus.COMPLETED
        assert r.state is PositionState.LONG_CALL
        return machine

    def test_entry_failure_after_close_leaves_flat(self):
        broker = ScriptedBroker(auto_fill=True)
        machine, _ = make_machine("a", broker=broker)
        machine.on_signal(SignalDirection.LONG)
        broker.reject_if = (
            lambda o: OrderStatus.REJECTED if o.order_action is OrderAction.OPEN else None
        )
        result = machine.on_signal(SignalDirection.SHORT)
        assert result.status is TransitionStatus.ENTRY_FAILED_FLAT
        assert result.state is PositionState.FLAT
        assert submitted_signature(broker) == [
            ("OPEN", "CALL", "BUY"),
            ("CLOSE", "CALL", "SELL"),
            ("OPEN", "PUT", "BUY"),
        ]
        # broker must show no PE position (rejected entry never materialised)
        assert all(
            d.contract.contract_type is ContractType.CE
            for d in broker.position_snapshot.details
        )
        assert machine.state is PositionState.FLAT


# --------------------------------------------------------- D: failure safety


class TestDFailureSafety:
    def test_exit_rejected_stops_and_blocks_opposite_leg(self):
        broker = ScriptedBroker(auto_fill=True)
        machine, _ = make_machine("a", broker=broker)
        machine.on_signal(SignalDirection.LONG)
        broker.reject_next = OrderStatus.REJECTED
        result = machine.on_signal(SignalDirection.SHORT)
        assert result.status is TransitionStatus.SAFETY_STOP
        assert result.safety_stop_reason is SafetyStopReason.CLOSE_REJECTED
        assert machine.state is PositionState.LONG_CALL  # position untouched
        assert submitted_signature(broker) == [
            ("OPEN", "CALL", "BUY"),
            ("CLOSE", "CALL", "SELL"),
        ]
        # Only the close was attempted; no opposite OPEN was submitted.
        assert all(
            o.contract.contract_type is ContractType.CE
            for o in broker.submitted
        )  # every order was on CE; no PE order ever appeared

    def test_exit_unfilled_stops_and_blocks_opposite_leg(self):
        broker = ScriptedBroker(auto_fill=True)
        machine, _ = make_machine("a", broker=broker)
        machine.on_signal(SignalDirection.LONG)
        broker.reject_next = OrderStatus.UNFILLED
        result = machine.on_signal(SignalDirection.SHORT)
        assert result.status is TransitionStatus.SAFETY_STOP
        assert result.safety_stop_reason is SafetyStopReason.CLOSE_UNFILLED
        assert submitted_signature(broker) == [
            ("OPEN", "CALL", "BUY"),
            ("CLOSE", "CALL", "SELL"),
        ]

    def test_reconcile_not_flat_blocks_opposite_leg(self):
        from fno_ai_paper_trading.execution.vnext.broker import (
            PositionDetail,
            PositionSnapshot,
        )

        broker = ScriptedBroker(auto_fill=True)
        machine, _ = make_machine("a", broker=broker)
        machine.on_signal(SignalDirection.LONG)
        # Simulate a broker whose close "fills" but whose position snapshot
        # still reports 1 CE (reconciliation disagrees with the fill report).
        ce = make_contract(OptionLeg.CALL)
        broker.latch_position = PositionSnapshot(
            details=(PositionDetail(contract=ce, quantity=1),)
        )
        result = machine.on_signal(SignalDirection.SHORT)
        assert result.status is TransitionStatus.SAFETY_STOP
        assert result.safety_stop_reason is SafetyStopReason.RECONCILIATION_NOT_FLAT
        assert result.state is PositionState.LONG_CALL
        # Only the close was attempted; no opposite OPEN was submitted.
        assert submitted_signature(broker) == [
            ("OPEN", "CALL", "BUY"),
            ("CLOSE", "CALL", "SELL"),
        ]

    def test_hold_never_places_an_order(self):
        cases = [
            (PositionState.FLAT, SignalDirection.FLAT),
            (PositionState.LONG_CALL, SignalDirection.LONG),
            (PositionState.LONG_PUT, SignalDirection.SHORT),
        ]
        for state, signal in cases:
            steps, target = plan_orders(state, signal)
            assert steps == []
            assert target == state

    def test_open_entry_rejected_from_flat_returns_to_flat(self):
        broker = ScriptedBroker(auto_fill=True)
        broker.reject_next = OrderStatus.REJECTED
        machine, _ = make_machine("a", broker=broker)
        result = machine.on_signal(SignalDirection.LONG)
        assert result.status is TransitionStatus.ENTRY_FAILED_FLAT
        assert result.safety_stop_reason is SafetyStopReason.ENTRY_REJECTED
        assert result.state is PositionState.FLAT
        assert machine.state is PositionState.FLAT
        assert broker.position_snapshot.details == ()


# --------------------------------------------------------- E: single-slot


class TestESingleSlot:
    def test_second_run_cannot_open_while_slot_held(self):
        registry = SlotRegistry()
        machine_a, _ = make_machine("run-a", registry=registry)
        machine_a.on_signal(SignalDirection.LONG)
        machine_b, broker_b = make_machine("run-b", registry=registry)
        with pytest.raises(SingleSlotViolationError):
            machine_b.on_signal(SignalDirection.SHORT)
        assert broker_b.submitted == []

    def test_ce_and_pe_are_distinct_but_same_slot(self):
        # A CE held by one run must block a PE open by another run.
        registry = SlotRegistry()
        machine_a, _ = make_machine("run-a", registry=registry)
        machine_a.on_signal(SignalDirection.LONG)  # holds CE
        machine_b, broker_b = make_machine("run-b", registry=registry)
        with pytest.raises(SingleSlotViolationError):
            machine_b.on_signal(SignalDirection.SHORT)  # wants PE
        assert broker_b.submitted == []

    def test_reversal_releases_then_reacquires_slot(self):
        registry = SlotRegistry()
        machine_a, _ = make_machine("run-a", registry=registry)
        machine_a.on_signal(SignalDirection.LONG)
        machine_a.on_signal(SignalDirection.SHORT)  # CE released, PE reserved
        assert registry.occupied
        assert registry.contract_type is ContractType.PE
        machine_a.on_signal(SignalDirection.FLAT)
        assert not registry.occupied

    def test_plan_has_at_most_one_open_step(self):
        for state in PositionState:
            for signal in SignalDirection:
                try:
                    steps, _ = plan_orders(state, signal)
                except ValueError:
                    continue
                assert sum(1 for s in steps if s.order_action is OrderAction.OPEN) <= 1


# ------------------------------------------------- F: instrument consistency


class TestFInstrumentConsistency:
    def test_call_pairs_with_ce(self):
        validate_order_semantics(
            action=OrderAction.OPEN,
            option_leg=OptionLeg.CALL,
            contract_type=ContractType.CE,
            tx_side=TxSide.BUY,
        )
        with pytest.raises(Exception):
            validate_order_semantics(
                action=OrderAction.OPEN,
                option_leg=OptionLeg.CALL,
                contract_type=ContractType.PE,
                tx_side=TxSide.BUY,
            )

    def test_put_pairs_with_pe(self):
        validate_order_semantics(
            action=OrderAction.OPEN,
            option_leg=OptionLeg.PUT,
            contract_type=ContractType.PE,
            tx_side=TxSide.BUY,
        )
        with pytest.raises(Exception):
            validate_order_semantics(
                action=OrderAction.OPEN,
                option_leg=OptionLeg.PUT,
                contract_type=ContractType.CE,
                tx_side=TxSide.BUY,
            )

    def test_long_call_requires_ce(self):
        assert position_to_contract_type(PositionState.LONG_CALL) is ContractType.CE
        assert position_to_contract_type(PositionState.LONG_PUT) is ContractType.PE

    def test_unexpected_holding_pair_reconciled_as_safety_stop(self):
        # Broker snapshot with CE+PE simultaneously => ambiguous => safety stop.
        ce = make_contract(OptionLeg.CALL)
        pe = make_contract(OptionLeg.PUT)
        from fno_ai_paper_trading.execution.vnext.broker import PositionDetail, PositionSnapshot
        snapshot = PositionSnapshot(
            details=(
                PositionDetail(contract=ce, quantity=1),
                PositionDetail(contract=pe, quantity=1),
            )
        )
        decision = reconcile_to_state(snapshot)
        assert decision.safety_stop is not None
        assert decision.safety_stop is SafetyStopReason.AMBIGUOUS_POSITIONS

    def test_short_position_reconciled_as_safety_stop(self):
        ce = make_contract(OptionLeg.CALL)
        from fno_ai_paper_trading.execution.vnext.broker import PositionDetail, PositionSnapshot
        snapshot = PositionSnapshot(
            details=(PositionDetail(contract=ce, quantity=-1),)
        )
        decision = reconcile_to_state(snapshot)
        assert decision.safety_stop is not None
        assert decision.safety_stop is SafetyStopReason.UNEXPECTED_SHORT


# ------------------------------------------------------- G: restart/reconcile


class TestGRestartReconcile:
    def test_restart_with_broker_ce_becomes_long_call(self):
        broker = ScriptedBroker(auto_fill=True)
        broker.set_position(make_contract(OptionLeg.CALL), 1)
        machine = VNextOptionExecutionMachine(
            broker=broker,
            resolver=make_resolver(broker.contracts),
            guard=SlotRegistry().guard("restart"),
        )
        assert machine.state is PositionState.LONG_CALL

    def test_restart_with_broker_pe_becomes_long_put(self):
        broker = ScriptedBroker(auto_fill=True)
        broker.set_position(make_contract(OptionLeg.PUT), 1)
        machine = VNextOptionExecutionMachine(
            broker=broker,
            resolver=make_resolver(broker.contracts),
            guard=SlotRegistry().guard("restart"),
        )
        assert machine.state is PositionState.LONG_PUT

    def test_restart_with_flat_broker_stays_flat(self):
        broker = ScriptedBroker(auto_fill=True)
        machine = VNextOptionExecutionMachine(
            broker=broker,
            resolver=make_resolver(broker.contracts),
            guard=SlotRegistry().guard("restart"),
        )
        assert machine.state is PositionState.FLAT

    def test_restart_with_ce_and_pe_refuses_to_run(self):
        broker = ScriptedBroker(auto_fill=True)
        broker.set_position(make_contract(OptionLeg.CALL), 1)
        broker.set_position(make_contract(OptionLeg.PUT), 1)
        with pytest.raises(ReconciliationError):
            VNextOptionExecutionMachine(
                broker=broker,
                resolver=make_resolver(broker.contracts),
                guard=SlotRegistry().guard("restart"),
            )

    def test_restart_never_autotrades_to_repair(self):
        broker = ScriptedBroker(auto_fill=True)
        broker.set_position(make_contract(OptionLeg.CALL), 1)
        machine, _ = make_machine("restart", broker=broker)
        # hold signal: nothing to do
        result = machine.on_signal(SignalDirection.LONG)
        assert result.status is TransitionStatus.HELD
        assert broker.submitted == []


# ------------------------------------------------------- H: idempotency


class TestHIdempotency:
    def test_repeated_signal_during_inflight_transition_does_not_duplicate(self):
        broker = ScriptedBroker(auto_fill=False)
        machine, _ = make_machine("a", broker=broker)
        r = machine.on_signal(SignalDirection.LONG)
        assert r.status is TransitionStatus.STOPPED  # open pending
        count1 = len(broker.submitted)
        # Repeat identical signal while in flight.
        r2 = machine.on_signal(SignalDirection.LONG)
        assert r2.status is TransitionStatus.STOPPED
        assert len(broker.submitted) == count1  # no duplicate order

    def test_duplicate_reversal_signal_does_not_open_twice(self):
        broker = ScriptedBroker(auto_fill=False)
        machine, _ = make_machine("a", broker=broker)
        r0 = machine.on_signal(SignalDirection.LONG)
        assert r0.status is TransitionStatus.STOPPED
        broker.set_order_status(r0.order_ids[0], OrderStatus.FILLED)
        r1 = machine.on_signal(SignalDirection.LONG)
        assert r1.status is TransitionStatus.COMPLETED
        assert r1.state is PositionState.LONG_CALL

        def pe_opens() -> int:
            return sum(
                1
                for o in broker.submitted
                if o.contract.contract_type is ContractType.PE
                and o.order_action is OrderAction.OPEN
            )

        # Reversal begins: close pending, no PE opened yet.
        r2 = machine.on_signal(SignalDirection.SHORT)
        assert r2.status is TransitionStatus.STOPPED
        assert pe_opens() == 0

        # Duplicate reversal while in flight must not open PE a second time.
        r3 = machine.on_signal(SignalDirection.SHORT)
        assert r3.status is TransitionStatus.STOPPED
        assert pe_opens() == 0

        # Confirm the close, then confirm the entry: exactly one PE open total.
        broker.set_order_status(r2.order_ids[0], OrderStatus.FILLED)
        r4 = machine.on_signal(SignalDirection.SHORT)
        assert r4.status is TransitionStatus.STOPPED  # PE entry now pending
        broker.set_order_status(r4.order_ids[0], OrderStatus.FILLED)
        r5 = machine.on_signal(SignalDirection.SHORT)
        assert r5.status is TransitionStatus.COMPLETED
        assert r5.state is PositionState.LONG_PUT
        assert pe_opens() == 1

    def test_slot_released_once_on_successful_close_then_reopen(self):
        registry = SlotRegistry()
        machine, _ = make_machine("a", registry=registry)
        machine.on_signal(SignalDirection.LONG)
        machine.on_signal(SignalDirection.FLAT)
        assert not registry.occupied
        machine.on_signal(SignalDirection.SHORT)  # reopens a fresh slot
        assert registry.occupied
        assert registry.contract_type is ContractType.PE


# ------------------------------------------------------- I: determinism


class TestIDeterminism:
    def test_identical_scenario_produces_identical_order_stream(self):
        def run_once() -> tuple[list[tuple[str, str, str]], list[TransitionStatus]]:
            machine, broker = make_machine("d")
            statuses = []
            for signal in (
                SignalDirection.LONG,
                SignalDirection.SHORT,
                SignalDirection.FLAT,
                SignalDirection.SHORT,
            ):
                result = machine.on_signal(signal)
                statuses.append(result.status)
            return submitted_signature(broker), statuses

        sig1, statuses1 = run_once()
        sig2, statuses2 = run_once()
        assert sig1 == sig2
        assert statuses1 == statuses2

    def test_deterministic_transition_key(self):
        machine1, _ = make_machine("k1")
        machine2, _ = make_machine("k2")
        key1 = machine1._compute_key(SignalDirection.LONG)  # noqa: SLF001
        key2 = machine2._compute_key(SignalDirection.LONG)  # noqa: SLF001
        assert key1 == key2 == "FLAT:LONG"


# ------------------------------------------- regression: forbidden semantics


class TestRegressionForbiddenOpenings:
    def test_put_sell_as_opening_is_rejected(self):
        with pytest.raises(Exception):
            validate_order_semantics(
                action=OrderAction.OPEN,
                option_leg=OptionLeg.PUT,
                contract_type=ContractType.PE,
                tx_side=TxSide.SELL,
            )

    def test_call_sell_as_opening_is_rejected(self):
        with pytest.raises(Exception):
            validate_order_semantics(
                action=OrderAction.OPEN,
                option_leg=OptionLeg.CALL,
                contract_type=ContractType.CE,
                tx_side=TxSide.SELL,
            )

    def test_bearish_machine_never_emits_sell_call_or_sell_put(self):
        machine, broker = make_machine("r")
        machine.on_signal(SignalDirection.SHORT)
        signature = submitted_signature(broker)
        assert all(o[2] == "BUY" for o in signature)  # entry is BUY (PE)
        assert not any(o[0] == "CLOSE" for o in signature)
        assert not any(o[1] in ("CALL",) for o in signature)  # never sell CE

    def test_plan_for_bearish_never_sells_anything(self):
        steps, _ = plan_orders(PositionState.FLAT, SignalDirection.SHORT)
        assert [s.tx_side.value for s in steps] == ["BUY"]
        assert [s.contract_type.value for s in steps] == ["PE"]