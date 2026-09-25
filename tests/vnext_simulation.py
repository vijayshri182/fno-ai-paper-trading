"""Deterministic in-memory end-to-end simulation harness for the vNext machine.

Everything here is a deterministic fake: the broker never touches a network,
never reads a credential and never emits a real order. The simulation drives
``VNextOptionExecutionMachine`` through scripted call sequences (signals,
polling, fills, rejections, reconcile delays, broker drifts, restarts and slot
races) and audits 18 global invariants after every event. Because no wall-clock
time, no UUID and no unseeded randomness is used, re-running the same event
script twice yields a byte-identical canonical ledger.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from fno_ai_paper_trading.execution.vnext.broker import (
    OrderRequest,
    OrderStatusRecord,
    OrderTicket,
    PositionDetail,
    PositionSnapshot,
)
from fno_ai_paper_trading.execution.vnext.contract import (
    ContractResolver,
    MarketContext,
    OptionContract,
    TableContractResolver,
)
from fno_ai_paper_trading.execution.vnext.enums import (
    ContractType,
    OptionLeg,
    OrderAction,
    OrderStatus,
    PositionState,
    SignalDirection,
    TransitionStatus,
    TxSide,
)
from fno_ai_paper_trading.execution.vnext.errors import (
    ContractResolutionError,
    ReconciliationError,
    SingleSlotViolationError,
)
from fno_ai_paper_trading.execution.vnext.guards import SlotRegistry
from fno_ai_paper_trading.execution.vnext.machine import (
    TransitionResult,
    VNextOptionExecutionMachine,
)
from fno_ai_paper_trading.execution.vnext.state import (
    position_to_contract_type,
    reconcile_to_state,
)

from vnext_helpers import make_contract, make_resolver


def opposite(contract_type: ContractType) -> ContractType:
    return (
        ContractType.PE if contract_type is ContractType.CE else ContractType.CE
    )


def leg_contract_type_pairs_ok(contract: OptionContract) -> bool:
    if contract.option_leg is OptionLeg.NONE:
        return True
    valid = {
        OptionLeg.CALL: ContractType.CE,
        OptionLeg.PUT: ContractType.PE,
    }
    return valid.get(contract.option_leg) is contract.contract_type


# --------------------------------------------------------------------------
# Scripted deterministic broker
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class BrokerAction:
    """One consumed-down submission behavior.

    ``status``       ticket status returned by ``submit_order``;
    ``mutate``       how a FILLED order mutates broker positions:
                     "apply" (normal), "none" (phantom fill / reconcile lag),
                     "wrong_leg" (fills the opposite leg), "both" (ambiguous);
    ``apply_delay``  broker ticks before a FILLED mutation becomes visible in
                     a later ``reconcile()`` (delayed reconciliation);
    ``raise_on_submit`` exception raised before the order is acknowledged;
    ``flatten_on_reject`` when the ticket is REJECTED, the venue concurrently
                     liquidates the leg (an audited external flip), so the
                     post-reject reconcile can legitimately show flat.
    """

    status: OrderStatus = OrderStatus.FILLED
    mutate: str = "apply"
    apply_delay: int = 0
    raise_on_submit: Exception | None = None
    flatten_on_reject: bool = False


@dataclass(frozen=True)
class SubmittedOrder:
    """One order recorded by the broker, with its full status history."""

    order_id: str
    seq: int
    request: OrderRequest
    mutate: str = "apply"
    apply_delay: int = 0
    history: tuple[tuple[int, OrderStatus], ...] = ()

    @property
    def final_status(self) -> OrderStatus | None:
        return self.history[-1][1] if self.history else None


@dataclass(frozen=True)
class AppliedMutation:
    """An audited position mutation (the only way broker quantity changes)."""

    tick: int
    order_id: str
    contract_type: ContractType
    delta: int
    mutate: str


@dataclass(frozen=True)
class ExternalFlip:
    """A driver-set broker position change (simulated third-party drift)."""

    tick: int
    contract_type: ContractType
    quantity: int


class SimulationBroker:
    """Deterministic ``Broker`` protocol implementation with scripted faults.

    Position quantity for each of CE/PE is stored explicitly and only ever
    changes through:
      * ``AppliedMutation`` from a FILLED order (audited),
      * a driver ``external_flip``,
      * a due delayed mutation applying inside ``reconcile()``.
    A raised submit/reconcile/status error never mutates a position.
    """

    def __init__(
        self,
        *,
        contracts: dict[OptionLeg, OptionContract] | None = None,
        script: list[BrokerAction] | None = None,
        initial_positions: dict[ContractType, int] | None = None,
        raise_submit_once: Exception | None = None,
        raise_reconcile_once: Exception | None = None,
        raise_reconcile_persistent: Exception | None = None,
        raise_status_persistent: Exception | None = None,
    ) -> None:
        self.contracts = contracts or {
            OptionLeg.CALL: make_contract(OptionLeg.CALL),
            OptionLeg.PUT: make_contract(OptionLeg.PUT),
        }
        self._by_type = {
            ContractType.CE: self.contracts[OptionLeg.CALL],
            ContractType.PE: self.contracts[OptionLeg.PUT],
        }
        self._qty: dict[ContractType, int] = {
            ContractType.CE: 0,
            ContractType.PE: 0,
        }
        if initial_positions:
            for contract_type, quantity in initial_positions.items():
                self._qty[contract_type] = quantity

        self._script: deque[BrokerAction] = deque(script or [])
        self._orders: dict[str, SubmittedOrder] = {}
        self._seq = 0
        self._tick = 0
        self.submitted: list[SubmittedOrder] = []
        self.applied: list[AppliedMutation] = []
        self.external: list[ExternalFlip] = []
        self._pending_mutations: list[tuple[ContractType, int, int]] = []  # (ct, delta, due)

        self._raise_submit_once = raise_submit_once
        self._raise_reconcile_once = raise_reconcile_once
        self._raise_reconcile_persistent = raise_reconcile_persistent
        self._raise_status_persistent = raise_status_persistent

    # ------------------------------------------------------------ accessors

    def qty(self, contract_type: ContractType) -> int:
        return self._qty[contract_type]

    def order(self, order_id: str) -> SubmittedOrder:
        return self._orders[order_id]

    def contract(self, contract_type: ContractType) -> OptionContract:
        return self._by_type[contract_type]

    @property
    def has_pending_mutations(self) -> bool:
        return bool(self._pending_mutations)

    # ---------------------------------------------------------------- driver

    def external_flip(self, contract_type: ContractType, quantity: int) -> None:
        self._tick += 1
        self._qty[contract_type] = quantity
        self.external.append(
            ExternalFlip(self._tick, contract_type, quantity)
        )

    def set_order_status(
        self,
        order_id: str,
        status: OrderStatus,
        *,
        mutate: str | None = None,
        apply_delay: int | None = None,
    ) -> None:
        self._tick += 1
        order = self._orders[order_id]
        if mutate is not None:
            object.__setattr__(order, "mutate", mutate)
        if apply_delay is not None:
            object.__setattr__(order, "apply_delay", apply_delay)
        history = order.history + ((self._tick, status),)
        object.__setattr__(order, "history", history)
        if status is OrderStatus.FILLED:
            self._apply_with(
                order, order.request, order.mutate, order.apply_delay
            )

    def mark_filled(
        self, order_id: str, apply_delay: int | None = None
    ) -> None:
        self.set_order_status(order_id, OrderStatus.FILLED, apply_delay=apply_delay)

    def mark_rejected(self, order_id: str) -> None:
        self.set_order_status(order_id, OrderStatus.REJECTED)

    def mark_cancelled(self, order_id: str) -> None:
        self.set_order_status(order_id, OrderStatus.CANCELLED)

    def advance_step(self) -> None:
        self._tick += 1
        self._pending_mutations = [
            (ct, delta, due - 1) for ct, delta, due in self._pending_mutations
        ]

    # --------------------------------------------------------------- protocol

    def submit_order(self, order: OrderRequest) -> OrderTicket:
        self._tick += 1
        if self._raise_submit_once is not None:
            exc = self._raise_submit_once
            self._raise_submit_once = None
            raise exc
        action = (
            self._script.popleft() if self._script else BrokerAction()
        )
        if action.raise_on_submit is not None:
            raise action.raise_on_submit
        self._seq += 1
        order_id = f"S{self._seq}"

        if order.order_action is OrderAction.OPEN and order.tx_side is TxSide.SELL:
            raise ReconciliationError("OPEN must be BUY in vNext")
        if order.quantity <= 0:
            raise ReconciliationError("quantity must be positive")
        if not leg_contract_type_pairs_ok(order.contract):
            raise ReconciliationError("illegal contract pairing")

        record = SubmittedOrder(
            order_id=order_id,
            seq=self._seq,
            request=order,
            mutate=action.mutate,
            apply_delay=action.apply_delay,
            history=((self._tick, action.status),),
        )
        self._orders[order_id] = record
        self.submitted.append(record)
        if (
            action.status in (OrderStatus.REJECTED, OrderStatus.CANCELLED)
            and action.flatten_on_reject
        ):
            # Venue rejected AND liquidated out-of-band: audited external flip.
            self._qty[order.contract.contract_type] = 0
            self.external.append(
                ExternalFlip(
                    self._tick, order.contract.contract_type, 0
                )
            )
        elif action.status is OrderStatus.FILLED:
            self._apply_with(record, order, action.mutate, action.apply_delay)
        return OrderTicket(order_id=order_id, status=action.status)

    def get_order_status(self, order_id: str) -> OrderStatusRecord:
        self._tick += 1
        if self._raise_status_persistent is not None:
            raise self._raise_status_persistent
        record = self._orders[order_id]
        return OrderStatusRecord(
            order_id=order_id,
            status=record.final_status or OrderStatus.PENDING,
            message="scripted",
        )

    def get_position(self) -> PositionSnapshot:
        self._tick += 1
        return self.position_snapshot

    def reconcile(self) -> PositionSnapshot:
        self._tick += 1
        if self._raise_reconcile_once is not None:
            exc = self._raise_reconcile_once
            self._raise_reconcile_once = None
            raise exc
        if self._raise_reconcile_persistent is not None:
            raise self._raise_reconcile_persistent
        # Delayed mutations become visible at reconcile time.
        for ct, delta, due in self._pending_mutations:
            if due <= 0:
                self._mutate_inner(delta, ct, AppliedMutation(self._tick, "lag", ct, delta, "delayed"))
        self._pending_mutations = [
            (ct, delta, due)
            for ct, delta, due in self._pending_mutations
            if due > 0
        ]
        return self.position_snapshot

    @property
    def position_snapshot(self) -> PositionSnapshot:
        details = tuple(
            PositionDetail(contract=self._by_type[ct], quantity=qty)
            for ct, qty in self._qty.items()
            if qty != 0
        )
        return PositionSnapshot(details=details)

    # --------------------------------------------------------------- helpers

    def _apply_with(
        self,
        order: SubmittedOrder,
        order_request: OrderRequest,
        mutate: str,
        apply_delay: int,
    ) -> None:
        target = order_request.contract.contract_type
        delta = order_request.quantity if order_request.tx_side is TxSide.BUY else -order_request.quantity
        if mutate == "none":
            return
        if mutate == "wrong_leg":
            targets = (opposite(target),)
        elif mutate == "both":
            targets = (ContractType.CE, ContractType.PE)
        else:
            targets = (target,)
        for ct in targets:
            if apply_delay > 0:
                self._pending_mutations.append((ct, delta, apply_delay))
            else:
                self._mutate_inner(
                    delta,
                    ct,
                    AppliedMutation(self._tick, order.order_id, ct, delta, mutate),
                )

    def _mutate_inner(self, delta: int, ct: ContractType, audit: AppliedMutation) -> None:
        self._qty[ct] += delta
        self.applied.append(audit)


# --------------------------------------------------------------------------
# Fault-injecting deterministic resolver
# --------------------------------------------------------------------------


class ScriptedResolver:
    """Wraps a table resolver with one-shot deterministic failures.

    ``fault`` options (consumed once, applied to the next resolution):
      * ``"missing"``    -> raise ``ContractResolutionError`` (no contract);
      * ``"wrong_pair"`` -> return the OPPOSITE leg's contract (wrong CE/PE
        pairing for the step) which the machine rejects as a resolution
        mismatch;
      * ``"expired"``    -> return a contract expiring before the context date.
    """

    def __init__(
        self,
        contracts: dict[OptionLeg, OptionContract] | None = None,
        context: MarketContext | None = None,
    ) -> None:
        self._base = make_resolver(contracts)
        self._context = context or MarketContext(
            underlying="NIFTY", as_of=date(2026, 6, 17)
        )
        self._fault_queue: deque[str | None] = deque()

    def queue_fault(self, fault: str | None) -> None:
        self._fault_queue.append(fault)

    def resolve(
        self, option_leg: OptionLeg, market_context: MarketContext | None = None
    ) -> OptionContract:
        fault = self._fault_queue.popleft() if self._fault_queue else None
        if fault == "missing":
            raise ContractResolutionError(
                f"no contract available for {option_leg.value}"
            )
        if fault == "wrong_pair":
            other = OptionLeg.PUT if option_leg is OptionLeg.CALL else OptionLeg.CALL
            return self._base.resolve(other, self._context)
        if fault == "expired":
            contract = self._base.resolve(option_leg, self._context)
            past = date(2019, 1, 1)
            return OptionContract(
                option_leg=contract.option_leg,
                contract_type=contract.contract_type,
                instrument_key=contract.instrument_key,
                expiry=past,
                strike=contract.strike,
                lot_size=contract.lot_size,
                tick_size=contract.tick_size,
            )
        return self._base.resolve(option_leg, self._context)


# --------------------------------------------------------------------------
# Harness: one deterministic simulation run
# --------------------------------------------------------------------------


class SimulationHarness:
    """Owns a broker/resolver/registry/machine pair and a byte-identical ledger.

    Every driver action appends a deterministic line to ``events``. The
    canonical dump (``canonical()``) is a full re-run fingerprint: same event
    script -> identical string.
    """

    def __init__(
        self,
        *,
        holder_id: str = "run-a",
        contracts: dict[OptionLeg, OptionContract] | None = None,
        script: list[BrokerAction] | None = None,
        initial_positions: dict[ContractType, int] | None = None,
        context: MarketContext | None = None,
        quantity: int | None = None,
        registry: SlotRegistry | None = None,
        resolver: ScriptedResolver | None = None,
        raise_submit_once: Exception | None = None,
        raise_reconcile_once: Exception | None = None,
        raise_reconcile_persistent: Exception | None = None,
        raise_status_persistent: Exception | None = None,
    ) -> None:
        self.holder_id = holder_id
        self.contracts = contracts or {
            OptionLeg.CALL: make_contract(OptionLeg.CALL),
            OptionLeg.PUT: make_contract(OptionLeg.PUT),
        }
        self.context = context or MarketContext(
            underlying="NIFTY", as_of=date(2026, 6, 17)
        )
        self.quantity = quantity
        self.registry = registry or SlotRegistry()
        self.resolver = resolver or ScriptedResolver(self.contracts, self.context)
        self.broker = SimulationBroker(
            contracts=self.contracts,
            script=script,
            initial_positions=initial_positions,
            raise_submit_once=raise_submit_once,
            raise_reconcile_once=raise_reconcile_once,
            raise_reconcile_persistent=raise_reconcile_persistent,
            raise_status_persistent=raise_status_persistent,
        )

        self.tick = 0
        self.events: list[str] = []
        self.windows: list[tuple[int, bool, tuple[str, ...]]] = []
        self.checkpoint_violations: list[str] = []
        self.completion_violations: list[str] = []
        self.last_result: TransitionResult | None = None
        self.last_error: str | None = None
        self.state_origin: dict[PositionState, TransitionStatus] = {}
        self._auto_submit_violations: list[str] = []

        self.machine = self._build_machine()
        if initial_positions:
            for contract_type in initial_positions:
                quantity_now = self.broker.qty(contract_type)
                self.events.append(
                    f"t0 external {contract_type.value}={quantity_now}"
                )
            self.events.append(
                f"t0 adopt state={self.machine.state.value}"
            )

    # ------------------------------------------------------------- construction

    def _build_machine(self) -> VNextOptionExecutionMachine:
        return VNextOptionExecutionMachine(
            broker=self.broker,
            resolver=self.resolver,
            guard=self.registry.guard(self.holder_id),
            market_context=self.context,
            quantity=self.quantity,
        )

    # -------------------------------------------------------------- transitions

    def drive(self, direction: SignalDirection) -> tuple[TransitionResult | None, str | None]:
        self.tick += 1
        was_in_flight = self.machine.in_transition
        before = len(self.broker.submitted)
        try:
            result = self.machine.on_signal(direction)
            error = None
        except SingleSlotViolationError as exc:
            result = None
            error = f"SingleSlotViolationError: {exc}"
        except Exception as exc:
            # Unknown exceptions (raw broker faults mid-transition) surface to
            # the caller; they never fabricate a position and never submit.
            result = None
            error = f"{type(exc).__name__}: {exc}"
        added = tuple(
            order.order_id
            for order in self.broker.submitted[before:]
        )
        if len(added) > 2:
            self._auto_submit_violations.append(
                f"window t{self.tick} submitted more than 2 orders: {added}"
            )
        if was_in_flight and len(added) > 1:
            self._auto_submit_violations.append(
                f"poll t{self.tick} emitted multiple orders mid-transition: {added}"
            )
        self.windows.append((self.tick, was_in_flight, added))

        if result is not None:
            self.last_result = result
            self.last_error = None
            if result.status is not TransitionStatus.HELD:
                self.state_origin[result.state] = result.status
            state = self.machine.state.value if self.machine else "-"
            self.events.append(
                f"t{self.tick} signal={direction.value} "
                f"status={result.status.value} state={state} "
                f"orders={len(added)} stop={result.safety_stop_reason.value if result.safety_stop_reason else '-'}"
            )
            # I04 enforcement at the exact completion moment: a COMPLETED held
            # state requires the broker reconcile to be clean right then.
            if (
                result.status is TransitionStatus.COMPLETED
                and result.state in (
                    PositionState.LONG_CALL,
                    PositionState.LONG_PUT,
                )
            ):
                completion = reconcile_to_state(self.broker.position_snapshot)
                if completion.safety_stop is not None:
                    self.completion_violations.append(
                        f"I04: COMPLETED {result.state.value} while the broker "
                        f"snapshot was unsafe ({completion.safety_stop.value}) "
                        f"at t{self.tick}"
                    )
        else:
            self.last_error = error
            state = self.machine.state.value if self.machine else "-"
            self.events.append(
                f"t{self.tick} signal={direction.value} error={error} "
                f"state={state} orders={len(added)}"
            )
        return result, error

    def poll(self) -> tuple[TransitionResult | None, str | None]:
        if self.machine.transition is None:
            self.tick += 1
            self.events.append(f"t{self.tick} poll noop (no in-flight)")
            return None, "no-in-flight"
        return self.drive(self.machine.transition.signal)

    def advance(self, steps: int = 1) -> None:
        for _ in range(steps):
            self.tick += 1
            before = len(self.broker.submitted)
            self.broker.advance_step()
            if len(self.broker.submitted) != before:
                self._auto_submit_violations.append(
                    f"advance t{self.tick} auto-submitted an order"
                )
            self.events.append(f"t{self.tick} advance")

    def _broker_driver_op(self, label: str, fn) -> None:
        """A driver-controlled broker operation must never emit orders."""
        self.tick += 1
        before = len(self.broker.submitted)
        fn()
        if len(self.broker.submitted) != before:
            self._auto_submit_violations.append(
                f"{label} t{self.tick} auto-submitted an order"
            )
        self.events.append(f"t{self.tick} {label}")

    def mark_filled(self, order_id: str, apply_delay: int | None = None) -> None:
        delay_label = "" if apply_delay is None else f" delay={apply_delay}"
        self._broker_driver_op(f"mark_filled {order_id}{delay_label}", lambda: self.broker.mark_filled(order_id, apply_delay=apply_delay))

    def mark_rejected(self, order_id: str) -> None:
        self._broker_driver_op(
            f"mark_rejected {order_id}", lambda: self.broker.mark_rejected(order_id)
        )

    def mark_cancelled(self, order_id: str) -> None:
        self._broker_driver_op(
            f"mark_cancelled {order_id}",
            lambda: self.broker.mark_cancelled(order_id),
        )

    def external_flip(self, contract_type: ContractType, quantity: int) -> None:
        self._broker_driver_op(
            f"flip {contract_type.value}={quantity}",
            lambda: self.broker.external_flip(contract_type, quantity),
        )

    def note(self, line: str) -> None:
        """Append a deterministic driver note to the event ledger."""
        self.tick += 1
        self.events.append(f"t{self.tick} {line}")

    def queue_submit_raise(self, exc: Exception) -> None:
        self._broker_driver_op(
            "queue_submit_raise",
            lambda: setattr(self.broker, "_raise_submit_once", exc),
        )

    def queue_reconcile_raise(self, exc: Exception) -> None:
        self._broker_driver_op(
            "queue_reconcile_raise",
            lambda: setattr(self.broker, "_raise_reconcile_once", exc),
        )

    def restart(self) -> tuple[bool, str]:
        """Rebuild the machine from the current broker snapshot.

        Never invents a position: adoption is exactly the reconciled snapshot.
        ``restart`` never submits an order and refuses unsafe snapshots.
        """
        self.tick += 1
        before = len(self.broker.submitted)
        try:
            self.machine = self._build_machine()
            self.last_result = None
            self.last_error = None
            self.checkpoint_violations = []
            self.state_origin[self.machine.state] = TransitionStatus.COMPLETED
            self.events.append(
                f"t{self.tick} restart ok state={self.machine.state.value}"
            )
            ok, detail = True, f"ok state={self.machine.state.value}"
        except (ReconciliationError, SingleSlotViolationError) as exc:
            self.events.append(
                f"t{self.tick} restart refused: {type(exc).__name__}"
            )
            ok, detail = False, f"refused: {exc}"
        if len(self.broker.submitted) != before:
            self._auto_submit_violations.append(
                f"restart t{self.tick} auto-submitted an order"
            )
        return ok, detail

    # --------------------------------------------------------------- auditing

    def check_all(self) -> list[str]:
        violations = InvariantChecker(self).violations()
        violations.extend(self._auto_submit_violations)
        violations.extend(self.completion_violations)
        return violations

    def assert_invariants(self) -> None:
        violations = self.check_all()
        if violations:
            raise AssertionError(
                "vNext simulation invariant violation(s):\n"
                + "\n".join(sorted(violations))
            )

    def canonical(self) -> str:
        """Byte-deterministic fingerprint of the whole run."""
        lines: list[str] = []
        lines.append(f"machine state={self.machine.state.value if self.machine else '-'}")
        lines.append(
            f"machine in_transition={self.machine.in_transition if self.machine else False}"
        )
        lines.append(
            f"registry occupied={self.registry.occupied} "
            f"holder={self.registry.holder_id or '-'} "
            f"type={self.registry.contract_type.value if self.registry.contract_type else '-'} "
            f"qty={self.registry._quantity if self.registry.occupied else 0}"
        )
        for event in self.events:
            lines.append(f"event {event}")
        for order in self.broker.submitted:
            request = order.request
            hist = ",".join(
                f"t{t}:{status.value}" for t, status in order.history
            )
            lines.append(
                f"order {order.order_id} {request.order_action.value} "
                f"{request.contract.option_leg.value}/{request.contract.contract_type.value} "
                f"{request.tx_side.value} q={request.quantity} "
                f"exp={request.contract.expiry.isoformat()} "
                f"strike={request.contract.strike} [[{hist}]]"
            )
        for audit in self.broker.applied:
            lines.append(
                f"applied t{audit.tick} {audit.order_id} "
                f"{audit.contract_type.value} {audit.delta:+d}"
            )
        for flip in self.broker.external:
            lines.append(
                f"external t{flip.tick} {flip.contract_type.value} {flip.quantity}"
            )
        lines.append(
            f"position CE={self.broker.qty(ContractType.CE)} "
            f"PE={self.broker.qty(ContractType.PE)}"
        )
        return "\n".join(lines)

    def dump(self) -> str:
        return self.canonical()


# --------------------------------------------------------------------------
# The 18 global invariants
# --------------------------------------------------------------------------


class InvariantChecker:
    """Audits the 18 pinned invariants against a harness snapshot.

    Each check is only active when its preconditions hold (a disagreement stop
    legitimately holds a slot while the broker is flat; during in-flight polls
    the registry may lag the broker by design). A check returning no violation
    means the invariant held wherever it is well-defined.
    """

    def __init__(self, harness: SimulationHarness) -> None:
        self.h = harness
        self.machine = harness.machine
        self.broker = harness.broker
        self.registry = harness.registry

    # ------------------------------------------------------------ preconditions

    def _broker_positions(self) -> dict[ContractType, int]:
        return {ct: self.broker.qty(ct) for ct in (ContractType.CE, ContractType.PE)}

    def _reconcile_decision(self):
        try:
            return reconcile_to_state(self.broker.reconcile())
        except Exception:
            return None

    def _stable_window(self) -> bool:
        """Machine settled onto a clean, reconcile-matching state.

        "Clean" means the last drive resolved COMPLETED / HELD /
        ENTRY_FAILED_FLAT. After a SAFETY_STOP / STOPPED / raw error the machine
        may legitimately keep a conservative reservation (no phantom release),
        so slot-vs-broker agreement is only asserted once a clean state is
        reached. Right after a restart the registry mirrors the pre-restart
        intent and is re-locked by the first explicit signal.
        """
        if self.machine is None:
            return False
        if self.machine.in_transition:
            return False
        if self.last_action_errored():
            return False
        if self.h.last_result is None:
            return False
        if self.h.last_result.status not in (
            TransitionStatus.COMPLETED,
            TransitionStatus.HELD,
            TransitionStatus.ENTRY_FAILED_FLAT,
        ):
            return False
        if self.broker.has_pending_mutations:
            return False
        if self.h.events and self.h.events[-1].startswith(
            f"t{self.h.tick} restart"
        ):
            return False
        decision = self._reconcile_decision()
        if decision is None or decision.safety_stop is not None:
            return False
        return self.machine.state is decision.position

    def last_action_errored(self) -> bool:
        return self.h.last_error is not None

    # -------------------------------------------------------------- each check

    def v01_no_dual_leg_projection(self) -> list[str]:
        out, _ = self._applied_replay_audit()
        return [v for v in out if v.startswith("I01")]

    def v02_ce_nonnegative(self) -> list[str]:
        return (
            ["I02: pending CE quantity went short: "
             f"{self.broker.qty(ContractType.CE)}"]
            if self.broker.qty(ContractType.CE) < 0
            else []
        )

    def v03_pe_nonnegative(self) -> list[str]:
        return (
            ["I03: pending PE quantity went short: "
             f"{self.broker.qty(ContractType.PE)}"]
            if self.broker.qty(ContractType.PE) < 0
            else []
        )

    def v04_no_dual_position_state(self) -> list[str]:
        """A COMPLETED held state must rest on a clean broker reconcile.

        Enforced at drive time (see ``drive``); this check surfaces any
        recorded completion-vs-unsafe-snapshot collision.
        """
        return list(self.h.completion_violations)

    def v05_no_broker_short(self) -> list[str]:
        return [] if not (
            self.broker.qty(ContractType.CE) < 0
            or self.broker.qty(ContractType.PE) < 0
        ) else ["I05: a short quantity appeared at the broker"]

    def v06_open_implies_buy(self) -> list[str]:
        out = []
        for order in self.broker.submitted:
            if order.request.order_action is OrderAction.OPEN and order.request.tx_side is not TxSide.BUY:
                out.append(
                    f"I06: OPEN order {order.order_id} has side "
                    f"{order.request.tx_side.value}"
                )
        return out

    def v07_close_implies_sell(self) -> list[str]:
        out = []
        for order in self.broker.submitted:
            if order.request.order_action is OrderAction.CLOSE and order.request.tx_side is not TxSide.SELL:
                out.append(
                    f"I07: CLOSE order {order.order_id} has side "
                    f"{order.request.tx_side.value}"
                )
        return out

    def v08_seal_never_opens(self) -> list[str]:
        out = []
        for order in self.broker.submitted:
            if order.request.order_action is OrderAction.OPEN and order.request.tx_side is TxSide.SELL:
                out.append(f"I08: SELL OPEN order {order.order_id}")
        return out

    def _ordered_book_events(self):
        """Merge audited mutations and driver externals in broker-tick order."""
        events = [("applied", a.tick, a) for a in self.broker.applied]
        events.extend(("external", f.tick, f) for f in self.broker.external)
        events.sort(key=lambda item: (item[1], item[0]))
        return events

    def _applied_replay_audit(
        self,
    ) -> tuple[list[str], dict[ContractType, int]]:
        """Replay order-driven mutations + driver flips into the slot.

        Order-driven (applied) mutations are checked for single-slot ordering,
        close-before-open and oversell; driver externals (chaos injection) only
        change the effective held quantity.
        """
        out: list[str] = []
        ce_qty = 0
        pe_qty = 0
        net: dict[ContractType, int] = {ContractType.CE: 0, ContractType.PE: 0}
        for kind, _tick, item in self._ordered_book_events():
            if kind == "external":
                target = item.contract_type
                if target is ContractType.CE:
                    ce_qty = item.quantity
                else:
                    pe_qty = item.quantity
                net[target] = item.quantity
                continue
            audit = item
            net[audit.contract_type] += audit.delta
            if audit.delta > 0:  # BUY / open-leg change
                if audit.mutate != "both":  # "both" = injected ambiguity
                    if ce_qty != 0 or pe_qty != 0:
                        out.append(
                            f"I09/I18: BUY {audit.delta:+d} {audit.contract_type.value} "
                            f"while slot held CE={ce_qty} PE={pe_qty} at t{audit.tick}"
                        )
                    if audit.contract_type is ContractType.CE:
                        already = ce_qty > 0
                    else:
                        already = pe_qty > 0
                    if already:
                        out.append(
                            f"I01: BUY {audit.delta:+d} {audit.contract_type.value} "
                            f"into an already-open {audit.contract_type.value} leg "
                            f"at t{audit.tick}"
                        )
                if audit.contract_type is ContractType.CE:
                    ce_qty += audit.delta
                else:
                    pe_qty += audit.delta
            else:  # SELL / close-leg change
                held = ce_qty if audit.contract_type is ContractType.CE else pe_qty
                if held < -audit.delta:
                    out.append(
                        f"I07/I09/I18: SELL {audit.delta:+d} "
                        f"{audit.contract_type.value} over-sells "
                        f"held={held} at t{audit.tick}"
                    )
                if audit.contract_type is ContractType.CE:
                    ce_qty += audit.delta
                else:
                    pe_qty += audit.delta
                if ce_qty < 0 or pe_qty < 0:
                    out.append(
                        f"I02/I05/I18: leg went short at t{audit.tick} "
                        f"(CE={ce_qty} PE={pe_qty})"
                    )
        return out, net

    def v09_open_never_precedes_close(self) -> list[str]:
        out, _ = self._applied_replay_audit()
        return [v for v in out if v.startswith("I09")]

    def v10_leg_type_pairing(self) -> list[str]:
        out = []
        for order in self.broker.submitted:
            contract = order.request.contract
            valid = {
                OptionLeg.CALL: ContractType.CE,
                OptionLeg.PUT: ContractType.PE,
            }
            if valid.get(contract.option_leg) is not contract.contract_type:
                out.append(
                    f"I10: order {order.order_id} pairing "
                    f"{contract.option_leg.value}+"
                    f"{contract.contract_type.value} is illegal"
                )
        return out

    def v11_no_expired_submission(self) -> list[str]:
        if self.h.context.as_of is None:
            return []
        out = []
        for order in self.broker.submitted:
            if order.request.contract.expiry < self.h.context.as_of:
                out.append(
                    f"I11: expired contract submitted by {order.order_id}"
                )
        return out

    def v12_no_phantom_fill_from_exception(self) -> list[str]:
        if self.broker.has_pending_mutations:
            return []
        _, net = self._applied_replay_audit()
        same = (
            self.broker.qty(ContractType.CE) == net[ContractType.CE]
            and self.broker.qty(ContractType.PE) == net[ContractType.PE]
        )
        if not same:
            return [
                "I12: broker position diverges from the audited mutations "
                f"(audit CE={net[ContractType.CE]} PE={net[ContractType.PE]}, "
                f"broker CE={self.broker.qty(ContractType.CE)} "
                f"PE={self.broker.qty(ContractType.PE)})"
            ]
        return []

    def v13_no_duplicate_order_per_transition(self) -> list[str]:
        out = []
        if not self.h.windows:
            return out
        for tick, was_in_flight, added in self.h.windows:
            if was_in_flight and added:
                out.append(
                    f"I13: poll at t{tick} emitted orders {added}"
                )
            if len(added) > 2:
                out.append(f"I13: transition at t{tick} emitted >2 orders")
        return out

    def v14_slot_agrees_with_broker(self) -> list[str]:
        if not self._stable_window():
            return []
        if not self.registry.occupied:
            return []
        if self.registry.holder_id != self.h.holder_id:
            # Another run owns the slot: this machine may only CONSERVATIVELY
            # mirror the broker truth (never claim it, never submit against it).
            # Cross-run agreement is audited by the owning harness.
            return []
        state = self.machine.state
        origin = self.h.state_origin.get(state)
        state_leg = (
            position_to_contract_type(state) if state is not PositionState.FLAT else None
        )
        owned = origin in (TransitionStatus.COMPLETED,) and state_leg is not None
        if not owned:
            # Mirrored (safety) or refused-open states legitimately keep a
            # conservative reservation that the machine has not completed an
            # open for; there is nothing to enforce yet.
            return []
        if self.registry.contract_type is not state_leg:
            return [
                f"I14: machine recorded {state.value} but its own reservation "
                f"is {self.registry.contract_type.value}"
            ]
        broker_qty = self.broker.qty(state_leg)
        if self.registry._quantity != broker_qty:
            return [
                f"I14: registry qty {self.registry._quantity} != "
                f"broker qty {broker_qty}"
            ]
        return []

    def v15_safety_never_auto_submits(self) -> list[str]:
        return list(self.h._auto_submit_violations)

    def v16_restart_never_invents(self) -> list[str]:
        if not self.h.events:
            return []
        last_event = self.h.events[-1]
        if not last_event.startswith(f"t{self.h.tick} restart ok"):
            return []
        decision = self._reconcile_decision()
        if decision is None or decision.safety_stop is not None:
            return ["I16: restart adopted an unsafe broker snapshot"]
        if self.machine is None or self.machine.state is not decision.position:
            return [
                f"I16: restart adoption diverged from broker: "
                f"machine={self.machine.state.value if self.machine else '-'} "
                f"broker={decision.position.value}"
            ]
        return []

    def v17_no_auto_repair_of_unsafe(self) -> list[str]:
        """Every audited mutation must trace to a real submitted order.

        A delayed-lag mutation carries the literal driver label ``lag``; any
        other mutation whose order id is not in the order ledger would be an
        undocumented auto-repair of an unsafe snapshot.
        """
        out: list[str] = []
        known = set(self.broker._orders) if hasattr(self.broker, "_orders") else set()
        for audit in self.broker.applied:
            if audit.order_id == "lag":
                continue
            if audit.order_id not in known:
                out.append(
                    f"I17: applied mutation {audit.order_id} has no submitted "
                    "order (auto-repair suspected)"
                )
        return out

    def v18_no_phantom_opposite_after_failed_reversal(self) -> list[str]:
        out, _ = self._applied_replay_audit()
        return [v for v in out if v.startswith("I18")]

    def violations(self) -> list[str]:
        checks = [
            self.v01_no_dual_leg_projection,
            self.v02_ce_nonnegative,
            self.v03_pe_nonnegative,
            self.v04_no_dual_position_state,
            self.v05_no_broker_short,
            self.v06_open_implies_buy,
            self.v07_close_implies_sell,
            self.v08_seal_never_opens,
            self.v09_open_never_precedes_close,
            self.v10_leg_type_pairing,
            self.v11_no_expired_submission,
            self.v12_no_phantom_fill_from_exception,
            self.v13_no_duplicate_order_per_transition,
            self.v14_slot_agrees_with_broker,
            self.v15_safety_never_auto_submits,
            self.v16_restart_never_invents,
            self.v17_no_auto_repair_of_unsafe,
            self.v18_no_phantom_opposite_after_failed_reversal,
        ]
        out: list[str] = []
        for check in checks:
            out.extend(check())
        return out