"""Single-entry/single-exit state machine for the live execution test (WS 7.9).

The machine is deliberately strict. It mechanically enforces the hard
safeguards of §17p *independently of* the caller's logic:

* at most **one entry** order may ever be given,
* at most **one exit** order may ever be given,
* no averaging and no re-entry after an exit,
* a completed/failed/aborted test must halt before anything else can happen.

Every transition must be an allowed edge; anything else raises ``ValueError``
and leaves state untouched (same integrity philosophy as ``ORDER_TRANSITIONS``).
"""
from __future__ import annotations

from enum import Enum

#: Terminal states: no further trading activity is possible after reaching them.
TERMINAL_STATES: frozenset["ExecutionTestState"] = frozenset(
    {"COMPLETE", "FAILED", "ABORTED", "HALTED"}
)

#: States in which a fresh entry may still be given (and only one).
_PRE_ENTRY_STATES: frozenset["ExecutionTestState"] = frozenset(
    {"INSTRUMENT_VALIDATED"}
)

#: States in which a fresh exit may still be given (and only one).
_PRE_EXIT_STATES: frozenset["ExecutionTestState"] = frozenset({"HOLDING"})


class ExecutionTestState(str, Enum):
    """One step of the controlled live execution experiment."""

    IDLE = "IDLE"
    AUTHENTICATING = "AUTHENTICATING"
    INSTRUMENT_VALIDATED = "INSTRUMENT_VALIDATED"
    ENTRY_REQUESTED = "ENTRY_REQUESTED"
    ENTRY_ACKNOWLEDGED = "ENTRY_ACKNOWLEDGED"
    ENTRY_FILLED = "ENTRY_FILLED"
    HOLDING = "HOLDING"
    EXIT_REQUESTED = "EXIT_REQUESTED"
    EXIT_ACKNOWLEDGED = "EXIT_ACKNOWLEDGED"
    EXIT_FILLED = "EXIT_FILLED"
    FLAT_RECONCILED = "FLAT_RECONCILED"
    COMPLETE = "COMPLETE"
    ABORTED = "ABORTED"
    FAILED = "FAILED"
    HALTED = "HALTED"


ALLOWED_TRANSITIONS: frozenset[tuple[ExecutionTestState, ExecutionTestState]] = frozenset(
    {
        (ExecutionTestState.IDLE, ExecutionTestState.AUTHENTICATING),
        (ExecutionTestState.AUTHENTICATING, ExecutionTestState.INSTRUMENT_VALIDATED),
        (ExecutionTestState.AUTHENTICATING, ExecutionTestState.FAILED),  # auth failure
        (ExecutionTestState.AUTHENTICATING, ExecutionTestState.ABORTED),  # preflight fail
        (ExecutionTestState.INSTRUMENT_VALIDATED, ExecutionTestState.ENTRY_REQUESTED),
        (ExecutionTestState.INSTRUMENT_VALIDATED, ExecutionTestState.ABORTED),  # preflight fail
        (ExecutionTestState.ENTRY_REQUESTED, ExecutionTestState.ENTRY_ACKNOWLEDGED),
        (ExecutionTestState.ENTRY_REQUESTED, ExecutionTestState.FAILED),  # ack failure
        (ExecutionTestState.ENTRY_ACKNOWLEDGED, ExecutionTestState.ENTRY_FILLED),
        (ExecutionTestState.ENTRY_ACKNOWLEDGED, ExecutionTestState.FAILED),  # fill timeout
        (ExecutionTestState.ENTRY_FILLED, ExecutionTestState.HOLDING),
        (ExecutionTestState.HOLDING, ExecutionTestState.EXIT_REQUESTED),
        (ExecutionTestState.HOLDING, ExecutionTestState.FAILED),
        (ExecutionTestState.EXIT_REQUESTED, ExecutionTestState.EXIT_ACKNOWLEDGED),
        (ExecutionTestState.EXIT_REQUESTED, ExecutionTestState.FAILED),
        (ExecutionTestState.EXIT_ACKNOWLEDGED, ExecutionTestState.EXIT_FILLED),
        (ExecutionTestState.EXIT_ACKNOWLEDGED, ExecutionTestState.FAILED),
        (ExecutionTestState.EXIT_FILLED, ExecutionTestState.FLAT_RECONCILED),
        (ExecutionTestState.EXIT_FILLED, ExecutionTestState.FAILED),  # reconcile failure
        (ExecutionTestState.FLAT_RECONCILED, ExecutionTestState.COMPLETE),
    }
)


class ExecutionTestStateMachine:
    """Enforces the single-entry/single-exit lifecycle of one test run."""

    def __init__(self, initial: ExecutionTestState = ExecutionTestState.IDLE) -> None:
        if not isinstance(initial, ExecutionTestState):
            raise TypeError("initial must be an ExecutionTestState")
        if initial not in TERMINAL_STATES:
            self._state = initial
        else:
            raise ValueError("a state machine must not start in a terminal state")
        self._entry_given = 0
        self._exit_given = 0
        self.entry_order_id: str | None = None
        self.exit_order_id: str | None = None

    @property
    def state(self) -> ExecutionTestState:
        return self._state

    @property
    def entry_given(self) -> int:
        return self._entry_given

    @property
    def exit_given(self) -> int:
        return self._exit_given

    @property
    def halted(self) -> bool:
        return self._state is ExecutionTestState.HALTED

    @property
    def is_terminal(self) -> bool:
        return self._state in TERMINAL_STATES

    @property
    def can_give_entry(self) -> bool:
        return (
            not self.is_terminal
            and self._entry_given == 0
            and self._state in _PRE_ENTRY_STATES
        )

    @property
    def can_give_exit(self) -> bool:
        return (
            not self.is_terminal
            and self._exit_given == 0
            and self._state in _PRE_EXIT_STATES
        )

    def transition(self, target: ExecutionTestState, reason: str = "") -> "ExecutionTestStateMachine":
        """Move to ``target`` along an allowed edge (no entry/exit obligations).

        The entry and exit request edges raise here — they must go through
        :meth:`give_entry` / :meth:`give_exit` so the single-use counters are
        maintained with the transition atomically.
        """
        if target in (ExecutionTestState.ENTRY_REQUESTED, ExecutionTestState.EXIT_REQUESTED):
            raise ValueError(
                f"use give_entry()/give_exit() for {target.value}; a bare transition "
                "would bypass the single-use safeguards"
            )
        self._validate(target, reason)
        self._state = target
        return self

    def give_entry(self, order_id: str) -> "ExecutionTestStateMachine":
        """Record the (single) entry order id and move to ENTRY_REQUESTED."""
        order_id = (order_id or "").strip()
        if not order_id:
            raise ValueError("entry requires an order id")
        if not self.can_give_entry:
            raise ValueError(
                "entry refused: the machine allows exactly one entry, and only "
                f"before any exit (state={self._state.value}, entry_given={self._entry_given})"
            )
        self._validate(ExecutionTestState.ENTRY_REQUESTED, "entry")
        self._entry_given += 1
        self.entry_order_id = order_id
        self._state = ExecutionTestState.ENTRY_REQUESTED
        return self

    def give_exit(self, order_id: str) -> "ExecutionTestStateMachine":
        """Record the (single) exit order id and move to EXIT_REQUESTED."""
        order_id = (order_id or "").strip()
        if not order_id:
            raise ValueError("exit requires an order id")
        if not self.can_give_exit:
            raise ValueError(
                "exit refused: the machine allows exactly one exit, and only "
                f"while a filled entry is held (state={self._state.value}, exit_given={self._exit_given})"
            )
        self._validate(ExecutionTestState.EXIT_REQUESTED, "exit")
        self._exit_given += 1
        self.exit_order_id = order_id
        self._state = ExecutionTestState.EXIT_REQUESTED
        return self

    def fail(self, reason: str = "failed") -> "ExecutionTestStateMachine":
        """Move to FAILED from any live state (raises from terminal states)."""
        if self._state in frozenset({ExecutionTestState.FAILED, ExecutionTestState.COMPLETE, ExecutionTestState.HALTED, ExecutionTestState.ABORTED}):
            raise ValueError(f"cannot fail an already-terminal machine ({self._state.value})")
        return self.transition(ExecutionTestState.FAILED, reason)

    def abort(self, reason: str = "aborted") -> "ExecutionTestStateMachine":
        """Move to ABORTED (pre-trade rejections) from a live, pre-entry state."""
        if self._state not in (ExecutionTestState.AUTHENTICATING, ExecutionTestState.INSTRUMENT_VALIDATED):
            raise ValueError(f"abort only allowed before an entry is given (state={self._state.value})")
        return self.transition(ExecutionTestState.ABORTED, reason)

    def _validate(self, target: ExecutionTestState, reason: str) -> None:
        if not isinstance(target, ExecutionTestState):
            raise TypeError(f"target must be an ExecutionTestState, got {type(target).__name__}")
        if self.is_terminal and self._state is not ExecutionTestState.HALTED:
            raise ValueError(
                f"terminal state {self._state.value} forbids further transitions"
            )
        if (self._state, target) not in ALLOWED_TRANSITIONS:
            raise ValueError(
                f"illegal execution-test transition {self._state.value!r} -> {target.value!r}"
            )
        _ = reason  # kept for future metadata; determinism is unchanged