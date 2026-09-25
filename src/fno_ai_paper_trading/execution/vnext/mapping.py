"""Pure mapping from a signal direction to the vNext contract+side.

The mapping is deliberately PURE and mirrored 1:1 from
``tests/test_execution_semantics_vnext_spec.py``:

    LONG  -> CALL -> CE -> BUY
    SHORT -> PUT  -> PE -> BUY
    FLAT  -> NONE

A direction alone never expresses SELL. SELL is only added when the position
holder closes a held contract (see ``plan_orders``).
"""
from __future__ import annotations

from dataclasses import dataclass

from fno_ai_paper_trading.execution.vnext.enums import (
    ContractType,
    OptionLeg,
    OrderAction,
    PositionState,
    SignalDirection,
    TxSide,
)
from fno_ai_paper_trading.execution.vnext.order_semantics import (
    close_side_for_leg,
    entry_side_for_leg,
    leg_to_contract_type,
)


def direction_to_leg(direction: SignalDirection) -> OptionLeg:
    """LONG->CALL, SHORT->PUT, FLAT->NONE."""
    if direction is SignalDirection.LONG:
        return OptionLeg.CALL
    if direction is SignalDirection.SHORT:
        return OptionLeg.PUT
    return OptionLeg.NONE


@dataclass(frozen=True)
class OrderStep:
    """One ordered execution step of a transition.

    Combined with a resolved contract, the machine converts the step into an
    ``OrderRequest`` (action + contract + side + quantity). Ordering of steps
    matters (reversal: SELL the held leg BEFORE any BUY of the opposite leg).
    """

    order_action: OrderAction
    option_leg: OptionLeg
    tx_side: TxSide
    contract_type: ContractType

    def __post_init__(self) -> None:
        if self.contract_type is not leg_to_contract_type(self.option_leg):
            raise ValueError(
                "instrument leg/contract mismatch in OrderStep"
            )
        if self.order_action is OrderAction.OPEN and self.tx_side is TxSide.SELL:
            raise ValueError("OPEN must be BUY in vNext")


def plan_orders(
    position: PositionState, direction: SignalDirection
) -> tuple[list[OrderStep], PositionState]:
    """Return the ordered steps and resulting state for a (state, signal) pair.

    Mirrors the pinned table in
    ``docs/execution_semantics_vnext_design.md`` section C (rows 1-9).
    """
    leg = direction_to_leg(direction)

    if position is PositionState.FLAT and leg is OptionLeg.NONE:
        return [], PositionState.FLAT
    if position is PositionState.FLAT and leg is OptionLeg.CALL:
        return [
            OrderStep(
                OrderAction.OPEN,
                OptionLeg.CALL,
                entry_side_for_leg(OptionLeg.CALL),
                leg_to_contract_type(OptionLeg.CALL),
            )
        ], PositionState.LONG_CALL
    if position is PositionState.FLAT and leg is OptionLeg.PUT:
        return [
            OrderStep(
                OrderAction.OPEN,
                OptionLeg.PUT,
                entry_side_for_leg(OptionLeg.PUT),
                leg_to_contract_type(OptionLeg.PUT),
            )
        ], PositionState.LONG_PUT
    if position is PositionState.LONG_CALL and leg is OptionLeg.CALL:
        return [], PositionState.LONG_CALL  # HOLD
    if position is PositionState.LONG_PUT and leg is OptionLeg.PUT:
        return [], PositionState.LONG_PUT  # HOLD
    if position is PositionState.LONG_CALL and leg is OptionLeg.NONE:
        return [
            OrderStep(
                OrderAction.CLOSE,
                OptionLeg.CALL,
                close_side_for_leg(OptionLeg.CALL),
                leg_to_contract_type(OptionLeg.CALL),
            )
        ], PositionState.FLAT
    if position is PositionState.LONG_PUT and leg is OptionLeg.NONE:
        return [
            OrderStep(
                OrderAction.CLOSE,
                OptionLeg.PUT,
                close_side_for_leg(OptionLeg.PUT),
                leg_to_contract_type(OptionLeg.PUT),
            )
        ], PositionState.FLAT
    if position is PositionState.LONG_CALL and leg is OptionLeg.PUT:
        # Reversal: SELL CE (close), confirm FLAT, then BUY PE (open).
        return [
            OrderStep(
                OrderAction.CLOSE,
                OptionLeg.CALL,
                close_side_for_leg(OptionLeg.CALL),
                leg_to_contract_type(OptionLeg.CALL),
            ),
            OrderStep(
                OrderAction.OPEN,
                OptionLeg.PUT,
                entry_side_for_leg(OptionLeg.PUT),
                leg_to_contract_type(OptionLeg.PUT),
            ),
        ], PositionState.LONG_PUT
    if position is PositionState.LONG_PUT and leg is OptionLeg.CALL:
        # Reversal: SELL PE (close), confirm FLAT, then BUY CE (open).
        return [
            OrderStep(
                OrderAction.CLOSE,
                OptionLeg.PUT,
                close_side_for_leg(OptionLeg.PUT),
                leg_to_contract_type(OptionLeg.PUT),
            ),
            OrderStep(
                OrderAction.OPEN,
                OptionLeg.CALL,
                entry_side_for_leg(OptionLeg.CALL),
                leg_to_contract_type(OptionLeg.CALL),
            ),
        ], PositionState.LONG_CALL
    raise ValueError(f"illegal transition {position.value}+{direction.value}")