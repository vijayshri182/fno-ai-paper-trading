"""Execution-semantics vNext — isolated TEST SPECIFICATION.

This module encodes the PROPOSED vNext model ONLY. It is fully self-contained:
local pure helpers, no import of any production module, no modification of any
existing test or source file. It pins the design so a future implementation has
an agreed, executable acceptance spec, and it documents that the vNext model
DIFFERS from the current WS 7.9 mapping (CALL->BUY, PUT->SELL).
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import pytest

# --------------------------------------------------------------- vocabulary


class Mandate(Enum):
    """Proposed signal input vocabulary (F&O expression input)."""

    LONG = "LONG"
    SHORT = "SHORT"
    FLAT = "FLAT"


class Contract(Enum):
    """Option contract identity — a name, NEVER a transaction direction."""

    CE = "CE"  # Call
    PE = "PE"  # Put
    NONE = "NONE"


class TxSide(Enum):
    """Broker transaction direction — BUY or SELL."""

    BUY = "BUY"
    SELL = "SELL"


class Position(Enum):
    """Single-slot option position state (at most one open position system-wide)."""

    FLAT = "FLAT"
    LONG_CALL = "LONG_CALL"
    LONG_PUT = "LONG_PUT"


class InvalidMappingError(ValueError):
    """A rejected instrument/state pairing under the vNext model."""


@dataclass(frozen=True)
class OrderStep:
    """One ordered execution step of a transition."""

    action: str  # "OPEN" | "CLOSE"
    contract: Contract
    tx: TxSide


# ------------------------------------------------------------- pure helpers


def resolve_contract(mandate: Mandate) -> Contract:
    """LONG -> CE, SHORT -> PE, FLAT -> NONE."""
    if mandate is Mandate.LONG:
        return Contract.CE
    if mandate is Mandate.SHORT:
        return Contract.PE
    return Contract.NONE


def entry_transaction(mandate: Mandate) -> TxSide:
    """Entry is ALWAYS a buy in vNext: BUY CALL for LONG, BUY PUT for SHORT.

    Never PUT = SELL. SHORT enters by BUY PE (long put), not by writing one.
    """
    if mandate is Mandate.FLAT:
        raise InvalidMappingError("FLAT has no entry transaction")
    return TxSide.BUY


def close_transaction(contract: Contract) -> TxSide:
    """Closing the held contract is always SELL (CE or PE)."""
    if contract is Contract.NONE:
        raise InvalidMappingError("NONE has no close transaction")
    return TxSide.SELL


def open_contract_for(state: Position) -> Contract:
    """The contract a position state holds; rejects illegal pairings."""
    if state is Position.LONG_CALL:
        return Contract.CE
    if state is Position.LONG_PUT:
        return Contract.PE
    raise InvalidMappingError(f"{state.value} holds no open contract")


def plan(position: Position, mandate: Mandate) -> tuple[list[OrderStep], Position]:
    """Ordered transition plan (section C table). SELL-before-BUY on reversal."""
    if position is Position.FLAT and mandate is Mandate.FLAT:
        return [], Position.FLAT
    if position is Position.FLAT:
        contract = resolve_contract(mandate)
        return [
            OrderStep(action="OPEN", contract=contract, tx=TxSide.BUY),
        ], Position.LONG_CALL if contract is Contract.CE else Position.LONG_PUT
    if position is Position.LONG_CALL and mandate is Mandate.LONG:
        return [], Position.LONG_CALL
    if position is Position.LONG_PUT and mandate is Mandate.SHORT:
        return [], Position.LONG_PUT
    if position is Position.LONG_CALL and mandate is Mandate.FLAT:
        return [
            OrderStep(action="CLOSE", contract=Contract.CE, tx=TxSide.SELL),
        ], Position.FLAT
    if position is Position.LONG_PUT and mandate is Mandate.FLAT:
        return [
            OrderStep(action="CLOSE", contract=Contract.PE, tx=TxSide.SELL),
        ], Position.FLAT
    if position is Position.LONG_CALL and mandate is Mandate.SHORT:
        return [
            OrderStep(action="CLOSE", contract=Contract.CE, tx=TxSide.SELL),
            OrderStep(action="OPEN", contract=Contract.PE, tx=TxSide.BUY),
        ], Position.LONG_PUT
    if position is Position.LONG_PUT and mandate is Mandate.LONG:
        return [
            OrderStep(action="CLOSE", contract=Contract.PE, tx=TxSide.SELL),
            OrderStep(action="OPEN", contract=Contract.CE, tx=TxSide.BUY),
        ], Position.LONG_CALL
    raise InvalidMappingError(f"illegal transition {position.value} + {mandate.value}")


def apply_with_confirmations(
    position: Position,
    mandate: Mandate,
    *,
    close_confirmed: bool = True,
    entry_confirmed: bool = True,
) -> Position:
    """Simulate a transition honouring confirmation gates (section D invariants).

    Reversal ordering: the CLOSE step must be confirmed BEFORE the OPEN step is
    attempted; a failed close blocks the opposite leg entirely. A failed entry
    leaves the system at the state AFTER any executed close (never an unheld
    opposite leg, never a phantom fill).
    """
    steps, target = plan(position, mandate)
    held = position
    for step in steps:
        if step.action == "CLOSE":
            if not close_confirmed:
                return held
            held = Position.FLAT
            continue
        if step.action == "OPEN":
            if not entry_confirmed:
                return held
            held = target
    return held


def open_contracts_count(steps: list[OrderStep]) -> int:
    """Number of OPEN steps in a plan — never more than one in vNext."""
    return sum(1 for step in steps if step.action == "OPEN")


def validate_held_contract(state: Position, contract: Contract) -> None:
    """Instrument-consistency: LONG_PUT requires PE, LONG_CALL requires CE."""
    expected = open_contract_for(state)
    if contract is not expected:
        raise InvalidMappingError(
            f"{state.value} requires {expected.value}, got {contract.value}"
        )


# ---------------------------------------------------------- A: signal mapping


class TestASignalMapping:
    def test_long_resolves_call(self):
        assert resolve_contract(Mandate.LONG) is Contract.CE

    def test_short_resolves_put(self):
        assert resolve_contract(Mandate.SHORT) is Contract.PE

    def test_flat_resolves_none(self):
        assert resolve_contract(Mandate.FLAT) is Contract.NONE

    def test_long_entry_is_buy_call(self):
        assert entry_transaction(Mandate.LONG) is TxSide.BUY

    def test_short_entry_is_buy_put_not_sell(self):
        # The core divergence: SHORT -> BUY PE, NEVER PUT = SELL.
        assert entry_transaction(Mandate.SHORT) is TxSide.BUY

    def test_flat_has_no_entry(self):
        with pytest.raises(InvalidMappingError):
            entry_transaction(Mandate.FLAT)

    def test_never_put_equals_sell(self):
        # PUT is a contract name; SELL as entry would write/close, never open.
        for mandate in (Mandate.LONG, Mandate.SHORT):
            assert entry_transaction(mandate) is TxSide.BUY


# ---------------------------------------------------------- B: closing


class TestBSignalClosing:
    def test_long_call_flat_closes_call(self):
        steps, target = plan(Position.LONG_CALL, Mandate.FLAT)
        assert steps == [OrderStep("CLOSE", Contract.CE, TxSide.SELL)]
        assert target is Position.FLAT

    def test_long_put_flat_closes_put(self):
        steps, target = plan(Position.LONG_PUT, Mandate.FLAT)
        assert steps == [OrderStep("CLOSE", Contract.PE, TxSide.SELL)]
        assert target is Position.FLAT

    def test_same_side_is_hold(self):
        assert plan(Position.LONG_CALL, Mandate.LONG) == ([], Position.LONG_CALL)
        assert plan(Position.LONG_PUT, Mandate.SHORT) == ([], Position.LONG_PUT)

    def test_close_is_always_sell(self):
        assert close_transaction(Contract.CE) is TxSide.SELL
        assert close_transaction(Contract.PE) is TxSide.SELL


# ---------------------------------------------------------- C: reversal


class TestCReversalOrdering:
    def test_call_to_put_closes_first(self):
        steps, target = plan(Position.LONG_CALL, Mandate.SHORT)
        assert [s.action for s in steps] == ["CLOSE", "OPEN"]
        assert steps[0] == OrderStep("CLOSE", Contract.CE, TxSide.SELL)
        assert steps[1] == OrderStep("OPEN", Contract.PE, TxSide.BUY)
        assert target is Position.LONG_PUT

    def test_put_to_call_closes_first(self):
        steps, target = plan(Position.LONG_PUT, Mandate.LONG)
        assert [s.action for s in steps] == ["CLOSE", "OPEN"]
        assert steps[0] == OrderStep("CLOSE", Contract.PE, TxSide.SELL)
        assert steps[1] == OrderStep("OPEN", Contract.CE, TxSide.BUY)
        assert target is Position.LONG_CALL

    def test_reversal_never_opens_before_close_confirmed(self):
        for position, mandate in (
            (Position.LONG_CALL, Mandate.SHORT),
            (Position.LONG_PUT, Mandate.LONG),
        ):
            result = apply_with_confirmations(
                position, mandate, close_confirmed=False, entry_confirmed=True
            )
            assert result is position

    def test_entry_failure_after_close_leaves_flat(self):
        for position, mandate in (
            (Position.LONG_CALL, Mandate.SHORT),
            (Position.LONG_PUT, Mandate.LONG),
        ):
            result = apply_with_confirmations(
                position, mandate, close_confirmed=True, entry_confirmed=False
            )
            assert result is Position.FLAT


# ---------------------------------------------------------- D: failure safety


class TestDFailureSafety:
    def test_open_from_flat_needs_entry_confirmation(self):
        assert (
            apply_with_confirmations(
                Position.FLAT, Mandate.LONG, entry_confirmed=False
            )
            is Position.FLAT
        )
        assert (
            apply_with_confirmations(
                Position.FLAT, Mandate.SHORT, entry_confirmed=False
            )
            is Position.FLAT
        )

    def test_hold_never_places_an_order(self):
        for position, mandate in (
            (Position.FLAT, Mandate.FLAT),
            (Position.LONG_CALL, Mandate.LONG),
            (Position.LONG_PUT, Mandate.SHORT),
        ):
            steps, _ = plan(position, mandate)
            assert steps == []

    def test_no_phantom_position_on_failed_fill(self):
        assert apply_with_confirmations(
            Position.FLAT, Mandate.LONG, entry_confirmed=False
        ) is Position.FLAT
        assert apply_with_confirmations(
            Position.FLAT, Mandate.SHORT, entry_confirmed=False
        ) is Position.FLAT


# ---------------------------------------------------------- E: single-slot


class TestESingleSlot:
    def test_at_most_one_open_step_per_plan(self):
        for position in Position:
            for mandate in Mandate:
                try:
                    steps, _ = plan(position, mandate)
                except InvalidMappingError:
                    continue
                assert open_contracts_count(steps) <= 1

    def test_states_are_mutually_exclusive(self):
        states = {Position.LONG_CALL, Position.LONG_PUT, Position.FLAT}
        assert len(states) == 3

    def test_never_call_and_put_open_simultaneously(self):
        for position in (Position.LONG_CALL, Position.LONG_PUT):
            for mandate in (Mandate.LONG, Mandate.SHORT, Mandate.FLAT):
                try:
                    steps, _ = plan(position, mandate)
                except InvalidMappingError:
                    continue
                contracts = {s.contract for s in steps if s.action == "OPEN"}
                assert len(contracts) <= 1

    def test_each_open_issued_only_from_flat(self):
        for mandate in (Mandate.LONG, Mandate.SHORT):
            steps, _ = plan(Position.FLAT, mandate)
            assert [s.action for s in steps] == ["OPEN"]


# ---------------------------------------------------------- F: instrument consistency


class TestFInstrumentConsistency:
    def test_put_pairing_requires_pe(self):
        validate_held_contract(Position.LONG_PUT, Contract.PE)  # legal
        with pytest.raises(InvalidMappingError):
            validate_held_contract(Position.LONG_PUT, Contract.CE)  # reject PUT+CALL

    def test_buy_put_on_ce_rejected(self):
        # Opening SHORT resolves PE; an OPEN step on CE for SHORT is a defect.
        steps, _ = plan(Position.FLAT, Mandate.SHORT)
        assert steps[0].contract is Contract.PE
        assert steps[0].tx is TxSide.BUY

    def test_long_put_state_requires_pe_instrument(self):
        open_contract_for(Position.LONG_PUT)
        with pytest.raises(InvalidMappingError):
            open_contract_for(Position.FLAT)

    def test_call_pairing_requires_ce(self):
        validate_held_contract(Position.LONG_CALL, Contract.CE)  # legal
        with pytest.raises(InvalidMappingError):
            validate_held_contract(Position.LONG_CALL, Contract.PE)  # reject CALL+PE

    def test_flat_requires_no_open_contract(self):
        with pytest.raises(InvalidMappingError):
            open_contract_for(Position.FLAT)


# ------------------------------------------------- G: current-behavior compatibility pin


class TestGCompatibilityPin:
    def test_vnext_diverges_from_current_pinned_mapping(self):
        # Current WS 7.9: CallPutSignal.order_side CALL->BUY, PUT->SELL
        # (execution/signal.py:30-34). vNext fixes 'PUT' as long-put entry.
        assert entry_transaction(Mandate.SHORT) is TxSide.BUY
        assert entry_transaction(Mandate.SHORT) is not TxSide.SELL

    def test_vnext_needs_isolated_harness_change(self):
        # A future 'forced PUT' under vNext is BUY(PE) -> SELL(PE), which is a
        # behavior change vs today's SELL(PE) -> BUY(PE) script path and vs the
        # CE-based forced test fixture. It must live behind its own switch.
        steps, _ = plan(Position.FLAT, Mandate.SHORT)
        assert [s.tx for s in steps] == [TxSide.BUY]
        close_steps, _ = plan(Position.LONG_PUT, Mandate.FLAT)
        assert [s.tx for s in close_steps] == [TxSide.SELL]