"""Position-state reconciliation helpers.

Broker snapshots are raw quantities; the machine reconciles them into the
single-slot ``PositionState`` without ever assuming a fill.
"""
from __future__ import annotations

from dataclasses import dataclass

from fno_ai_paper_trading.execution.vnext.broker import PositionSnapshot
from fno_ai_paper_trading.execution.vnext.enums import (
    ContractType,
    PositionState,
    SafetyStopReason,
)
from fno_ai_paper_trading.execution.vnext.errors import ReconciliationError


@dataclass(frozen=True)
class ReconcileDecision:
    """Result of reconciling a broker snapshot into a single-slot position."""

    position: PositionState
    safety_stop: SafetyStopReason | None = None


def _quantity_of(
    snapshot: PositionSnapshot, contract_type: ContractType
) -> int:
    return sum(
        detail.quantity
        for detail in snapshot.details
        if detail.contract.contract_type is contract_type
    )


def reconcile_to_state(
    snapshot: PositionSnapshot,
) -> ReconcileDecision:
    """Map raw broker quantities to the single-slot position state.

    Rules:
      * any negative (short) quantity          -> UNEXPECTED_SHORT safety stop
      * any non-integer quantity (float/str)   -> INVALID_QUANTITY safety stop
      * CE>0 AND PE>0                           -> AMBIGUOUS_POSITIONS stop
      * CE>0 only / PE>0 only                   -> LONG_CALL / LONG_PUT
      * both zero                               -> FLAT
    A malformed detail (missing contract fields) raises ``ReconciliationError``
    rather than being silently summed. The machine never auto-repairs an
    ambiguous or invalid snapshot.
    """
    ce_qty = 0
    pe_qty = 0
    try:
        for detail in snapshot.details:
            quantity = detail.quantity
            if isinstance(quantity, bool) or not isinstance(quantity, int):
                return ReconcileDecision(
                    PositionState.FLAT, SafetyStopReason.INVALID_QUANTITY
                )
            contract_type = detail.contract.contract_type
            if contract_type is ContractType.CE:
                ce_qty += quantity
            elif contract_type is ContractType.PE:
                pe_qty += quantity
    except (AttributeError, TypeError) as exc:
        raise ReconciliationError(
            f"malformed broker snapshot: {exc!r}"
        ) from exc

    if ce_qty < 0 or pe_qty < 0:
        return ReconcileDecision(
            PositionState.FLAT, SafetyStopReason.UNEXPECTED_SHORT
        )
    if ce_qty > 0 and pe_qty > 0:
        return ReconcileDecision(
            PositionState.FLAT, SafetyStopReason.AMBIGUOUS_POSITIONS
        )
    if ce_qty > 0:
        return ReconcileDecision(PositionState.LONG_CALL)
    if pe_qty > 0:
        return ReconcileDecision(PositionState.LONG_PUT)
    return ReconcileDecision(PositionState.FLAT)


def position_to_contract_type(position: PositionState) -> ContractType:
    """The contract type a position state holds (LONG_CALL->CE, LONG_PUT->PE)."""
    if position is PositionState.LONG_CALL:
        return ContractType.CE
    if position is PositionState.LONG_PUT:
        return ContractType.PE
    raise ValueError(f"{position.value} holds no open contract")


def expect_single_position(
    snapshot: PositionSnapshot,
    contract_type: ContractType,
) -> None:
    """Raise unless the snapshot holds exactly the expected leg, positive qty.

    Used to confirm a fill actually materialised the intended position (no
    phantom fill, no blind assume of a market order).
    """
    decision = reconcile_to_state(snapshot)
    if decision.safety_stop is not None:
        raise ReconciliationError(
            f"unsafe snapshot: {decision.safety_stop.value}"
        )
    expected = (
        PositionState.LONG_CALL
        if contract_type is ContractType.CE
        else PositionState.LONG_PUT
    )
    if decision.position is not expected:
        raise ReconciliationError(
            f"expected {expected.value} {contract_type.value}, got {decision.position.value}"
        )