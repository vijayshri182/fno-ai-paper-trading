"""Shared exception types for the isolated vNext execution layer."""
from __future__ import annotations


class VNextError(Exception):
    """Base class for all vNext execution-layer errors."""


class InvalidOrderSemanticsError(VNextError):
    """An order violated the vNext order semantics (e.g. PUT=SELL as OPEN).

    The vNext model forbids opening by SELL (no naked short call/put) and
    forbids any PUT/CE or CALL/PE pairing.
    """


class ContractResolutionError(VNextError):
    """A contract could not be resolved for a requested option leg."""


class SingleSlotViolationError(VNextError):
    """A second option position was requested while the slot is held.

    The single directional option slot is system-wide and symbol-independent:
    a CE and a PE are different instruments but still compete for the same slot.
    """


class ReconciliationError(VNextError):
    """Broker reconciliation produced an ambiguous or unsafe position."""