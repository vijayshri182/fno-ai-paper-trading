"""Single-slot invariant enforcement.

The invariant is symbol-independent: a CE and a PE are two different
instruments but they still compete for the SAME directional option slot. A
process-wide ``SlotRegistry`` (shared across runs) enforces that no second run
may hold a different leg while the slot is occupied.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field

from fno_ai_paper_trading.execution.vnext.enums import ContractType
from fno_ai_paper_trading.execution.vnext.errors import SingleSlotViolationError


@dataclass
class SlotRegistry:
    """Process-wide single directional option slot (symbol-independent).

    A slot is held for a pair (holder_id, contract_type). While occupied, any
    different holder asking to open any leg is rejected; the same holder may
    continue its own sequencing (e.g. reversal) without re-acquiring.
    """

    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _holder_id: str | None = None
    _contract_type: ContractType | None = None
    _quantity: int = 0

    def acquire(
        self, holder_id: str, contract_type: ContractType, quantity: int
    ) -> None:
        with self._lock:
            if self._holder_id is None:
                self._holder_id = holder_id
                self._contract_type = contract_type
                self._quantity = quantity
                return
            if self._holder_id == holder_id:
                # Same holder re-acquiring during a reversal: refresh the held
                # instrument so the registry never reports a stale leg.
                self._contract_type = contract_type
                self._quantity = quantity
                return
            raise SingleSlotViolationError(
                "directional option slot already held by "
                f"{self._holder_id} ({self._contract_type.value})"
            )

    def release(self, holder_id: str) -> None:
        with self._lock:
            if self._holder_id is None:
                return
            if self._holder_id != holder_id:
                raise SingleSlotViolationError(
                    f"slot held by {self._holder_id}, cannot release by {holder_id}"
                )
            self._holder_id = None
            self._contract_type = None
            self._quantity = 0

    @property
    def holder_id(self) -> str | None:
        return self._holder_id

    @property
    def contract_type(self) -> ContractType | None:
        return self._contract_type

    @property
    def occupied(self) -> bool:
        return self._holder_id is not None

    def guard(self, holder_id: str) -> "SingleSlotGuard":
        """Convenience: build a per-run guard bound to this registry."""
        return SingleSlotGuard(self, holder_id)


class SingleSlotGuard:
    """Per-run guard sitting at the position-controller boundary.

    Rejects any OPEN that would violate the shared single-slot invariant and
    records intended quantity so reconciliation can verify it precisely.
    """

    def __init__(self, registry: SlotRegistry, holder_id: str) -> None:
        self._registry = registry
        self._holder_id = holder_id
        self._expected_contract_type: ContractType | None = None
        self._expected_quantity: int = 0

    @property
    def registry(self) -> SlotRegistry:
        return self._registry

    def check_can_open(self) -> None:
        """Raise if the slot is held by a different holder (pre-trade veto)."""
        if (
            self._registry.occupied
            and self._registry.holder_id != self._holder_id
        ):
            raise SingleSlotViolationError(
                "single-slot invariant: another run holds the directional "
                "option slot"
            )

    def record_open(self, contract_type: ContractType, quantity: int) -> None:
        """Acquire/refresh slot ownership with the intended instrument."""
        self._registry.acquire(self._holder_id, contract_type, quantity)
        self._expected_contract_type = contract_type
        self._expected_quantity = quantity

    def record_close(self) -> None:
        """Release the slot only if this guard actually recorded an open.

        Idempotent: a guard that never acquired (or already released) the slot
        is a no-op. Releasing a slot held by a DIFFERENT holder still raises —
        that would indicate cross-run corruption.
        """
        if self._expected_contract_type is None:
            return
        self._registry.release(self._holder_id)
        self._expected_contract_type = None
        self._expected_quantity = 0

    @property
    def expected_contract_type(self) -> ContractType | None:
        return self._expected_contract_type

    @property
    def expected_quantity(self) -> int:
        return self._expected_quantity