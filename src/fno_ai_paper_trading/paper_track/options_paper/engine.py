"""Phase 11 — options paper-trading lifecycle engine.

Consumes explicit entry/exit events carrying the Phases 5-10 results, enforces
the upstream gates, fingerprints, contract identity and timestamps, then
simulates the paper lifecycle through :class:`LifecyclePhase` states using the
existing :class:`PaperBroker` (order lifecycle, slippage, commission) and the
``paper_track`` session policy.

Safety contract:

* strictly paper-only: the only broker used is ``PaperBroker`` (``is_live`` is
  hard-coded ``False``); ``execution.*`` (Upstox adapter, OAuth, live gate,
  live prep, audit/signal) and credentials are never touched by this module;
* no wall-clock reads: the engine requires an injected ``now_fn`` — all times
  come from injected events and the injected clock;
* upstream results are never re-run, never reinterpreted and never silently
  repaired: gate/identity/fingerprint/timestamp mismatches reject the attempt;
* unavailable upstream results produce no simulated trade and preserve status;
* no options stop-loss is invented (Phase 10 ``STOP_MODEL_NOT_YET_DEFINED`` is
  surfaced as a rejected/unavailable attempt); premium exposure is never
  equated with stop-loss risk;
* fill prices are only ever derived from observed bid/ask/last data; missing,
  crossed or otherwise invalid references are never fabricated.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from typing import Callable

from fno_ai_paper_trading.broker.paper_broker import PaperBroker, PaperBrokerConfig
from fno_ai_paper_trading.models.enums import OrderSide
from fno_ai_paper_trading.models.market import MarketPrice
from fno_ai_paper_trading.models.order import Order
from fno_ai_paper_trading.paper_track.options_paper.fill_model import (
    FILL_MODEL_VERSION,
    fill_reference,
    mark_reference,
    premium_exposure,
)
from fno_ai_paper_trading.paper_track.options_paper.models import (
    EntryEvent,
    EntryFill,
    ExitEvent,
    ExitFill,
    Financials,
    LIFECYCLE_TRANSITIONS,
    LifecyclePhase,
    LifecycleRecord,
    MarkEvent,
    MarkToMarket,
    Reconciliation,
    Step,
)
from fno_ai_paper_trading.paper_track.policy import TRACK_SESSION, SessionPolicy
from fno_ai_paper_trading.research.options.models import OptionQuote
from fno_ai_paper_trading.research.quality import selection_fingerprint

__all__ = ["OptionsPaperConfig", "OptionsPaperEngine"]


@dataclass(frozen=True)
class OptionsPaperConfig:
    """Deterministic engine configuration (all inputs injected)."""

    broker_config: PaperBrokerConfig = PaperBrokerConfig()
    session_policy: SessionPolicy = TRACK_SESSION
    enforce_session_gate: bool = True
    fill_model_version: str = FILL_MODEL_VERSION


def _reference_bar(quote: OptionQuote, reference: Decimal) -> MarketPrice:
    """Synthetic fill-reference carrier: OHLC all equal to one observed value."""
    return MarketPrice(
        instrument=quote.instrument,
        timestamp=quote.timestamp,
        open=reference,
        high=reference,
        low=reference,
        close=reference,
        volume=0,
    )


class OptionsPaperEngine:
    """Deterministic options paper lifecycle + ledger + reconciliation."""

    def __init__(
        self,
        *,
        now_fn: Callable[[], datetime],
        config: OptionsPaperConfig | None = None,
    ) -> None:
        if now_fn is None:
            raise ValueError(
                "OptionsPaperEngine requires an injected now_fn (no wall-clock reads)"
            )
        self.config = config if config is not None else OptionsPaperConfig()
        self._now: Callable[[], datetime] = now_fn
        self._broker = PaperBroker(self.config.broker_config, now_fn=now_fn)
        self._by_event: dict[str, LifecycleRecord] = {}
        self._by_id: dict[str, LifecycleRecord] = {}

    # ------------------------------------------------------------------ #
    # public API
    # ------------------------------------------------------------------ #

    def submit_entry(self, event: EntryEvent) -> tuple[LifecycleRecord, bool]:
        """Process one explicit paper-entry event.

        Returns ``(record, created)``. Re-submitting the identical event is
        idempotent (``created=False``, never double-filled). A conflicting
        duplicate ``event_id`` is recorded as a rejected attempt and the
        original record is left untouched.
        """
        existing = self._by_event.get(event.event_id)
        if existing is not None:
            if self._equivalent(existing, event):
                return existing, False
            conflicting = self._conflict_rejected(existing, event)
            self._record(conflicting)
            return conflicting, True

        skeleton = self._skeleton(event)

        gate = self.config.session_policy.gate(event.decision_timestamp)
        if self.config.enforce_session_gate and not gate.allows_entries:
            rejected = self._reject(
                skeleton,
                f"entry outside trading session (gate {gate.gate.value})",
                at=event.decision_timestamp,
            )
            self._record(rejected)
            return rejected, True

        reasons, meta = self._validate_entry(event)
        if reasons:
            rejected = self._reject(skeleton, reasons, at=event.decision_timestamp)
            self._record(rejected)
            return rejected, True

        quote = meta["quote"]
        ref_used, ref_price, ref_reason = fill_reference(quote, OrderSide.BUY)
        if ref_reason is not None:
            rejected = self._reject(skeleton, ref_reason, at=event.decision_timestamp)
            self._record(rejected)
            return rejected, True

        order = Order(
            instrument=quote.instrument,
            side=OrderSide.BUY,
            quantity=event.quantity,
            created_at=event.decision_timestamp,
        )
        ordered = self._advance(
            skeleton, LifecyclePhase.PAPER_ORDER_CREATED,
            "candidate validated; paper entry order created",
            at=event.decision_timestamp,
        )
        fill = self._broker.place_order(order, _reference_bar(quote, ref_price))
        if fill is None:
            raise RuntimeError("paper broker unexpectedly returned no fill")

        filled = self._advance(
            ordered, LifecyclePhase.PAPER_FILLED,
            f"paper entry fill {fill.order_id}", at=fill.filled_at,
        )
        opened = self._advance(
            filled, LifecyclePhase.POSITION_OPEN,
            "position open (long)", at=fill.filled_at,
        )
        entry_fill = self._entry_fill(quote, event.risk, ref_used, ref_price, fill)
        stop_model = event.risk.stop_model
        opened = replace(
            opened,
            entry=entry_fill,
            financials=Financials(entry_commission=fill.commission),
            limitations=opened.limitations
            + (f"no paper stop-loss simulated; risk stop_model={stop_model}",),
        )
        self._record(opened)
        return opened, True

    def request_exit(self, event: ExitEvent) -> tuple[LifecycleRecord | None, bool]:
        """Request a protective exit for the matching open contract.

        Returns ``(record, applied)``; ``record`` is ``None`` when no open
        position matches the exit quote's contract key (no state change).
        A required exit whose data is unavailable becomes ``EXIT_REQUESTED``
        with a recorded limitation — never a fabricated exit price.
        """
        candidates = [
            r for r in self._by_event.values()
            if r.open_position and r.contract_key == event.quote.key
        ]
        if not candidates:
            return None, False
        record = candidates[0]

        if event.timestamp < record.entry.filled_at:
            record = replace(
                record,
                limitations=record.limitations
                + ("exit timestamp precedes entry fill; exit ignored",),
            )
            self._record(record)
            return record, False

        gate = self.config.session_policy.gate(event.timestamp)
        if self.config.enforce_session_gate and not gate.allows_exits:
            record = replace(
                record,
                limitations=record.limitations
                + (f"exit outside session gate ({gate.gate.value}); exit ignored",),
            )
            self._record(record)
            return record, False

        ref_used, ref_price, ref_reason = fill_reference(event.quote, OrderSide.SELL)
        if ref_reason is not None:
            if record.phase is LifecyclePhase.EXIT_REQUESTED:
                # Previously data-starved exit retried with no new data: stay
                # EXIT_REQUESTED, only append the limitation (no re-advance).
                requested = replace(
                    record,
                    limitations=record.limitations
                    + (ref_reason + "; no exit fill — position remains open",),
                )
            else:
                requested = self._advance(
                    record, LifecyclePhase.EXIT_REQUESTED,
                    f"exit requested ({event.reason or 'EXPLICIT'})", at=event.timestamp,
                )
                requested = replace(
                    requested,
                    limitations=requested.limitations
                    + (ref_reason + "; no exit fill — position remains open",),
                )
            self._record(requested)
            return requested, True

        order = Order(
            instrument=event.quote.instrument,
            side=OrderSide.SELL,
            quantity=record.quantity,
            created_at=event.timestamp,
        )
        if record.phase is LifecyclePhase.EXIT_REQUESTED:
            # Retry of a previously data-starved exit with now-valid data:
            # re-file from EXIT_REQUESTED rather than re-advancing.
            requested = record
        else:
            requested = self._advance(
                record, LifecyclePhase.EXIT_REQUESTED,
                f"exit requested ({event.reason or 'EXPLICIT'})", at=event.timestamp,
            )
        fill = self._broker.place_order(order, _reference_bar(event.quote, ref_price))
        if fill is None:
            raise RuntimeError("paper broker unexpectedly returned no fill")

        exit_filled = self._advance(
            requested, LifecyclePhase.EXIT_FILLED,
            f"paper exit fill {fill.order_id}", at=fill.filled_at,
        )
        closed = self._advance(
            exit_filled, LifecyclePhase.POSITION_CLOSED,
            f"position closed ({event.reason or 'EXPLICIT'})", at=fill.filled_at,
        )
        exit_fill = self._exit_fill(event, ref_used, ref_price, fill)
        closed = replace(
            closed,
            exit=exit_fill,
            financials=self._close_financials(record, exit_fill),
        )
        self._record(closed)
        return closed, True

    def mark_market(self, record_id: str, event: MarkEvent) -> LifecycleRecord | None:
        """Apply a point-in-time mark for unrealized P&L (reliable data only)."""
        record = self._by_id.get(record_id)
        if record is None or not record.open_position:
            return None
        if record.contract_key != event.quote.key:
            record = replace(
                record,
                limitations=record.limitations + ("mark on a different contract; ignored",),
            )
            self._record(record)
            return record
        ref_used, ref_price, ref_reason = mark_reference(event.quote)
        if ref_reason is not None:
            record = replace(
                record,
                limitations=record.limitations + (ref_reason + "; no unrealized P&L",),
            )
            self._record(record)
            return record
        unrealized = (
            (ref_price - record.entry.fill_price)
            * record.quantity * record.lot_size * record.multiplier
        )
        mark = MarkToMarket(event.timestamp, ref_used, ref_price, unrealized)
        if record.financials is not None:
            financials = replace(
                record.financials,
                marks=record.financials.marks + (mark,),
                unrealized_latest=unrealized,
            )
        else:
            financials = Financials(
                entry_commission=record.entry.commission,
                marks=(mark,),
                unrealized_latest=unrealized,
            )
        record = replace(record, financials=financials)
        self._record(record)
        return record

    def reconcile(self, record_id: str) -> LifecycleRecord | None:
        """Re-derive the ledger identities from stored data and, only when the
        record is closed and clean, mark it ``RECONCILED``. Violations and
        open-position attempts are recorded, never repaired."""
        record = self._by_id.get(record_id)
        if record is None:
            return None
        performed_at = self._now()
        if record.phase is not LifecyclePhase.POSITION_CLOSED:
            failed = replace(
                record,
                limitations=record.limitations
                + ("reconciliation attempted while the position is not closed; unresolved",),
                reconciliation=Reconciliation(
                    performed_at, False, ("position is not POSITION_CLOSED; cannot reconcile",)
                ),
            )
            self._record(failed)
            return failed

        violations = self._verify_closed(record)
        if violations:
            failed = replace(
                record,
                limitations=record.limitations
                + ("reconciliation reported violations: " + "; ".join(violations),),
                reconciliation=Reconciliation(performed_at, False, tuple(violations)),
            )
            self._record(failed)
            return failed

        reconciled = self._advance(
            record, LifecyclePhase.RECONCILED,
            "reconciliation clean", at=performed_at,
        )
        reconciled = replace(
            reconciled, reconciliation=Reconciliation(performed_at, True, ())
        )
        self._record(reconciled)
        return reconciled

    # ------------------------------------------------------------------ #
    # read-only views / state
    # ------------------------------------------------------------------ #

    def record(self, record_id: str) -> LifecycleRecord | None:
        return self._by_id.get(record_id)

    def record_for_event(self, event_id: str) -> LifecycleRecord | None:
        return self._by_event.get(event_id)

    def records(self) -> tuple[LifecycleRecord, ...]:
        return tuple(self._by_event.values())

    def open_positions(self) -> tuple[LifecycleRecord, ...]:
        return tuple(r for r in self._by_event.values() if r.open_position)

    def broker_fills(self) -> int:
        return len(self._broker.fills)

    def counts(self) -> dict[str, int]:
        return {
            "total": len(self._by_event),
            "rejected": sum(
                1 for r in self._by_event.values() if r.phase is LifecyclePhase.REJECTED
            ),
            "open": len(self.open_positions()),
            "closed": sum(
                1 for r in self._by_event.values()
                if r.phase in (LifecyclePhase.POSITION_CLOSED, LifecyclePhase.RECONCILED)
            ),
            "reconciled": sum(
                1 for r in self._by_event.values() if r.phase is LifecyclePhase.RECONCILED
            ),
        }

    # ------------------------------------------------------------------ #
    # internals
    # ------------------------------------------------------------------ #

    def _record(self, record: LifecycleRecord) -> None:
        # A conflicting-duplicate attempt is auditable by record_id but must
        # never clobber the original event's lifecycle mapping.
        if not record.record_id.endswith(".dup"):
            self._by_event[record.event_id] = record
        self._by_id[record.record_id] = record

    def restore(self, records: Sequence[LifecycleRecord]) -> int:
        """Restore previously persisted records (restart recovery).

        Refuses to silently overwrite an existing record or to restore two
        records claiming the same event id. Returns the number of records
        restored.
        """
        for record in records:
            if record.record_id in self._by_id:
                raise ValueError(f"refusing restore: duplicate record {record.record_id}")
            existing = self._by_event.get(record.event_id)
            if existing is not None and existing.record_id != record.record_id:
                raise ValueError(f"refusing restore: duplicate event {record.event_id}")
            self._record(record)
        return len(records)

    def _reject(
        self, record: LifecycleRecord, why: str | Sequence[str], *, at: datetime
    ) -> LifecycleRecord:
        """Advance to REJECTED carrying auditable ``reasons`` (history too)."""
        reasons = tuple(why) if not isinstance(why, str) else (why,)
        rejected = self._advance(
            record, LifecyclePhase.REJECTED, "; ".join(reasons), at=at
        )
        return replace(rejected, reasons=reasons)

    def _equivalent(self, existing: LifecycleRecord, event: EntryEvent) -> bool:
        return (
            existing.contract_key == event.selection.selected_key
            and existing.decision_timestamp == event.decision_timestamp
            and existing.quantity == event.quantity
            and existing.fingerprint == self._fingerprint(event)
        )

    def _fingerprint(self, event: EntryEvent) -> str | None:
        return selection_fingerprint(event.selection)

    def _skeleton(self, event: EntryEvent) -> LifecycleRecord:
        quote = event.selection.selected_contract
        underlying = (
            quote.instrument.underlying_symbol
            if quote is not None
            else event.snapshot.underlying.symbol
        )
        return LifecycleRecord(
            event_id=event.event_id,
            record_id=f"record-{event.event_id}",
            decision_timestamp=event.decision_timestamp,
            instrument_key=quote.key if quote is not None else f"{underlying}|unknown",
            contract_key=quote.key if quote is not None else None,
            underlying_symbol=underlying,
            option_side=quote.side.value if quote is not None else "UNKNOWN",
            strike=quote.strike if quote is not None else None,
            expiry=quote.expiry.isoformat() if quote is not None else "",
            quantity=event.quantity,
            lot_size=quote.instrument.lot_size if quote is not None else 0,
            multiplier=quote.instrument.multiplier if quote is not None else 1,
            direction=event.direction,
            fingerprint=self._fingerprint(event),
            phase=LifecyclePhase.CANDIDATE,
            history=(
                Step(LifecyclePhase.CANDIDATE, event.decision_timestamp, "candidate received"),
            ),
        )

    def _conflict_rejected(self, existing: LifecycleRecord, event: EntryEvent) -> LifecycleRecord:
        """Explicitly audited conflicting duplicate (original untouched)."""
        rejected = self._skeleton(event)
        rejected = replace(
            rejected,
            record_id=f"record-{event.event_id}.dup",
            phase=LifecyclePhase.REJECTED,
            reasons=(
                "duplicate_conflict: event_id already processed with a different payload",
            ),
            history=rejected.history
            + (Step(LifecyclePhase.REJECTED, event.decision_timestamp, "duplicate conflict"),),
        )
        return rejected

    def _validate_entry(self, event: EntryEvent) -> tuple[list[str], dict]:
        reasons: list[str] = []
        meta: dict = {}
        selection, quality, risk = event.selection, event.quality, event.risk

        # --- Phase 5-10 gates (never re-run; never bypassed) --------------
        if not selection.has_selection or selection.selected_contract is None:
            reasons.append(
                f"phase-8 gate: selection did not select a contract (outcome {selection.outcome.value})"
            )
            return reasons, meta
        if not quality.has_passed:
            reasons.append(f"phase-9 gate: quality not PASS (outcome {quality.outcome.value})")
        if not risk.is_eligible:
            if risk.outcome.value == "UNAVAILABLE":
                reasons.append(
                    "phase-10 gate: risk UNAVAILABLE — no simulated trade "
                    "(STOP_MODEL_NOT_YET_DEFINED preserved)"
                )
            else:
                reasons.append(f"phase-10 gate: risk not ELIGIBLE (outcome {risk.outcome.value})")
        if not event.regime.has_regime:
            reasons.append("phase-7 gate: no valid regime (data_state unavailable)")

        quote = selection.selected_contract
        meta["quote"] = quote
        if not any(q.key == quote.key for q in event.snapshot.quotes):
            reasons.append("snapshot does not contain the selected contract")

        # --- identity / fingerprint pins (never silently repaired) --------
        fingerprint = self._fingerprint(event)
        if quality.selection_fingerprint is None or quality.selection_fingerprint != fingerprint:
            reasons.append("fingerprint mismatch: quality does not pin the supplied selection")
        if risk.selection_fingerprint is None or risk.selection_fingerprint != fingerprint:
            reasons.append("fingerprint mismatch: risk does not pin the supplied selection")
        keys = {selection.selected_key, quality.contract_key, risk.contract_key}
        if len(keys) != 1 or None in keys:
            reasons.append("contract identity mismatch across selection/quality/risk")

        # --- timeline / timestamp pins -------------------------------------
        if risk.timestamp != event.decision_timestamp:
            reasons.append(
                "decision timestamp mismatch: risk evaluated at "
                f"{risk.timestamp.isoformat()}, event at {event.decision_timestamp.isoformat()}"
            )
        if (
            selection.timestamp is not None
            and quality.timestamp is not None
            and not (selection.timestamp <= quality.timestamp <= event.decision_timestamp)
        ):
            reasons.append("timeline mismatch: selection/quality not before decision")
        if quote.expiry is not None and quote.expiry < event.decision_timestamp.date():
            reasons.append("contract expired at decision time")

        # --- quantity / lot-size / exposure pins ---------------------------
        if risk.lot_size is None or risk.lot_size != quote.instrument.lot_size:
            reasons.append("lot size mismatch: risk and provider master disagree")
        if risk.allowed_quantity is None or event.quantity > risk.allowed_quantity:
            reasons.append("quantity exceeds the risk cap (allowed_quantity)")
        premium = quote.last_price
        if premium is None:
            reasons.append("no decision premium (last_price missing); exposure never fabricated")
        else:
            exposure = premium_exposure(
                premium, event.quantity, quote.instrument.lot_size, quote.instrument.multiplier
            )
            meta["premium"] = premium
            meta["exposure"] = exposure
            if (
                risk.candidate_premium_exposure is not None
                and risk.candidate_premium_exposure != exposure
            ):
                reasons.append("premium exposure mismatch: risk engine exposure differs")

        return reasons, meta

    def _entry_fill(
        self, quote: OptionQuote, risk, ref_used: str, ref_price: Decimal, fill
    ) -> EntryFill:
        instrument = quote.instrument
        premium_value = fill.quantity * fill.price * instrument.multiplier
        exposure = premium_exposure(
            quote.last_price, fill.quantity, instrument.lot_size, instrument.multiplier
        )
        return EntryFill(
            order_id=fill.order_id,
            reference_used=ref_used,
            reference_price=ref_price,
            observed_bid=quote.bid,
            observed_ask=quote.ask,
            observed_last=quote.last_price,
            fill_price=fill.price,
            slippage_rate=self.config.broker_config.slippage_rate,
            commission=fill.commission,
            filled_at=fill.filled_at,
            premium_value=premium_value,
            premium_exposure=exposure,
            candidate_premium_exposure=risk.candidate_premium_exposure,
            max_premium_exposure=risk.max_premium_exposure,
        )

    def _exit_fill(self, event: ExitEvent, ref_used: str, ref_price: Decimal, fill) -> ExitFill:
        return ExitFill(
            order_id=fill.order_id,
            reference_used=ref_used,
            reference_price=ref_price,
            observed_bid=event.quote.bid,
            observed_ask=event.quote.ask,
            observed_last=event.quote.last_price,
            fill_price=fill.price,
            slippage_rate=self.config.broker_config.slippage_rate,
            commission=fill.commission,
            filled_at=fill.filled_at,
            exit_reason=event.reason or "EXPLICIT",
        )

    def _close_financials(self, record: LifecycleRecord, exit_fill: ExitFill) -> Financials:
        entry = record.entry
        gross = (
            (exit_fill.fill_price - entry.fill_price)
            * record.quantity * record.lot_size * record.multiplier
        )
        net = gross - (entry.commission + exit_fill.commission)
        holding = int((exit_fill.filled_at - entry.filled_at).total_seconds())
        marks = record.financials.marks if record.financials is not None else ()
        return Financials(
            entry_commission=entry.commission,
            exit_commission=exit_fill.commission,
            gross_realized_pnl=gross,
            net_realized_pnl=net,
            holding_duration_seconds=holding,
            close_day=exit_fill.filled_at.date().isoformat(),
            marks=marks,
            unrealized_latest=None,
        )

    def _verify_closed(self, record: LifecycleRecord) -> list[str]:
        violations: list[str] = []
        if record.entry is None or record.exit is None or record.financials is None:
            violations.append("missing entry/exit/financials; cannot reconcile")
            return violations
        expected_gross = (
            (record.exit.fill_price - record.entry.fill_price)
            * record.quantity * record.lot_size * record.multiplier
        )
        if record.financials.gross_realized_pnl != expected_gross:
            violations.append("gross realized P&L mismatch")
        expected_net = expected_gross - record.entry.commission - record.exit.commission
        if record.financials.net_realized_pnl != expected_net:
            violations.append("net realized P&L mismatch")
        if (
            record.entry.candidate_premium_exposure is not None
            and record.entry.premium_exposure != record.entry.candidate_premium_exposure
        ):
            violations.append("premium exposure does not match the risk engine's cross-check")
        history = record.history
        for previous, current in zip(history, history[1:]):
            if current.phase not in LIFECYCLE_TRANSITIONS[previous.phase]:
                violations.append(
                    f"invalid lifecycle transition {previous.phase.value} -> {current.phase.value}"
                )
        return violations

    def _advance(
        self, record: LifecycleRecord, phase: LifecyclePhase, reason: str, *, at: datetime
    ) -> LifecycleRecord:
        if phase not in LIFECYCLE_TRANSITIONS[record.phase]:
            raise ValueError(
                f"invalid lifecycle transition {record.phase.value} -> {phase.value}"
            )
        return replace(
            record,
            phase=phase,
            history=record.history + (Step(phase, at, reason),),
        )