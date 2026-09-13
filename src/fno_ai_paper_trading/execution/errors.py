"""Typed errors for the live-execution integration test layer (WS 7.9).

The manager maps every failure into a typed exception so the caller always sees
structured, action-failing outcomes instead of silent state corruption.
"""


class ExecutionError(Exception):
    """Base error for the execution-test layer."""


class UpstoxExecutionError(ExecutionError):
    """The Upstox brokerage returned an unexpected error for a request."""


class UpstoxOrderRejectedError(ExecutionError):
    """Upstox rejected the order with an explicit rejection status/reason."""


class OrderTimeoutError(ExecutionError):
    """An order did not reach its expected status within the poll budget."""


class ReconciliationError(ExecutionError):
    """Post-exit positions did not reconcile to FLAT."""


class InstrumentValidationError(ExecutionError):
    """The F&O instrument failed validation (expiry/strike/option type/lot)."""