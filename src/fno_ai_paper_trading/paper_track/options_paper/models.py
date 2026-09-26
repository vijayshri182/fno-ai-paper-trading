"""Deterministic models for the Phase 11 options paper-trading simulation.

The lifecycle is explicit and auditable: a lifecycle record advances through
``LifecyclePhase`` states via :data:`LIFECYCLE_TRANSITIONS` (analogous to the
existing ``ORDER_TRANSITIONS`` convention). Records carry entry/exit fills,
financials, marks, limitations and reconciliation status. Everything is frozen;
nothing here reads the clock, performs I/O or touches credentials.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum

from fno_ai_paper_trading.research.options.models import OptionChainSnapshot, OptionQuote
from fno_ai_paper_trading.research.quality.models import TradeQualityResult
from fno_ai_paper_trading.research.regime import MarketRegimeReport
from fno_ai_paper_trading.research.risk.models import RiskResult
from fno_ai_paper_trading.research.selection.models import ContractSelectionResult
from fno_ai_paper_trading.utils.functions import positive_int

__all__ = [
    "LIFECYCLE_TRANSITIONS",
    "TERMINAL_PHASES",
    "LifecyclePhase",
    "EntryEvent",
    "ExitEvent",
    "MarkEvent",
    "Step",
    "EntryFill",
    "ExitFill",
    "MarkToMarket",
    "Financials",
    "Reconciliation",
    "LifecycleRecord",
]


class LifecyclePhase(str, Enum):
    """Explicit, auditable states of one options paper-trade lifecycle."""

    CANDIDATE = "candidate"
    REJECTED = "rejected"
    PAPER_ORDER_CREATED = "paper order created"
    PAPER_FILLED = "paper filled"
    POSITION_OPEN = "position open"
    EXIT_REQUESTED = "exit requested"
    EXIT_FILLED = "exit filled"
    POSITION_CLOSED = "position closed"
    RECONCILED = "reconciled"


#: Deterministic lifecycle. Each phase maps to the set of phases it may move to.
#: Missing phases are terminal; any other move is an error (never repaired).
LIFECYCLE_TRANSITIONS: dict[LifecyclePhase, frozenset[LifecyclePhase]] = {
    LifecyclePhase.CANDIDATE: frozenset(
        {LifecyclePhase.REJECTED, LifecyclePhase.PAPER_ORDER_CREATED}
    ),
    LifecyclePhase.PAPER_ORDER_CREATED: frozenset({LifecyclePhase.PAPER_FILLED}),
    LifecyclePhase.PAPER_FILLED: frozenset({LifecyclePhase.POSITION_OPEN}),
    LifecyclePhase.POSITION_OPEN: frozenset({LifecyclePhase.EXIT_REQUESTED}),
    LifecyclePhase.EXIT_REQUESTED: frozenset({LifecyclePhase.EXIT_FILLED}),
    LifecyclePhase.EXIT_FILLED: frozenset({LifecyclePhase.POSITION_CLOSED}),
    LifecyclePhase.POSITION_CLOSED: frozenset({LifecyclePhase.RECONCILED}),
    LifecyclePhase.REJECTED: frozenset(),
    LifecyclePhase.RECONCILED: frozenset(),
}

TERMINAL_PHASES = frozenset({LifecyclePhase.REJECTED, LifecyclePhase.RECONCILED})

# Phase 11 is long-only, consistent with the Phase 10 long-bias risk engine.
SUPPORTED_DIRECTION = "LONG"


def _valid_event_id(event_id: str, name: str) -> str:
    if not isinstance(event_id, str) or not event_id.strip():
        raise ValueError(f"{name} event_id must be a non-empty string")
    return event_id


def _naive(dt: datetime, name: str) -> datetime:
    if dt.tzinfo is not None:
        raise ValueError(f"{name} must be a naive IST datetime")
    return dt


@dataclass(frozen=True)
class EntryEvent:
    """Explicit paper-entry event from the caller/test harness.

    Carries the Phases 5-10 results already produced at decision time. It is a
    simulation input — never a signal, never a recommendation, never an order.
    """

    event_id: str
    decision_timestamp: datetime
    quantity: int
    snapshot: OptionChainSnapshot
    regime: MarketRegimeReport
    selection: ContractSelectionResult
    quality: TradeQualityResult
    risk: RiskResult
    direction: str = SUPPORTED_DIRECTION
    note: str | None = None

    def __post_init__(self) -> None:
        _valid_event_id(self.event_id, "entry")
        _naive(self.decision_timestamp, "decision_timestamp")
        object.__setattr__(self, "quantity", positive_int(self.quantity, "quantity"))
        if self.direction != SUPPORTED_DIRECTION:
            raise ValueError(
                "Phase 11 supports a LONG (buy-to-open) direction only; "
                f"got {self.direction!r}"
            )


@dataclass(frozen=True)
class ExitEvent:
    """Explicit protective-exit request naming the open contract.

    ``quote`` is the observed market view at ``timestamp``; the fill only ever
    uses observed bid/ask/last data (never a fabricated exit price).
    """

    event_id: str
    timestamp: datetime
    quote: OptionQuote
    reason: str | None = "EXPLICIT"
    note: str | None = None

    def __post_init__(self) -> None:
        _valid_event_id(self.event_id, "exit")
        _naive(self.timestamp, "exit timestamp")


@dataclass(frozen=True)
class MarkEvent:
    """A point-in-time mark for unrealized P&L (only used when reliable)."""

    event_id: str
    timestamp: datetime
    quote: OptionQuote

    def __post_init__(self) -> None:
        _valid_event_id(self.event_id, "mark")
        _naive(self.timestamp, "mark timestamp")


@dataclass(frozen=True)
class Step:
    """One audited lifecycle step."""

    phase: LifecyclePhase
    at: datetime
    reason: str = ""

    def to_dict(self) -> dict:
        return {"phase": self.phase.value, "at": self.at.isoformat(), "reason": self.reason}


@dataclass(frozen=True)
class EntryFill:
    """Auditable simulated entry fill (derived from observed market data)."""

    order_id: str
    reference_used: str
    reference_price: Decimal
    observed_bid: Decimal | None
    observed_ask: Decimal | None
    observed_last: Decimal | None
    fill_price: Decimal
    slippage_rate: Decimal
    commission: Decimal
    filled_at: datetime
    premium_value: Decimal
    premium_exposure: Decimal
    candidate_premium_exposure: Decimal | None
    max_premium_exposure: Decimal | None

    def to_dict(self) -> dict:
        return {
            "order_id": self.order_id,
            "reference_used": self.reference_used,
            "reference_price": str(self.reference_price),
            "observed_bid": str(self.observed_bid) if self.observed_bid is not None else None,
            "observed_ask": str(self.observed_ask) if self.observed_ask is not None else None,
            "observed_last": str(self.observed_last) if self.observed_last is not None else None,
            "fill_price": str(self.fill_price),
            "slippage_rate": str(self.slippage_rate),
            "commission": str(self.commission),
            "filled_at": self.filled_at.isoformat(),
            "premium_value": str(self.premium_value),
            "premium_exposure": str(self.premium_exposure),
            "candidate_premium_exposure": str(self.candidate_premium_exposure)
            if self.candidate_premium_exposure is not None
            else None,
            "max_premium_exposure": str(self.max_premium_exposure)
            if self.max_premium_exposure is not None
            else None,
        }


@dataclass(frozen=True)
class ExitFill:
    """Auditable simulated protective-exit fill (observed data only)."""

    order_id: str
    reference_used: str
    reference_price: Decimal
    observed_bid: Decimal | None
    observed_ask: Decimal | None
    observed_last: Decimal | None
    fill_price: Decimal
    slippage_rate: Decimal
    commission: Decimal
    filled_at: datetime
    exit_reason: str

    def to_dict(self) -> dict:
        return {
            "order_id": self.order_id,
            "reference_used": self.reference_used,
            "reference_price": str(self.reference_price),
            "observed_bid": str(self.observed_bid) if self.observed_bid is not None else None,
            "observed_ask": str(self.observed_ask) if self.observed_ask is not None else None,
            "observed_last": str(self.observed_last) if self.observed_last is not None else None,
            "fill_price": str(self.fill_price),
            "slippage_rate": str(self.slippage_rate),
            "commission": str(self.commission),
            "filled_at": self.filled_at.isoformat(),
            "exit_reason": self.exit_reason,
        }


@dataclass(frozen=True)
class MarkToMarket:
    """A recorded mark; ``unrealized_pnl`` is only present for reliable refs."""

    timestamp: datetime
    reference_used: str
    reference_price: Decimal
    unrealized_pnl: Decimal

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp.isoformat(),
            "reference_used": self.reference_used,
            "reference_price": str(self.reference_price),
            "unrealized_pnl": str(self.unrealized_pnl),
        }


@dataclass(frozen=True)
class Financials:
    """Rupee financials on the Phase 10 exposure basis (qty x lot x multiplier)."""

    entry_commission: Decimal
    exit_commission: Decimal | None = None
    gross_realized_pnl: Decimal | None = None
    net_realized_pnl: Decimal | None = None
    holding_duration_seconds: int | None = None
    close_day: str | None = None
    marks: tuple[MarkToMarket, ...] = ()
    unrealized_latest: Decimal | None = None

    def to_dict(self) -> dict:
        return {
            "entry_commission": str(self.entry_commission),
            "exit_commission": str(self.exit_commission) if self.exit_commission is not None else None,
            "gross_realized_pnl": str(self.gross_realized_pnl)
            if self.gross_realized_pnl is not None
            else None,
            "net_realized_pnl": str(self.net_realized_pnl)
            if self.net_realized_pnl is not None
            else None,
            "holding_duration_seconds": self.holding_duration_seconds,
            "close_day": self.close_day,
            "marks": [m.to_dict() for m in self.marks],
            "unrealized_latest": str(self.unrealized_latest)
            if self.unrealized_latest is not None
            else None,
        }


@dataclass(frozen=True)
class Reconciliation:
    """Result of re-deriving the ledger identities from stored record data."""

    performed_at: datetime
    ok: bool
    violations: tuple[str, ...]

    def to_dict(self) -> dict:
        return {
            "performed_at": self.performed_at.isoformat(),
            "ok": self.ok,
            "violations": list(self.violations),
        }


@dataclass(frozen=True)
class LifecycleRecord:
    """The auditable lifecycle of one options paper trade."""

    event_id: str
    record_id: str
    decision_timestamp: datetime
    instrument_key: str
    contract_key: str
    underlying_symbol: str
    option_side: str
    strike: Decimal | None
    expiry: str
    quantity: int
    lot_size: int
    multiplier: int
    direction: str
    fingerprint: str | None
    phase: LifecyclePhase
    reasons: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    history: tuple[Step, ...] = ()
    entry: EntryFill | None = None
    exit: ExitFill | None = None
    financials: Financials | None = None
    reconciliation: Reconciliation | None = None

    def __post_init__(self) -> None:
        _naive(self.decision_timestamp, "decision_timestamp")
        object.__setattr__(self, "quantity", positive_int(self.quantity, "quantity"))

    @property
    def open_position(self) -> bool:
        return self.phase in (
            LifecyclePhase.POSITION_OPEN,
            LifecyclePhase.EXIT_REQUESTED,
        )

    @property
    def is_duplicate(self) -> bool:
        return any("duplicate_conflict" in r for r in self.reasons)

    def to_dict(self) -> dict:
        return {
            "event_id": self.event_id,
            "record_id": self.record_id,
            "decision_timestamp": self.decision_timestamp.isoformat(),
            "instrument_key": self.instrument_key,
            "contract_key": self.contract_key,
            "underlying_symbol": self.underlying_symbol,
            "option_side": self.option_side,
            "strike": str(self.strike) if self.strike is not None else None,
            "expiry": self.expiry,
            "quantity": self.quantity,
            "lot_size": self.lot_size,
            "multiplier": self.multiplier,
            "direction": self.direction,
            "fingerprint": self.fingerprint,
            "phase": self.phase.value,
            "reasons": list(self.reasons),
            "limitations": list(self.limitations),
            "history": [s.to_dict() for s in self.history],
            "entry": self.entry.to_dict() if self.entry is not None else None,
            "exit": self.exit.to_dict() if self.exit is not None else None,
            "financials": self.financials.to_dict() if self.financials is not None else None,
            "reconciliation": self.reconciliation.to_dict()
            if self.reconciliation is not None
            else None,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "LifecycleRecord":
        """Restore a record from the stable dict form (used by persistence)."""
        return cls(
            event_id=data["event_id"],
            record_id=data["record_id"],
            decision_timestamp=datetime.fromisoformat(data["decision_timestamp"]),
            instrument_key=data["instrument_key"],
            contract_key=data["contract_key"],
            underlying_symbol=data["underlying_symbol"],
            option_side=data["option_side"],
            strike=Decimal(data["strike"]) if data.get("strike") is not None else None,
            expiry=data["expiry"],
            quantity=int(data["quantity"]),
            lot_size=int(data["lot_size"]),
            multiplier=int(data["multiplier"]),
            direction=data["direction"],
            fingerprint=data.get("fingerprint"),
            phase=LifecyclePhase(data["phase"]),
            reasons=tuple(data.get("reasons") or ()),
            limitations=tuple(data.get("limitations") or ()),
            history=tuple(Step(LifecyclePhase(s["phase"]), datetime.fromisoformat(s["at"]), s.get("reason") or "") for s in data.get("history") or ()),
            entry=_entry_fill_from_dict(data.get("entry")),
            exit=_exit_fill_from_dict(data.get("exit")),
            financials=_financials_from_dict(data.get("financials")),
            reconciliation=_reconciliation_from_dict(data.get("reconciliation")),
        )


def _entry_fill_from_dict(data: dict | None) -> EntryFill | None:
    if not data:
        return None
    return EntryFill(
        order_id=data["order_id"],
        reference_used=data["reference_used"],
        reference_price=Decimal(data["reference_price"]),
        observed_bid=Decimal(data["observed_bid"]) if data.get("observed_bid") is not None else None,
        observed_ask=Decimal(data["observed_ask"]) if data.get("observed_ask") is not None else None,
        observed_last=Decimal(data["observed_last"]) if data.get("observed_last") is not None else None,
        fill_price=Decimal(data["fill_price"]),
        slippage_rate=Decimal(data["slippage_rate"]),
        commission=Decimal(data["commission"]),
        filled_at=datetime.fromisoformat(data["filled_at"]),
        premium_value=Decimal(data["premium_value"]),
        premium_exposure=Decimal(data["premium_exposure"]),
        candidate_premium_exposure=Decimal(data["candidate_premium_exposure"])
        if data.get("candidate_premium_exposure") is not None
        else None,
        max_premium_exposure=Decimal(data["max_premium_exposure"])
        if data.get("max_premium_exposure") is not None
        else None,
    )


def _exit_fill_from_dict(data: dict | None) -> ExitFill | None:
    if not data:
        return None
    return ExitFill(
        order_id=data["order_id"],
        reference_used=data["reference_used"],
        reference_price=Decimal(data["reference_price"]),
        observed_bid=Decimal(data["observed_bid"]) if data.get("observed_bid") is not None else None,
        observed_ask=Decimal(data["observed_ask"]) if data.get("observed_ask") is not None else None,
        observed_last=Decimal(data["observed_last"]) if data.get("observed_last") is not None else None,
        fill_price=Decimal(data["fill_price"]),
        slippage_rate=Decimal(data["slippage_rate"]),
        commission=Decimal(data["commission"]),
        filled_at=datetime.fromisoformat(data["filled_at"]),
        exit_reason=data["exit_reason"],
    )


def _financials_from_dict(data: dict | None) -> Financials | None:
    if not data:
        return None
    return Financials(
        entry_commission=Decimal(data["entry_commission"]),
        exit_commission=Decimal(data["exit_commission"])
        if data.get("exit_commission") is not None
        else None,
        gross_realized_pnl=Decimal(data["gross_realized_pnl"])
        if data.get("gross_realized_pnl") is not None
        else None,
        net_realized_pnl=Decimal(data["net_realized_pnl"])
        if data.get("net_realized_pnl") is not None
        else None,
        holding_duration_seconds=data.get("holding_duration_seconds"),
        close_day=data.get("close_day"),
        marks=tuple(
            MarkToMarket(
                datetime.fromisoformat(m["timestamp"]),
                m["reference_used"],
                Decimal(m["reference_price"]),
                Decimal(m["unrealized_pnl"]),
            )
            for m in data.get("marks") or ()
        ),
        unrealized_latest=Decimal(data["unrealized_latest"])
        if data.get("unrealized_latest") is not None
        else None,
    )


def _reconciliation_from_dict(data: dict | None) -> Reconciliation | None:
    if not data:
        return None
    return Reconciliation(
        performed_at=datetime.fromisoformat(data["performed_at"]),
        ok=bool(data["ok"]),
        violations=tuple(data.get("violations") or ()),
    )