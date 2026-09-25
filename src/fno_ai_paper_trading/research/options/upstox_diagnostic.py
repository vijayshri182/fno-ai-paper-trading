"""Safe, deterministic diagnostics for the Upstox option-chain adapter.

Answers the observability question with an artifact, not raw payload logging:

* did we fetch a chain, how many contracts, how many strikes;
* which fields were available across the snapshot;
* how many quotes passed the Phase 5 tri-state validation.

Never includes credentials: inputs are a normalized :class:`OptionChainSnapshot`
and its :class:`ChainValidation`, which contain no tokens or headers by
construction. The renderer is a pure function over those values.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable, Mapping, Sequence

from fno_ai_paper_trading.research.options.models import OptionChainSnapshot, OptionSide
from fno_ai_paper_trading.research.options.validation import ChainValidation, DataState

_FIELD_NAMES = (
    "bid",
    "ask",
    "last_price",
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


def field_availability(snapshot: OptionChainSnapshot) -> dict[str, dict[str, int]]:
    """Present/absent counts for every optional quote field (ordered)."""
    total = len(snapshot.quotes)
    matrix: dict[str, dict[str, int]] = {}
    for name in _FIELD_NAMES:
        matrix[name] = {"present": 0, "absent": 0}
    for quote in snapshot.quotes:
        values = {
            "bid": quote.bid,
            "ask": quote.ask,
            "last_price": quote.last_price,
            "open_interest": quote.open_interest,
            "oi_change": quote.oi_change,
            "volume": quote.volume,
            "iv": quote.iv,
            "greeks_delta": quote.greeks.delta,
            "greeks_gamma": quote.greeks.gamma,
            "greeks_theta": quote.greeks.theta,
            "greeks_vega": quote.greeks.vega,
            "greeks_rho": quote.greeks.rho,
        }
        for name, value in values.items():
            state = matrix[name]
            state["present"] += 1 if value is not None else 0
            state["absent"] += 1 if value is None else 0
    matrix["_total"] = {"quotes": total}
    return matrix


def quote_state_counts(validation: ChainValidation) -> dict[str, int]:
    return {
        DataState.VALID: sum(1 for q in validation.quotes if q.state == DataState.VALID),
        DataState.UNAVAILABLE: sum(1 for q in validation.quotes if q.state == DataState.UNAVAILABLE),
        DataState.INVALID: sum(1 for q in validation.quotes if q.state == DataState.INVALID),
    }


def build_chain_diagnostic(
    snapshot: OptionChainSnapshot,
    validation: ChainValidation,
    *,
    fetched_at: datetime,
    live: bool,
    endpoints_used: Sequence[str],
    orders_called: bool,
    historical_options_data: str = "NOT_AVAILABLE",
    notes: Iterable[str] = (),
) -> dict[str, Any]:
    """Deterministic diagnostic payload (credential-free by construction)."""
    ce = len(snapshot.call_quotes())
    pe = len(snapshot.put_quotes())
    return {
        "status": "LIVE_VERIFIED" if live else "OFFLINE_FIXTURE",
        "provider": snapshot.source,
        "endpoints_used": sorted(endpoints_used),
        "underlying": {
            "symbol": snapshot.underlying.symbol,
            "underlying_symbol": snapshot.underlying.underlying_symbol,
            "instrument_key": snapshot.underlying.exchange_token,
            "instrument_type": snapshot.underlying.instrument_type.value,
        },
        "expiry": snapshot.expiry.isoformat(),
        "fetched_at": fetched_at.isoformat(),
        "snapshot_timestamp": snapshot.timestamp.isoformat(),
        "contracts_received": len(snapshot.quotes),
        "strikes": len(snapshot.strike_values()),
        "quotes": {"ce": ce, "pe": pe},
        "field_availability": field_availability(snapshot),
        "spot": {
            "source": "provider (underlying_spot_price)" if snapshot.spot_price is not None else "NOT_SUPPLIED",
            "value": str(snapshot.spot_price) if snapshot.spot_price is not None else None,
            "substituted": False,
        },
        "derived_fields": {
            "oi_change": "oi - prev_oi (documented derivation; not fabricated)",
        },
        "validation": {
            "state": validation.state,
            "quote_states": quote_state_counts(validation),
            "invalid_reasons": list(validation.invalid_reasons),
            "unavailable_fields": sorted(
                {field for quote in validation.quotes for field in quote.unavailable}
            ),
        },
        "fingerprint": None,  # filled by the caller when it computes it
        "historical_options_data": historical_options_data,
        "orders_called": bool(orders_called),
        "credentials_in_output": False,
        "notes": list(notes),
    }


def render_markdown(diagnostic: Mapping[str, Any]) -> str:
    """Render a diagnostic payload as a concise markdown report."""
    availability = diagnostic["field_availability"]
    lines: list[str] = []
    lines.append("# Upstox option-chain diagnostic (read-only)")
    lines.append("")
    lines.append(f"- Status: `{diagnostic['status']}`")
    lines.append(
        f"- Underlying: {diagnostic['underlying']['symbol']} "
        f"(`{diagnostic['underlying']['instrument_key']}`)"
    )
    lines.append(f"- Expiry: `{diagnostic['expiry']}` · fetched_at: `{diagnostic['fetched_at']}` IST")
    lines.append(f"- Provider: `{diagnostic['provider']}` · endpoints: {diagnostic['endpoints_used']}")
    lines.append(
        f"- Contracts received: **{diagnostic['contracts_received']}** "
        f"(CE {diagnostic['quotes']['ce']} / PE {diagnostic['quotes']['pe']}) · strikes: {diagnostic['strikes']}"
    )
    lines.append("- Field availability (present / total):")
    for name, counts in availability.items():
        if name.startswith("_"):
            continue
        lines.append(f"  - `{name}`: {counts['present']} / {availability['_total']['quotes']}")
    spot = diagnostic["spot"]
    lines.append(f"- Spot: source=`{spot['source']}` value=`{spot['value']}` substituted=`{spot['substituted']}`")
    validation = diagnostic["validation"]
    qstates = validation["quote_states"]
    lines.append(
        f"- Validation: `{validation['state']}` "
        f"(VALID {qstates['VALID']} / UNAVAILABLE {qstates['UNAVAILABLE']} / INVALID {qstates['INVALID']})"
    )
    if validation["invalid_reasons"]:
        for reason in validation["invalid_reasons"]:
            lines.append(f"  - INVALID: {reason}")
    if validation["unavailable_fields"]:
        lines.append(f"- Unavailable fields: {validation['unavailable_fields']}")
    lines.append(f"- Historical options data: `{diagnostic['historical_options_data']}`")
    lines.append(f"- Orders called: `{diagnostic['orders_called']}`")
    lines.append(f"- Credentials in output: `{diagnostic['credentials_in_output']}`")
    if diagnostic.get("notes"):
        for note in diagnostic["notes"]:
            lines.append(f"- Note: {note}")
    return "\n".join(lines) + "\n"


__all__ = [
    "build_chain_diagnostic",
    "field_availability",
    "quote_state_counts",
    "render_markdown",
]