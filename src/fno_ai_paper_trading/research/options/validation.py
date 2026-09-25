"""Strict data-quality layer for the options research stack (Phase 5).

Every input is classified into an explicit tri-state:

* ``VALID``      — all required semantics hold and no available field is bad;
* ``INVALID``    — at least one *present* value breaks a sanity rule (crossed
  bid/ask, negative OI, stale timestamp, duplicate contract, inconsistent
  expiry/CE-PE identity, ...);
* ``UNAVAILABLE`` — nothing is invalid but the provider did not supply some
  field(s); that absence is explicit and never fabricated.

No-fabrication rule enforced here: an absent field is reported as
``UNAVAILABLE`` with its name; the layer never invents OI/volume/IV/greeks,
never substitutes the underlying spot, and never copies a value from another
quote.

The validator is pure and deterministic: same snapshot + same reference time
=> byte-identical reports (no clock reads inside).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from types import MappingProxyType
from typing import Mapping, Sequence

from fno_ai_paper_trading.research.options.models import (
    Greeks,
    OptionChainSnapshot,
    OptionQuote,
    OptionSide,
)

_FIELD_ORDER = (
    "timestamp",
    "freshness",
    "identity",
    "side_mapping",
    "expiry_consistency",
    "bid",
    "ask",
    "bid_ask",
    "last_price",
    "price_presence",
    "open_interest",
    "oi_change",
    "volume",
    "iv",
    "greeks_delta",
    "greeks_gamma",
    "greeks_theta",
    "greeks_vega",
    "greeks_rho",
)


class DataState(str):
    """Explicit data-quality tri-state (string enum-compatible)."""

    VALID = "VALID"
    INVALID = "INVALID"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True)
class FieldReport:
    """One deterministic verdict about one logical field."""

    field: str
    state: str  # DataState.VALID / INVALID / UNAVAILABLE
    reason: str = ""
    value: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "field": self.field,
            "state": self.state,
            "reason": self.reason,
            "value": self.value,
        }


@dataclass(frozen=True)
class QuoteValidation:
    """Validation verdict for one quote."""

    quote: OptionQuote
    fields: tuple[FieldReport, ...]

    @property
    def key(self) -> str:
        return self.quote.key

    @property
    def state(self) -> str:
        if any(f.state == DataState.INVALID for f in self.fields):
            return DataState.INVALID
        if any(f.state == DataState.UNAVAILABLE for f in self.fields):
            return DataState.UNAVAILABLE
        return DataState.VALID

    @property
    def problems(self) -> tuple[str, ...]:
        return tuple(f.reason for f in self.fields if f.state == DataState.INVALID)

    @property
    def unavailable(self) -> tuple[str, ...]:
        return tuple(f.field for f in self.fields if f.state == DataState.UNAVAILABLE)

    def to_dict(self) -> dict[str, object]:
        return {
            "key": self.key,
            "state": self.state,
            "fields": [f.to_dict() for f in self.fields],
        }


@dataclass(frozen=True)
class ChainValidation:
    """Snapshot-level verdict plus per-quote and global reports."""

    snapshot: OptionChainSnapshot
    quotes: tuple[QuoteValidation, ...]
    global_issues: tuple[FieldReport, ...]

    @property
    def state(self) -> str:
        if any(q.state == DataState.INVALID for q in self.quotes) or any(
            i.state == DataState.INVALID for i in self.global_issues
        ):
            return DataState.INVALID
        if any(q.state == DataState.UNAVAILABLE for q in self.quotes) or any(
            i.state == DataState.UNAVAILABLE for i in self.global_issues
        ):
            return DataState.UNAVAILABLE
        return DataState.VALID

    @property
    def invalid_quotes(self) -> tuple[QuoteValidation, ...]:
        return tuple(q for q in self.quotes if q.state == DataState.INVALID)

    @property
    def invalid_reasons(self) -> tuple[str, ...]:
        reasons: list[str] = []
        for quote in self.invalid_quotes:
            for problem in quote.problems:
                reasons.append(f"{quote.key}: {problem}")
        reasons.extend(i.reason for i in self.global_issues if i.state == DataState.INVALID)
        return tuple(reasons)

    def summary(self) -> dict[str, object]:
        return {
            "state": self.state,
            "quotes": len(self.quotes),
            "invalid_reasons": list(self.invalid_reasons),
            "unavailable_fields": sorted(
                {field for quote in self.quotes for field in quote.unavailable}
                | {i.field for i in self.global_issues if i.state == DataState.UNAVAILABLE}
            ),
        }


def _verdict(field: str, state: str, reason: str = "", value: str | None = None) -> FieldReport:
    return FieldReport(field=field, state=state, reason=reason, value=value)


def _available(value, name: str, *, invalid_when=None, reason_label: str = "") -> FieldReport:
    """Validate one present scalar: VALID unless a sanity rule is violated."""
    if value is None:
        return _verdict(name, DataState.UNAVAILABLE, f"{reason_label}not supplied by provider")
    if invalid_when is not None and invalid_when(value):
        return _verdict(name, DataState.INVALID, f"{reason_label}{name} failed sanity check")
    return _verdict(name, DataState.VALID, "", str(value))


def _greeks_verdicts(greeks: Greeks) -> list[FieldReport]:
    reports: list[FieldReport] = []
    for name, value in (
        ("greeks_delta", greeks.delta),
        ("greeks_gamma", greeks.gamma),
        ("greeks_theta", greeks.theta),
        ("greeks_vega", greeks.vega),
        ("greeks_rho", greeks.rho),
    ):
        if value is None:
            reports.append(_verdict(name, DataState.UNAVAILABLE, "not supplied by provider"))
            continue
        if name in ("greeks_gamma", "greeks_vega") and value < 0:
            reports.append(_verdict(name, DataState.INVALID, f"{name} must be >= 0", str(value)))
            continue
        reports.append(_verdict(name, DataState.VALID, "", str(value)))
    return reports


def validate_quote(
    quote: OptionQuote,
    *,
    snapshot_expiry,
    ref_time: datetime,
    max_age_seconds: int | None,
) -> QuoteValidation:
    """Produce the deterministic per-quote verdict for ``quote``."""
    reports: list[FieldReport] = []

    timestamp_ok = quote.timestamp.tzinfo is None
    if not timestamp_ok:
        reports.append(_verdict("timestamp", DataState.INVALID, "expected naive IST timestamp"))
    else:
        reports.append(_verdict("timestamp", DataState.VALID, "", quote.timestamp.isoformat()))
        if max_age_seconds is not None and ref_time is not None:
            if quote.timestamp > ref_time:
                reports.append(_verdict("freshness", DataState.INVALID, "timestamp is in the future"))
            else:
                age = int((ref_time - quote.timestamp).total_seconds())
                if age > max_age_seconds:
                    reports.append(
                        _verdict(
                            "freshness",
                            DataState.INVALID,
                            f"stale: age {age}s > max {max_age_seconds}s",
                            str(age),
                        )
                    )
                else:
                    reports.append(_verdict("freshness", DataState.VALID, "", str(age)))
        else:
            reports.append(_verdict("freshness", DataState.VALID, "age check disabled"))

    identity = quote.instrument.underlying_symbol
    reports.append(_verdict("identity", DataState.VALID, "", identity or quote.key))

    side = quote.side
    expected = side.to_instrument_type()
    if quote.instrument.instrument_type is expected:
        reports.append(_verdict("side_mapping", DataState.VALID, "", side.value))
    else:
        reports.append(
            _verdict(
                "side_mapping",
                DataState.INVALID,
                f"option_type {side.value} does not match instrument_type "
                f"{quote.instrument.instrument_type.value}",
                side.value,
            )
        )

    if quote.expiry.isoformat() == snapshot_expiry.isoformat():
        reports.append(_verdict("expiry_consistency", DataState.VALID, "", quote.expiry.isoformat()))
    else:
        reports.append(
            _verdict(
                "expiry_consistency",
                DataState.INVALID,
                f"quote expiry {quote.expiry.isoformat()} differs from snapshot expiry "
                f"{snapshot_expiry.isoformat()}",
                quote.expiry.isoformat(),
            )
        )

    reports.append(_available(quote.bid, "bid", invalid_when=lambda v: v < 0))
    reports.append(_available(quote.ask, "ask", invalid_when=lambda v: v < 0))

    if quote.bid is not None and quote.ask is not None:
        if quote.bid > quote.ask:
            reports.append(_verdict("bid_ask", DataState.INVALID, "crossed bid/ask (bid > ask)"))
        else:
            reports.append(_verdict("bid_ask", DataState.VALID, "", f"{quote.bid}|{quote.ask}"))
    elif quote.bid is None and quote.ask is None:
        reports.append(_verdict("bid_ask", DataState.UNAVAILABLE, "bid/ask not supplied by provider"))
    else:
        reports.append(_verdict("bid_ask", DataState.UNAVAILABLE, "half depth: only one of bid/ask supplied"))

    reports.append(_available(quote.last_price, "last_price", invalid_when=lambda v: v < 0))

    if not quote.has_price:
        reports.append(
            _verdict("price_presence", DataState.INVALID, "no price at all (bid/ask/last all missing)")
        )
    else:
        reports.append(_verdict("price_presence", DataState.VALID, ""))
        if quote.last_price is None:
            reports.append(_verdict("price_presence", DataState.UNAVAILABLE, "last_price not supplied by provider"))

    reports.append(_available(quote.open_interest, "open_interest", invalid_when=lambda v: v < 0))
    reports.append(_available(quote.oi_change, "oi_change"))
    reports.append(_available(quote.volume, "volume", invalid_when=lambda v: v < 0))
    reports.append(_available(quote.iv, "iv", invalid_when=lambda v: v < 0))
    reports.extend(_greeks_verdicts(quote.greeks))

    reports.sort(key=lambda report: _FIELD_ORDER.index(report.field) if report.field in _FIELD_ORDER else 999)
    return QuoteValidation(quote=quote, fields=tuple(reports))


class ChainValidator:
    """Deterministic tri-state quality engine over an option-chain snapshot.

    Args:
        max_age_seconds: freshness ceiling for quote timestamps (naive IST age
            vs the supplied reference time). ``None`` disables the age check.
        require_side_pair: when ``True``, a strike with only one side present is
            a global ``UNAVAILABLE`` report (contract discovery aid; not a
            validity failure by default).
    """

    def __init__(self, *, max_age_seconds: int | None = None, require_side_pair: bool = False) -> None:
        self.max_age_seconds = max_age_seconds
        self.require_side_pair = require_side_pair

    def validate(self, snapshot: OptionChainSnapshot, *, ref_time: datetime | None = None) -> ChainValidation:
        """Classify ``snapshot``; ``ref_time`` must be naive IST when supplied."""
        if ref_time is not None and ref_time.tzinfo is not None:
            raise ValueError("ref_time must be naive IST")
        if snapshot.timestamp.tzinfo is not None:
            raise ValueError("snapshot timestamp must be naive IST")
        ref = ref_time if ref_time is not None else datetime.now()

        quote_validations = tuple(
            validate_quote(
                quote,
                snapshot_expiry=snapshot.expiry,
                ref_time=ref,
                max_age_seconds=self.max_age_seconds,
            )
            for quote in snapshot.quotes
        )

        global_issues: list[FieldReport] = []

        for key in snapshot.duplicate_keys():
            count = sum(1 for q in snapshot.quotes if q.key == key)
            global_issues.append(
                _verdict(
                    "duplicate",
                    DataState.INVALID,
                    f"duplicate contract '{key}' ({count} quotes)",
                )
            )

        if snapshot.spot_price is None:
            global_issues.append(
                _verdict("spot_unavailable", DataState.UNAVAILABLE, "underlying spot NOT supplied; never substituted")
            )
        else:
            if snapshot.spot_price < 0:
                global_issues.append(
                    _verdict("spot_unavailable", DataState.INVALID, "underlying spot is negative", str(snapshot.spot_price))
                )
            else:
                global_issues.append(_verdict("spot_unavailable", DataState.VALID, "", str(snapshot.spot_price)))

        if self.require_side_pair:
            by_strike: dict[Decimal, list[str]] = {}
            for quote in snapshot.quotes:
                by_strike.setdefault(quote.strike, []).append(quote.side.value)
            for strike in sorted(by_strike):
                sides = set(by_strike[strike])
                if len(sides) == 1:
                    global_issues.append(
                        _verdict(
                            "side_pair",
                            DataState.UNAVAILABLE,
                            f"strike {strike:g} has only {sorted(sides)[0]} side supplied",
                            f"{strike:g}",
                        )
                    )
                else:
                    global_issues.append(
                        _verdict("side_pair", DataState.VALID, "", f"{strike:g}|CE+PE")
                    )

        return ChainValidation(
            snapshot=snapshot,
            quotes=quote_validations,
            global_issues=tuple(global_issues),
        )


__all__ = [
    "ChainValidation",
    "ChainValidator",
    "DataState",
    "FieldReport",
    "QuoteValidation",
    "validate_quote",
]