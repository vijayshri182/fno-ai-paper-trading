"""Order semantics for the vNext layer.

Encodes the core invariant: PUT does NOT mean SELL. OPEN is BUY only; SELL is
only ever a closing transaction. CALL<->CE and PUT<->PE pairings are enforced.
"""
from __future__ import annotations

from dataclasses import dataclass

from fno_ai_paper_trading.execution.vnext.enums import (
    ContractType,
    OptionLeg,
    OrderAction,
    TxSide,
)
from fno_ai_paper_trading.execution.vnext.errors import InvalidOrderSemanticsError

#: Leg contract pairing: CALL must resolve to CE, PUT to PE.
_LEG_CONTRACT_TYPE: dict[OptionLeg, ContractType] = {
    OptionLeg.CALL: ContractType.CE,
    OptionLeg.PUT: ContractType.PE,
}

#: Entry transactions are always BUY in vNext — the direction never flips PUT
#: into SELL.
_ENTRY_SIDE: dict[OptionLeg, TxSide] = {
    OptionLeg.CALL: TxSide.BUY,
    OptionLeg.PUT: TxSide.BUY,
}

#: Closing transactions sell the held contract back.
_CLOSE_SIDE: dict[OptionLeg, TxSide] = {
    OptionLeg.CALL: TxSide.SELL,
    OptionLeg.PUT: TxSide.SELL,
}


def leg_to_contract_type(option_leg: OptionLeg) -> ContractType:
    """CALL -> CE, PUT -> PE. NONE has no contract."""
    try:
        return _LEG_CONTRACT_TYPE[option_leg]
    except KeyError:
        raise InvalidOrderSemanticsError(
            f"{option_leg.value} has no contract type"
        ) from None


def entry_side_for_leg(option_leg: OptionLeg) -> TxSide:
    """Entry is BUY for both CALL and PUT (never PUT=SELL)."""
    try:
        return _ENTRY_SIDE[option_leg]
    except KeyError:
        raise InvalidOrderSemanticsError(
            f"{option_leg.value} cannot be opened"
        ) from None


def close_side_for_leg(option_leg: OptionLeg) -> TxSide:
    """Closing always sells the held contract back."""
    try:
        return _CLOSE_SIDE[option_leg]
    except KeyError:
        raise InvalidOrderSemanticsError(
            f"{option_leg.value} cannot be closed"
        ) from None


def validate_order_semantics(
    *,
    action: OrderAction,
    option_leg: OptionLeg,
    contract_type: ContractType,
    tx_side: TxSide,
) -> None:
    """Enforce the vNext order-semantics invariants.

    - OPEN is BUY only: PUT+SELL as OPEN and CALL+SELL as OPEN are rejected.
    - CLOSE is SELL only: BUY is never a closing transaction.
    - The instrument pairing is enforced: CALL must resolve to CE, PUT to PE.
    - A leg may never be paired with the other contract type (PUT+CE, CALL+PE),
      and a contract type may never be bought against the wrong leg.
    """
    if option_leg is OptionLeg.NONE:
        raise InvalidOrderSemanticsError("NONE leg has no order semantics")

    expected = leg_to_contract_type(option_leg)
    if contract_type is not expected:
        raise InvalidOrderSemanticsError(
            f"{option_leg.value} paired with {contract_type.value} is invalid; "
            f"expected {expected.value}"
        )
    if action is OrderAction.OPEN:
        if tx_side is not TxSide.BUY:
            raise InvalidOrderSemanticsError(
                f"OPEN {option_leg.value}+{tx_side.value} rejected: "
                "vNext never opens on a SELL"
            )
    elif action is OrderAction.CLOSE:
        if tx_side is not TxSide.SELL:
            raise InvalidOrderSemanticsError(
                f"CLOSE {option_leg.value}+{tx_side.value} rejected: "
                "vNext never closes with a BUY"
            )
    else:
        raise InvalidOrderSemanticsError(f"unknown action {action}")