"""Exponential-backoff retry helper with deterministic testing support.

Retrying is intentionally injectable: callers pass ``sleep`` so tests can run
without real delays. By default only the exceptions listed in ``exceptions`` are
retried, so non-transient errors propagate immediately.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, TypeVar

T = TypeVar("T")


class RetryExhausted(RuntimeError):
    """Raised when all retry attempts have been exhausted.

    The final attempt's exception is available as ``__cause__``.
    """


def retry_call(
    fn: Callable[[], T],
    *,
    attempts: int = 3,
    delay: float = 0.1,
    backoff: float = 2.0,
    max_delay: float = 5.0,
    exceptions: tuple[type[BaseException], ...] = (Exception,),
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Call ``fn``, retrying on ``exceptions`` with exponential backoff.

    ``attempts`` is the total number of calls (1 = no retry). Between calls the
    thread sleeps for ``delay`` seconds, doubling up to ``max_delay``. When every
    attempt fails, the last error is re-raised wrapped in :class:`RetryExhausted`.
    """
    if attempts < 1:
        raise ValueError("attempts must be >= 1")

    last_error: BaseException | None = None
    wait = delay
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except exceptions as exc:  # noqa: BLE001 - retry only what the caller opts into
            last_error = exc
            if attempt == attempts:
                break
            sleep(max(0.0, wait))
            wait = min(wait * backoff, max_delay)

    assert last_error is not None  # attempts >= 1 guarantees at least one failure
    raise RetryExhausted(f"retry exhausted after {attempts} attempt(s)") from last_error


def describe_last_error(exc: RetryExhausted) -> BaseException | None:
    """Return the cause of a :class:`RetryExhausted` (the final attempt's error)."""
    cause = exc.__cause__
    while cause is not None and isinstance(cause, RetryExhausted):
        cause = cause.__cause__
    return cause