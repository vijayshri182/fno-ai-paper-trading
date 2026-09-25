"""Deterministic, provider-neutral normalization for option-chain payloads.

Two responsibilities, kept strictly apart:

1. **Normalization (raw provider payload -> domain models).** Structurally
   malformed input raises :class:`OptionNormalizationError` (bad timestamp,
   unknown side, non-numeric identity/price). *Absent* optional fields become
   ``None`` = explicitly unavailable. Semantics (crossed bid/ask, negative OI,
   stale, duplicates) are deliberately **not** judged here — the quality layer
   classifies them into ``VALID / INVALID / UNAVAILABLE``.
2. **Canonical round-trip (models -> dict -> models).** Stable, sorted-key
   serialization for later replay datasets, fingerprinting and byte-level
   determinism checks.

No-fabrication: spot is read only from the snapshot-level ``spot_price`` /
``underlying_price`` field and is never injected into option quotes; a quote
field the provider omitted stays ``None``.
"""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence

from fno_ai_paper_trading.data.market_hours import NSE_TZ
from fno_ai_paper_trading.models.enums import InstrumentType
from fno_ai_paper_trading.models.instruments import Instrument
from fno_ai_paper_trading.research.options.errors import OptionNormalizationError
from fno_ai_paper_trading.research.options.models import (
    Greeks,
    OptionChainSnapshot,
    OptionQuote,
    OptionSide,
    _decimal_or_none,
    _int_or_none,
)
from fno_ai_paper_trading.utils.functions import to_decimal

# Raw-payload field aliases accepted for the option underlying price.
_SPOT_KEYS = ("spot_price", "underlying_price")
_LAST_KEYS = ("last_price", "ltp")
_ROWS_KEYS = ("quotes", "rows")
_GREEK_KEYS = ("delta", "gamma", "theta", "vega", "rho")


def _stable_json_dumps(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def parse_timestamp(value: Any, *, name: str = "timestamp") -> datetime | None:
    """Deterministic timestamp parse: ISO-8601 or epoch seconds/millis -> naive IST.

    ``None`` for a missing value; raises :class:`OptionNormalizationError` for
    a present-but-unparseable value.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        epoch = float(value)
        if abs(epoch) > 1e12:  # milliseconds
            epoch = epoch / 1000.0
        try:
            return datetime.fromtimestamp(epoch, tz=NSE_TZ).replace(tzinfo=None)
        except (OverflowError, OSError, ValueError) as exc:
            raise OptionNormalizationError(f"unparseable {name}: {value!r}") from exc
    if not isinstance(value, str):
        raise OptionNormalizationError(f"unparseable {name}: {value!r}")
    text = value.strip()
    if text.isdigit() or (text[:1] == "-" and text[1:].isdigit()):
        return parse_timestamp(float(text), name=name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise OptionNormalizationError(f"unparseable {name}: {value!r}") from exc
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(NSE_TZ)
    return parsed.replace(tzinfo=None)


def parse_date(value: Any, *, name: str = "expiry") -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError as exc:
        raise OptionNormalizationError(f"unparseable {name}: {value!r}") from exc


def _require_decimal(raw: Mapping[str, Any], key: str) -> Decimal:
    value = raw.get(key)
    if value is None:
        raise OptionNormalizationError(f"missing required field {key!r}")
    try:
        return to_decimal(value)
    except (ValueError, TypeError, InvalidOperation) as exc:
        raise OptionNormalizationError(f"non-numeric required field {key!r}: {value!r}") from exc


def _optional_decimal(raw: Mapping[str, Any], key: str) -> Decimal | None:
    if key not in raw:
        return None
    try:
        return _decimal_or_none(raw[key], key)
    except ValueError as exc:
        raise OptionNormalizationError(f"non-numeric optional field {key!r}: {raw[key]!r}") from exc


def _optional_int(raw: Mapping[str, Any], key: str) -> int | None:
    if key not in raw:
        return None
    try:
        return _int_or_none(raw[key], key)
    except ValueError as exc:
        raise OptionNormalizationError(f"non-integer optional field {key!r}: {raw[key]!r}") from exc


def _parse_greeks(raw: Any) -> Greeks:
    if raw is None:
        return Greeks()
    if not isinstance(raw, Mapping):
        raise OptionNormalizationError("greeks must be a mapping when present")
    values: dict[str, Decimal | None] = {}
    for key in _GREEK_KEYS:
        if key not in raw:
            values[key] = None
            continue
        try:
            values[key] = _decimal_or_none(raw[key], key)
        except ValueError as exc:
            raise OptionNormalizationError(f"non-numeric greek {key!r}: {raw[key]!r}") from exc
    return Greeks(**values)


def _instrument_from_raw(raw: Mapping[str, Any], underlying: Instrument, side: OptionSide) -> Instrument:
    expiry = parse_date(raw.get("expiry"))
    if expiry is None:
        raise OptionNormalizationError(f"option row is missing an expiry: {raw.get('instrument_key') or ''!r}")
    strike = _require_decimal(raw, "strike")
    source_id = raw.get("instrument_key")
    symbol = raw.get("symbol") or source_id or f"{underlying.symbol} {expiry} {strike:g} {side.value}"
    try:
        return Instrument(
            symbol=str(symbol),
            instrument_type=side.to_instrument_type(),
            underlying_symbol=underlying.symbol,
            expiry=expiry,
            strike=strike,
            option_type=side.value,
            exchange=underlying.exchange,
            exchange_token=str(source_id) if source_id else None,
        )
    except ValueError as exc:
        raise OptionNormalizationError(f"malformed option contract: {exc}") from exc


def normalize_quote(raw: Mapping[str, Any], *, underlying: Instrument, source: str = "UNKNOWN") -> OptionQuote:
    """Normalize one provider row into a validated :class:`OptionQuote`.

    Required identity: ``expiry``, ``strike``, ``option_type``, ``timestamp``.
    Everything else may be absent (explicitly unavailable).
    """
    if not isinstance(raw, Mapping):
        raise OptionNormalizationError(f"quote row must be a mapping, got {type(raw).__name__}")
    side = OptionSide.parse(raw.get("option_type"))
    if side is None:
        raise OptionNormalizationError(f"missing/unknown option_type: {raw.get('option_type')!r}")
    instrument = _instrument_from_raw(raw, underlying, side)

    timestamp = parse_timestamp(raw.get("timestamp"))
    if timestamp is None:
        raise OptionNormalizationError("quote row is missing a timestamp")

    last_price = None
    for key in _LAST_KEYS:
        if key in raw:
            last_price = _optional_decimal(raw, key)
            break

    return OptionQuote(
        instrument=instrument,
        timestamp=timestamp,
        bid=_optional_decimal(raw, "bid"),
        ask=_optional_decimal(raw, "ask"),
        last_price=last_price,
        open_interest=_optional_int(raw, "oi"),
        oi_change=_optional_int(raw, "oi_change"),
        volume=_optional_int(raw, "volume"),
        iv=_optional_decimal(raw, "iv"),
        greeks=_parse_greeks(raw.get("greeks")),
        source=(raw.get("source") or source or "UNKNOWN"),
        source_timestamp=parse_timestamp(raw.get("source_timestamp"), name="source_timestamp"),
        source_id=raw.get("instrument_key"),
    )


def normalize_chain(
    raw: Mapping[str, Any],
    *,
    underlying: Instrument,
    source: str = "UNKNOWN",
) -> OptionChainSnapshot:
    """Normalize a provider chain payload into a sorted :class:`OptionChainSnapshot`.

    Rows are sorted deterministically by ``(expiry, strike, side)``; duplicate
    contracts are **preserved** (never dropped) and flagged by the quality
    layer. The snapshot expiry is taken from the payload ``expiry`` when
    present, otherwise derived from the (uniform) quote rows.
    """
    if not isinstance(raw, Mapping):
        raise OptionNormalizationError(f"chain payload must be a mapping, got {type(raw).__name__}")
    if underlying.is_option():
        raise OptionNormalizationError("the chain underlying must be a non-option instrument")

    rows: Sequence[Any] = raw.get(_ROWS_KEYS[0]) or raw.get(_ROWS_KEYS[1]) or []
    if not isinstance(rows, (list, tuple)):
        raise OptionNormalizationError("chain quote rows must be a list")
    quotes = tuple(normalize_quote(row, underlying=underlying, source=source) for row in rows)
    quotes = tuple(sorted(quotes, key=lambda q: (q.expiry, q.strike, q.side.value)))

    declared = raw.get("expiry")
    if declared is not None:
        expiry = parse_date(declared)
        if expiry is None:
            raise OptionNormalizationError("chain payload has an invalid expiry")
    elif quotes:
        expiries = {q.expiry.isoformat() for q in quotes}
        if len(expiries) != 1:
            raise OptionNormalizationError(
                "cannot derive snapshot expiry from heterogeneous quote rows: " + ", ".join(sorted(expiries))
            )
        expiry = quotes[0].expiry
    else:
        raise OptionNormalizationError("chain payload declares neither an expiry nor any quote rows")

    spot = None
    for key in _SPOT_KEYS:
        if key in raw:
            try:
                spot = _decimal_or_none(raw[key], key)
            except ValueError as exc:
                raise OptionNormalizationError(f"non-numeric {key}: {raw[key]!r}") from exc
            break

    timestamp = parse_timestamp(raw.get("timestamp")) or (max(q.timestamp for q in quotes) if quotes else None)
    if timestamp is None:
        raise OptionNormalizationError("chain payload is missing a snapshot timestamp")

    return OptionChainSnapshot(
        underlying=underlying,
        expiry=expiry,
        timestamp=timestamp,
        quotes=quotes,
        spot_price=spot,
        source=(raw.get("source") or source or "UNKNOWN"),
        source_timestamp=parse_timestamp(raw.get("source_timestamp"), name="source_timestamp"),
    )


# --------------------------------------------------------------------------- #
# canonical round-trip (replay datasets / fingerprints)
# --------------------------------------------------------------------------- #


def _instrument_to_meta(instrument: Instrument) -> dict[str, Any]:
    return {
        "symbol": instrument.symbol,
        "instrument_type": instrument.instrument_type.value,
        "underlying_symbol": instrument.underlying_symbol,
        "expiry": instrument.expiry.isoformat() if instrument.expiry else None,
        "strike": str(instrument.strike) if instrument.strike is not None else None,
        "option_type": instrument.option_type,
        "exchange": instrument.exchange,
        "exchange_token": instrument.exchange_token,
        "lot_size": instrument.lot_size,
        "tick_size": str(instrument.tick_size),
        "multiplier": instrument.multiplier,
    }


def _instrument_from_meta(meta: Mapping[str, Any]) -> Instrument:
    expiry = date.fromisoformat(meta["expiry"]) if meta.get("expiry") else None
    strike = Decimal(meta["strike"]) if meta.get("strike") is not None else None
    return Instrument(
        symbol=meta["symbol"],
        instrument_type=InstrumentType(meta["instrument_type"]),
        underlying_symbol=meta["underlying_symbol"],
        expiry=expiry,
        strike=strike,
        option_type=meta.get("option_type"),
        exchange=meta.get("exchange", "NSE"),
        exchange_token=meta.get("exchange_token"),
        lot_size=int(meta.get("lot_size", 1)),
        tick_size=Decimal(meta.get("tick_size", "0.05")),
        multiplier=int(meta.get("multiplier", 1)),
    )


def quote_to_dict(quote: OptionQuote, *, exclude_none: bool = False) -> dict[str, Any]:
    """Stable dict form of a quote (``exclude_none`` drops unavailable fields)."""
    payload: dict[str, Any] = {
        "instrument": _instrument_to_meta(quote.instrument),
        "timestamp": quote.timestamp.isoformat(),
        "bid": str(quote.bid) if quote.bid is not None else None,
        "ask": str(quote.ask) if quote.ask is not None else None,
        "last_price": str(quote.last_price) if quote.last_price is not None else None,
        "open_interest": quote.open_interest,
        "oi_change": quote.oi_change,
        "volume": quote.volume,
        "iv": str(quote.iv) if quote.iv is not None else None,
        "greeks": {
            "delta": str(quote.greeks.delta) if quote.greeks.delta is not None else None,
            "gamma": str(quote.greeks.gamma) if quote.greeks.gamma is not None else None,
            "theta": str(quote.greeks.theta) if quote.greeks.theta is not None else None,
            "vega": str(quote.greeks.vega) if quote.greeks.vega is not None else None,
            "rho": str(quote.greeks.rho) if quote.greeks.rho is not None else None,
        },
        "source": quote.source,
        "source_timestamp": quote.source_timestamp.isoformat() if quote.source_timestamp is not None else None,
        "source_id": quote.source_id,
    }
    if exclude_none:
        payload = {k: v for k, v in payload.items() if v is not None}
        payload["greeks"] = {k: v for k, v in payload["greeks"].items() if v is not None}
    return dict(sorted(payload.items()))


def quote_from_dict(payload: Mapping[str, Any]) -> OptionQuote:
    val = dict(payload)
    greeks_raw = val.get("greeks") or {}
    greeks = Greeks(
        **{key: (Decimal(greeks_raw[key]) if greeks_raw.get(key) is not None else None) for key in _GREEK_KEYS}
    )
    return OptionQuote(
        instrument=_instrument_from_meta(val["instrument"]),
        timestamp=datetime.fromisoformat(val["timestamp"]),
        bid=Decimal(val["bid"]) if val.get("bid") is not None else None,
        ask=Decimal(val["ask"]) if val.get("ask") is not None else None,
        last_price=Decimal(val["last_price"]) if val.get("last_price") is not None else None,
        open_interest=val.get("open_interest"),
        oi_change=val.get("oi_change"),
        volume=val.get("volume"),
        iv=Decimal(val["iv"]) if val.get("iv") is not None else None,
        greeks=greeks,
        source=str(val.get("source") or "UNKNOWN"),
        source_timestamp=datetime.fromisoformat(val["source_timestamp"]) if val.get("source_timestamp") else None,
        source_id=val.get("source_id"),
    )


def snapshot_to_dict(snapshot: OptionChainSnapshot) -> dict[str, Any]:
    return {
        "underlying": _instrument_to_meta(snapshot.underlying),
        "expiry": snapshot.expiry.isoformat(),
        "timestamp": snapshot.timestamp.isoformat(),
        "spot_price": str(snapshot.spot_price) if snapshot.spot_price is not None else None,
        "source": snapshot.source,
        "source_timestamp": snapshot.source_timestamp.isoformat() if snapshot.source_timestamp is not None else None,
        "quotes": [quote_to_dict(q) for q in snapshot.quotes],
    }


def snapshot_from_dict(payload: Mapping[str, Any]) -> OptionChainSnapshot:
    val = dict(payload)
    return OptionChainSnapshot(
        underlying=_instrument_from_meta(val["underlying"]),
        expiry=date.fromisoformat(val["expiry"]),
        timestamp=datetime.fromisoformat(val["timestamp"]),
        quotes=tuple(quote_from_dict(q) for q in val["quotes"]),
        spot_price=Decimal(val["spot_price"]) if val.get("spot_price") is not None else None,
        source=str(val.get("source") or "UNKNOWN"),
        source_timestamp=datetime.fromisoformat(val["source_timestamp"]) if val.get("source_timestamp") else None,
    )


def chain_fingerprint(snapshot: OptionChainSnapshot) -> str:
    """Deterministic SHA-256 over the canonical chain dict (replays/fingerprint)."""
    canonical = snapshot_to_dict(snapshot)
    return hashlib.sha256(_stable_json_dumps(canonical).encode("utf-8")).hexdigest()


__all__ = [
    "chain_fingerprint",
    "normalize_chain",
    "normalize_quote",
    "parse_date",
    "parse_timestamp",
    "quote_from_dict",
    "quote_to_dict",
    "snapshot_from_dict",
    "snapshot_to_dict",
]