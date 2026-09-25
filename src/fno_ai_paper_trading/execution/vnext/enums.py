"""vNext vocabulary — explicitly separated from the WS 7.9 harness enums.

These enums intentionally reuse the design-stage names from
``docs/execution_semantics_vnext_design.md`` and
``tests/test_execution_semantics_vnext_spec.py`` so the implementation is a
faithful 1:1 refinement of the pinned spec.
"""
from __future__ import annotations

from enum import Enum


class SignalDirection(str, Enum):
    """Signal direction (LONG / SHORT / FLAT)."""

    LONG = "LONG"
    SHORT = "SHORT"
    FLAT = "FLAT"


class OptionLeg(str, Enum):
    """Option expression (CALL / PUT / NONE)."""

    CALL = "CALL"
    PUT = "PUT"
    NONE = "NONE"


class ContractType(str, Enum):
    """Option contract type: CE (call) or PE (put)."""

    CE = "CE"
    PE = "PE"


class TxSide(str, Enum):
    """Broker transaction direction — BUY or SELL."""

    BUY = "BUY"
    SELL = "SELL"


class OrderAction(str, Enum):
    """Order intent — OPEN or CLOSE."""

    OPEN = "OPEN"
    CLOSE = "CLOSE"


class PositionState(str, Enum):
    """Single-slot directional option position state."""

    FLAT = "FLAT"
    LONG_CALL = "LONG_CALL"
    LONG_PUT = "LONG_PUT"


class OrderStatus(str, Enum):
    """Broker order lifecycle status."""

    SUBMITTED = "SUBMITTED"
    PENDING = "PENDING"
    FILLED = "FILLED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    UNFILLED = "UNFILLED"


class SafetyStopReason(str, Enum):
    """Why the machine entered a hard safety stop (no opposite leg opened)."""

    CLOSE_REJECTED = "CLOSE_REJECTED"
    CLOSE_UNFILLED = "CLOSE_UNFILLED"
    ENTRY_REJECTED = "ENTRY_REJECTED"
    ENTRY_UNFILLED = "ENTRY_UNFILLED"
    RECONCILIATION_NOT_FLAT = "RECONCILIATION_NOT_FLAT"
    AMBIGUOUS_POSITIONS = "AMBIGUOUS_POSITIONS"
    UNEXPECTED_SHORT = "UNEXPECTED_SHORT"
    DUPLICATE_LEG = "DUPLICATE_LEG"
    INVALID_QUANTITY = "INVALID_QUANTITY"
    RECONCILIATION_DISAGREEMENT = "RECONCILIATION_DISAGREEMENT"
    BROKER_UNAVAILABLE = "BROKER_UNAVAILABLE"
    RESOLUTION_FAILED = "RESOLUTION_FAILED"


class TransitionStatus(str, Enum):
    """Outcome of processing one signal through the state machine."""

    COMPLETED = "COMPLETED"
    HELD = "HELD"
    REVERSED = "REVERSED"
    STOPPED = "STOPPED"
    ENTRY_FAILED_FLAT = "ENTRY_FAILED_FLAT"
    SAFETY_STOP = "SAFETY_STOP"
    SLOT_VIOLATION = "SLOT_VIOLATION"