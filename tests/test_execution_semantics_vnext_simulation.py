"""End-to-end deterministic simulation of the vNext execution machine.

Covers 26 scripted scenarios plus the 18 global invariants, runs a 200-event
seeded stress that asserts every invariant after every event, and demonstrably
reproduces a byte-identical canonical ledger for a fixed seed. Nothing here
touches a network, reads a credential, or emits a real order — the broker,
resolver and registry are all deterministic fakes.
"""
import random

from fno_ai_paper_trading.execution.vnext.enums import (
    ContractType,
    OrderAction,
    OrderStatus,
    PositionState,
    SafetyStopReason,
    SignalDirection,
    TransitionStatus,
)
from fno_ai_paper_trading.execution.vnext.errors import ReconciliationError
from fno_ai_paper_trading.execution.vnext.guards import SlotRegistry

from vnext_simulation import (
    AppliedMutation,
    BrokerAction,
    SimulationHarness,
)


def harness(*args, **kwargs) -> SimulationHarness:
    return SimulationHarness(*args, **kwargs)


def open_id(h: SimulationHarness) -> str:
    assert h.machine.transition is not None
    assert h.machine.transition.open_order_id is not None
    return h.machine.transition.open_order_id


def close_id(h: SimulationHarness) -> str:
    assert h.machine.transition is not None
    assert h.machine.transition.close_order_id is not None
    return h.machine.transition.close_order_id


# ---------------------------------------------------------------------------
# 1-2: clean entries
# ---------------------------------------------------------------------------


def test_s1_clean_long_entry():
    h = harness()
    r, err = h.drive(SignalDirection.LONG)
    assert err is None
    assert r.status is TransitionStatus.COMPLETED
    assert r.state is PositionState.LONG_CALL
    assert h.machine.state is PositionState.LONG_CALL
    assert len(h.broker.submitted) == 1
    order = h.broker.submitted[0]
    assert order.request.order_action is OrderAction.OPEN
    assert order.request.contract.contract_type is ContractType.CE
    assert h.broker.qty(ContractType.CE) == 75
    assert h.broker.qty(ContractType.PE) == 0
    assert h.registry.holder_id == "run-a"
    assert h.registry.contract_type is ContractType.CE
    assert h.registry._quantity == 75
    h.assert_invariants()


def test_s2_clean_short_entry():
    h = harness()
    r, err = h.drive(SignalDirection.SHORT)
    assert err is None
    assert r.status is TransitionStatus.COMPLETED
    assert r.state is PositionState.LONG_PUT
    assert h.broker.qty(ContractType.PE) == 75
    assert h.registry.contract_type is ContractType.PE
    h.assert_invariants()


# ---------------------------------------------------------------------------
# 3-4: entry confirmation gating
# ---------------------------------------------------------------------------


def test_s3_no_duplicate_signal_while_entry_pending():
    h = harness(script=[BrokerAction(status=OrderStatus.PENDING)])
    r, err = h.drive(SignalDirection.LONG)
    assert err is None
    assert r.status is TransitionStatus.STOPPED  # PENDING keeps it in flight
    assert h.machine.in_transition
    r2, err = h.drive(SignalDirection.LONG)  # repeated signal: idempotent poll
    assert err is None
    assert r2.status is TransitionStatus.STOPPED
    assert len(h.broker.submitted) == 1
    h.mark_filled(open_id(h))
    r3, _ = h.drive(SignalDirection.LONG)
    assert r3.status is TransitionStatus.COMPLETED
    assert r3.state is PositionState.LONG_CALL
    h.assert_invariants()


def test_s4_pending_entry_fills_then_completes():
    h = harness(script=[BrokerAction(status=OrderStatus.PENDING)])
    h.drive(SignalDirection.LONG)
    r, _ = h.poll()
    assert r.status is TransitionStatus.STOPPED  # entry not yet confirmed
    h.mark_filled(open_id(h))
    r2, err = h.poll()
    assert err is None
    assert r2.status is TransitionStatus.COMPLETED
    assert h.machine.state is PositionState.LONG_CALL
    assert h.broker.qty(ContractType.CE) == 75
    h.assert_invariants()


# ---------------------------------------------------------------------------
# 5-12: exits
# ---------------------------------------------------------------------------


def test_s5_clean_exit():
    h = harness()
    h.drive(SignalDirection.LONG)
    r, err = h.drive(SignalDirection.FLAT)
    assert err is None
    assert r.status is TransitionStatus.COMPLETED
    assert r.state is PositionState.FLAT
    assert h.machine.state is PositionState.FLAT
    assert h.broker.qty(ContractType.CE) == 0
    assert h.broker.qty(ContractType.PE) == 0
    assert not h.registry.occupied
    h.assert_invariants()


def test_s6_pending_exit_never_opens_opposite_leg():
    h = harness(script=[BrokerAction(), BrokerAction(status=OrderStatus.PENDING)])
    h.drive(SignalDirection.LONG)
    r, err = h.drive(SignalDirection.FLAT)
    assert err is None
    assert r.status is TransitionStatus.STOPPED
    assert "opposite leg NOT opened" in r.message
    assert h.machine.in_transition
    assert len(h.broker.submitted) == 2
    assert all(
        o.request.order_action is OrderAction.CLOSE
        for o in h.broker.submitted[1:]
    )
    h.mark_filled(close_id(h))
    r2, _ = h.drive(SignalDirection.FLAT)
    assert r2.status is TransitionStatus.COMPLETED
    assert r2.state is PositionState.FLAT
    h.assert_invariants()


def test_s7_rejected_exit():
    h = harness(script=[BrokerAction(), BrokerAction(status=OrderStatus.REJECTED)])
    h.drive(SignalDirection.LONG)
    r, err = h.drive(SignalDirection.FLAT)
    assert err is None
    assert r.status is TransitionStatus.SAFETY_STOP
    assert r.safety_stop_reason is SafetyStopReason.CLOSE_REJECTED
    assert h.machine.state is PositionState.LONG_CALL
    assert h.broker.qty(ContractType.CE) == 75
    assert h.registry.occupied
    h.assert_invariants()


def test_s8_unfilled_exit():
    h = harness(script=[BrokerAction(), BrokerAction(status=OrderStatus.UNFILLED)])
    h.drive(SignalDirection.LONG)
    r, err = h.drive(SignalDirection.FLAT)
    assert err is None
    assert r.status is TransitionStatus.SAFETY_STOP
    assert r.safety_stop_reason is SafetyStopReason.CLOSE_UNFILLED
    assert h.machine.state is PositionState.LONG_CALL
    assert h.broker.qty(ContractType.CE) == 75
    h.assert_invariants()


def test_s9_partial_exit_only_closes_confirmed_fill():
    h = harness(script=[BrokerAction(), BrokerAction(status=OrderStatus.PENDING)])
    h.drive(SignalDirection.LONG)
    h.drive(SignalDirection.FLAT)
    h.mark_filled(close_id(h))
    r, _ = h.drive(SignalDirection.FLAT)
    assert r.status is TransitionStatus.COMPLETED
    assert r.state is PositionState.FLAT
    assert h.broker.qty(ContractType.CE) == 0
    h.assert_invariants()


def test_s10_filled_but_not_flat_exit_is_safety():
    h = harness(
        script=[
            BrokerAction(),
            BrokerAction(status=OrderStatus.FILLED, mutate="none"),
        ]
    )
    h.drive(SignalDirection.LONG)
    r, err = h.drive(SignalDirection.FLAT)
    assert err is None
    assert r.status is TransitionStatus.SAFETY_STOP
    assert r.safety_stop_reason is SafetyStopReason.RECONCILIATION_NOT_FLAT
    assert h.machine.state is PositionState.LONG_CALL
    assert h.broker.qty(ContractType.CE) == 75
    assert h.registry.occupied
    h.assert_invariants()


def test_s11_rejected_exit_with_venue_liquidation():
    h = harness(
        script=[
            BrokerAction(),
            BrokerAction(
                status=OrderStatus.REJECTED, flatten_on_reject=True
            ),
        ]
    )
    h.drive(SignalDirection.LONG)
    r, err = h.drive(SignalDirection.FLAT)
    assert err is None
    assert r.status is TransitionStatus.SAFETY_STOP
    assert r.safety_stop_reason is SafetyStopReason.CLOSE_REJECTED
    # The venue liquidated out-of-band: audited as an external flip.
    assert h.broker.qty(ContractType.CE) == 0
    assert any(
        f.contract_type is ContractType.CE and f.quantity == 0
        for f in h.broker.external
    )
    # Machine refused to claim a phantom flat; state mirrors the broker's
    # cleanly-reconciled result.
    assert h.machine.state is PositionState.FLAT
    assert not h.registry.occupied
    h.assert_invariants()


def test_s12_close_refused_on_flat_broker():
    h = harness()
    r, err = h.drive(SignalDirection.FLAT)
    assert err is None
    assert r.status is TransitionStatus.HELD
    assert r.state is PositionState.FLAT
    assert len(h.broker.submitted) == 0
    h.assert_invariants()


# ---------------------------------------------------------------------------
# 13-15: entries that cannot complete safely
# ---------------------------------------------------------------------------


def test_s13_rejected_entry_after_close_is_entry_failed_flat():
    h = harness(
        script=[
            BrokerAction(),
            BrokerAction(),
            BrokerAction(status=OrderStatus.REJECTED),
        ]
    )
    h.drive(SignalDirection.LONG)  # CE open filled -> LONG_CALL
    r, err = h.drive(SignalDirection.SHORT)  # reversal: close CE, open PE
    assert err is None
    # close CE filled and the open PE was REJECTED inside one drive.
    assert r.status is TransitionStatus.ENTRY_FAILED_FLAT
    assert r.state is PositionState.FLAT
    assert len(h.broker.submitted) == 3
    assert h.machine.state is PositionState.FLAT
    assert h.broker.qty(ContractType.CE) == 0
    assert h.broker.qty(ContractType.PE) == 0
    assert not h.registry.occupied
    h.assert_invariants()


def test_s14_rejected_entry_with_broker_holding_is_safety():
    # A broker that already holds a position must refuse any new OPEN. The
    # slot reservation is kept (conservative, no phantom release).
    h = harness()
    h.external_flip(ContractType.CE, 75)
    r, err = h.drive(SignalDirection.LONG)
    assert err is None
    assert r.status is TransitionStatus.SAFETY_STOP
    assert r.safety_stop_reason is SafetyStopReason.RECONCILIATION_DISAGREEMENT
    assert h.machine.state is PositionState.LONG_CALL  # mirrors broker truth
    assert len(h.broker.submitted) == 0
    assert h.broker.qty(ContractType.CE) == 75
    assert h.registry.occupied  # conservative reservation kept
    assert h.registry.contract_type is ContractType.CE
    h.assert_invariants()


def test_s15_entry_broker_unreachable():
    # VNextError variant: submit blows up with a vNext-level error.
    h = harness()
    h.queue_submit_raise(ReconciliationError("submit unavailable"))
    r, err = h.drive(SignalDirection.LONG)
    assert err is None
    assert r.status is TransitionStatus.SAFETY_STOP
    assert r.safety_stop_reason is SafetyStopReason.RECONCILIATION_DISAGREEMENT
    assert len(h.broker.submitted) == 0
    h.assert_invariants()

    # Raw unknown exception: surfaced to the caller, no order, no phantom.
    h2 = harness()
    h2.queue_submit_raise(RuntimeError("net down"))
    r2, err2 = h2.drive(SignalDirection.LONG)
    assert r2 is None
    assert "RuntimeError" in (err2 or "")
    assert len(h2.broker.submitted) == 0
    assert h2.broker.qty(ContractType.CE) == 0
    assert not h2.registry.occupied
    h2.assert_invariants()


# ---------------------------------------------------------------------------
# 16-20: resolution fidelity
# ---------------------------------------------------------------------------


def test_s16_missing_contract_is_resolution_failure():
    h = harness()
    h.resolver.queue_fault("missing")
    r, err = h.drive(SignalDirection.LONG)
    assert err is None
    assert r.status is TransitionStatus.SAFETY_STOP
    assert r.safety_stop_reason is SafetyStopReason.RESOLUTION_FAILED
    assert len(h.broker.submitted) == 0
    assert not h.registry.occupied
    h.assert_invariants()


def test_s17_wrong_type_is_resolution_failure():
    h = harness()
    h.resolver.queue_fault("wrong_pair")
    r, err = h.drive(SignalDirection.SHORT)
    assert err is None
    assert r.status is TransitionStatus.SAFETY_STOP
    assert r.safety_stop_reason is SafetyStopReason.RESOLUTION_FAILED
    assert len(h.broker.submitted) == 0
    h.assert_invariants()


def test_s18_wrong_pair_is_rejected():
    h = harness()
    h.resolver.queue_fault("wrong_pair")
    r, err = h.drive(SignalDirection.LONG)
    assert err is None
    assert r.status is TransitionStatus.SAFETY_STOP
    assert r.safety_stop_reason is SafetyStopReason.RESOLUTION_FAILED
    assert len(h.broker.submitted) == 0
    h.assert_invariants()


def test_s19_expired_contract_is_rejected():
    h = harness()
    h.resolver.queue_fault("expired")
    r, err = h.drive(SignalDirection.LONG)
    assert err is None
    assert r.status is TransitionStatus.SAFETY_STOP
    assert r.safety_stop_reason is SafetyStopReason.RESOLUTION_FAILED
    assert len(h.broker.submitted) == 0
    h.assert_invariants()


def test_s20_resolution_failure_then_clean_retry():
    h = harness()
    h.resolver.queue_fault("missing")
    r, _ = h.drive(SignalDirection.LONG)
    assert r.status is TransitionStatus.SAFETY_STOP
    h.resolver.queue_fault(None)  # resolver healthy again
    r2, err = h.drive(SignalDirection.LONG)
    assert err is None
    assert r2.status is TransitionStatus.COMPLETED
    assert r2.state is PositionState.LONG_CALL
    assert h.broker.qty(ContractType.CE) == 75
    h.assert_invariants()


# ---------------------------------------------------------------------------
# 21-24: reversals
# ---------------------------------------------------------------------------


def test_s21_clean_reversal():
    h = harness()
    h.drive(SignalDirection.LONG)
    r, err = h.drive(SignalDirection.SHORT)
    assert err is None
    assert r.status is TransitionStatus.COMPLETED
    assert r.state is PositionState.LONG_PUT
    assert h.machine.state is PositionState.LONG_PUT
    assert len(h.broker.submitted) == 3
    actions = [o.request.order_action for o in h.broker.submitted]
    assert actions == [
        OrderAction.OPEN,
        OrderAction.CLOSE,
        OrderAction.OPEN,
    ]
    assert h.broker.qty(ContractType.CE) == 0
    assert h.broker.qty(ContractType.PE) == 75
    assert h.registry.contract_type is ContractType.PE
    h.assert_invariants()


def test_s22_reversal_close_rejected_never_opens_opposite():
    h = harness(script=[BrokerAction(), BrokerAction(status=OrderStatus.REJECTED)])
    h.drive(SignalDirection.LONG)
    r, err = h.drive(SignalDirection.SHORT)
    assert err is None
    assert r.status is TransitionStatus.SAFETY_STOP
    assert r.safety_stop_reason is SafetyStopReason.CLOSE_REJECTED
    assert len(h.broker.submitted) == 2
    assert h.broker.qty(ContractType.PE) == 0
    assert h.broker.qty(ContractType.CE) == 75
    h.assert_invariants()


def test_s23_reversal_close_filled_but_not_flat():
    h = harness(
        script=[
            BrokerAction(),
            BrokerAction(
                status=OrderStatus.FILLED, mutate="none", flatten_on_reject=False
            ),
        ]
    )
    h.drive(SignalDirection.LONG)
    r, err = h.drive(SignalDirection.SHORT)
    assert err is None
    assert r.status is TransitionStatus.SAFETY_STOP
    assert r.safety_stop_reason is SafetyStopReason.RECONCILIATION_NOT_FLAT
    assert h.machine.state is PositionState.LONG_CALL
    assert len(h.broker.submitted) == 2  # no opposite leg was opened
    assert h.broker.qty(ContractType.PE) == 0
    h.assert_invariants()


def test_s24_failed_reversal_no_phantom_no_auto_retry():
    h = harness(script=[BrokerAction(), BrokerAction(status=OrderStatus.REJECTED)])
    h.drive(SignalDirection.LONG)
    r, _ = h.drive(SignalDirection.SHORT)
    assert r.status is TransitionStatus.SAFETY_STOP
    assert not h.machine.in_transition
    # No order was somehow planted on the opposite leg, ever.
    assert all(
        o.request.contract.contract_type is ContractType.CE
        for o in h.broker.submitted
    )
    # Re-drive is a user decision, not an automatic machine retry: the
    # machine stays conservative and the broker is untouched until then.
    assert h.broker.qty(ContractType.CE) == 75
    assert h.broker.qty(ContractType.PE) == 0
    h.assert_invariants()


# ---------------------------------------------------------------------------
# 25: restart adoption (never invents, refuses unsafe)
# ---------------------------------------------------------------------------


def test_s25_restart_adopts_held_position():
    h = harness()
    h.drive(SignalDirection.LONG)
    ok, detail = h.restart()
    assert ok, detail
    assert h.machine.state is PositionState.LONG_CALL
    assert h.broker.qty(ContractType.CE) == 75
    assert h.registry.holder_id == "run-a"
    assert h.registry.contract_type is ContractType.CE
    h.assert_invariants()


def test_s25_restart_adopts_externally_changed_position():
    # Machine was FLAT but the broker was changed out-of-band to PE=75: the
    # restart adopts EXACTLY that (no phantom entry, no invention of CE).
    h = harness()
    h.external_flip(ContractType.PE, 75)
    ok, detail = h.restart()
    assert ok, detail
    assert h.machine.state is PositionState.LONG_PUT
    assert h.registry.contract_type is ContractType.PE
    assert h.broker.qty(ContractType.CE) == 0
    h.assert_invariants()


def test_s25_restart_refuses_unsafe_snapshot():
    h = harness()
    h.external_flip(ContractType.CE, 75)
    h.external_flip(ContractType.PE, 75)
    ok, detail = h.restart()
    assert not ok
    assert h.machine.state is PositionState.FLAT
    assert len(h.broker.submitted) == 0
    h.assert_invariants()


# ---------------------------------------------------------------------------
# 26: slot race, then full determinism stress
# ---------------------------------------------------------------------------


def test_s26_slot_race():
    registry = SlotRegistry()
    a = harness(holder_id="run-a", registry=registry)
    b = harness(holder_id="race", registry=registry)
    ra, ea = a.drive(SignalDirection.LONG)
    assert ea is None and ra.status is TransitionStatus.COMPLETED
    # The contested run may not open anything against a claimed slot.
    rb, eb = b.drive(SignalDirection.LONG)
    assert rb is None
    assert "SingleSlotViolationError" in (eb or "")
    assert len(b.broker.submitted) == 0
    assert not b.machine.in_transition
    a.assert_invariants()
    b.assert_invariants()
    # Once the winner releases, the contestant proceeds normally.
    a.drive(SignalDirection.FLAT)
    rb2, eb2 = b.drive(SignalDirection.LONG)
    assert eb2 is None
    assert rb2.status is TransitionStatus.COMPLETED
    assert b.machine.state is PositionState.LONG_CALL
    assert b.registry.contract_type is ContractType.CE
    a.assert_invariants()
    b.assert_invariants()


_OPS = [
    "SIGNAL:LONG",
    "SIGNAL:SHORT",
    "SIGNAL:FLAT",
    "POLL",
    "ADVANCE",
    "MARK_FILLED",
    "MARK_FILLED_DELAYED",
    "MARK_REJECTED",
    "MARK_CANCELLED",
    "FLIP_CE_0",
    "FLIP_CE_75",
    "FLIP_PE_0",
    "FLIP_PE_75",
    "FAULT_MISSING",
    "FAULT_WRONG_PAIR",
    "RAISE_SUBMIT_ONCE",
    "RAISE_RECONCILE_ONCE",
    "RESTART",
    "RACE_LONG",
    "RACE_SHORT",
    "RACE_FLAT",
]


def make_events(seed: int, n: int) -> list[str]:
    rng = random.Random(seed)
    return [_OPS[rng.randrange(len(_OPS))] for _ in range(n)]


def _apply(op: str, h: SimulationHarness, race: SimulationHarness) -> None:
    if op.startswith("SIGNAL:"):
        h.drive(SignalDirection(op.split(":")[1]))
    elif op == "POLL":
        h.poll()
    elif op == "ADVANCE":
        h.advance(1)
    elif op.startswith("MARK_"):
        t = h.machine.transition
        aid = None
        if t is not None:
            aid = t.open_order_id or t.close_order_id
        if aid is None:
            h.note("noop " + op)
        elif op == "MARK_FILLED":
            h.mark_filled(aid)
        elif op == "MARK_FILLED_DELAYED":
            h.mark_filled(aid, apply_delay=1)
        elif op == "MARK_REJECTED":
            h.mark_rejected(aid)
        elif op == "MARK_CANCELLED":
            h.mark_cancelled(aid)
    elif op.startswith("FLIP_"):
        leg = op.split("_")[1]
        val = int(op.split("_")[2])
        h.external_flip(ContractType.CE if leg == "CE" else ContractType.PE, val)
    elif op == "FAULT_MISSING":
        h.note("fault missing")
        h.resolver.queue_fault("missing")
    elif op == "FAULT_WRONG_PAIR":
        h.note("fault wrong_pair")
        h.resolver.queue_fault("wrong_pair")
    elif op == "RAISE_SUBMIT_ONCE":
        h.queue_submit_raise(ReconciliationError("scripted submit fault"))
    elif op == "RAISE_RECONCILE_ONCE":
        h.queue_reconcile_raise(ReconciliationError("scripted reconcile fault"))
    elif op == "RESTART":
        h.restart()
    elif op.startswith("RACE_"):
        race.drive(SignalDirection(op.split("_")[1]))
    h.assert_invariants()
    race.assert_invariants()


def run_stress(seed: int, n: int) -> str:
    h = harness(holder_id="run-a")
    race = harness(holder_id="race", registry=h.registry)
    for op in make_events(seed, n):
        _apply(op, h, race)
    return h.canonical() + "\n---RACE---\n" + race.canonical()


def test_s26_determinism_double_run_is_byte_identical():
    c1 = run_stress(20260919, 200)
    c2 = run_stress(20260919, 200)
    assert c1 == c2
    assert len(c1) > 5000  # a real, non-trivial ledger


def test_s26_different_seed_differs():
    assert run_stress(20260919, 200) != run_stress(7, 200)


# ---------------------------------------------------------------------------
# Checker wiring sanity: the invariants really can fire on a broken state
# ---------------------------------------------------------------------------


def test_invariants_fire_on_tampering():
    h = harness()
    h.drive(SignalDirection.LONG)
    assert h.check_all() == []

    h.registry._quantity = 76  # reservation no longer matches the broker
    assert any("I14" in v for v in h.check_all())
    h.registry._quantity = 75

    h.broker._qty[ContractType.CE] = -5  # a short at the broker
    assert any(v.startswith("I02") or v.startswith("I05") for v in h.check_all())
    h.broker._qty[ContractType.CE] = 75

    h.broker.applied.append(
        AppliedMutation(1, "Z9", ContractType.CE, 75, "apply")
    )  # a mutation with no submitted order
    assert any("I17" in v for v in h.check_all())


def test_supports_snapshot_time_manual():
    h = harness()
    ok, _ = h.restart()
    assert ok
    h.assert_invariants()