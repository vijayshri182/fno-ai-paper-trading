"""Curated research instruments with stable Upstox V3 instrument keys.

Real historical research needs ready-to-use instruments whose ``exchange_token``
is a valid Upstox key. Only *index* instruments have stable keys (e.g.
``NSE_INDEX|Nifty 50``), so this registry is deliberately limited to the broad
and sectoral indices a researcher typically studies first. Expiring futures and
options change every contract cycle and must come from the Upstox master file —
that download is intentionally not automated here (see
:class:`~fno_ai_paper_trading.data.upstox_provider.UpstoxHistoricalDataProvider`).

Helpers:

* :func:`get_research_instruments` / :func:`get_research_instrument` — indexed lookup.
* :func:`instrument_from_upstox_key` — build an ``Instrument`` from a raw
  ``SEGMENT|SYMBOL`` key (used by the acquisition CLI and the smoke test).
"""
from __future__ import annotations

from decimal import Decimal

from fno_ai_paper_trading.data.upstox_provider import upstox_instrument_key
from fno_ai_paper_trading.models.enums import InstrumentType
from fno_ai_paper_trading.models.instruments import Instrument


def _index(symbol: str) -> Instrument:
    return Instrument(
        symbol=symbol,
        instrument_type=InstrumentType.INDEX,
        underlying_symbol=symbol,
        exchange="NSE",
        exchange_token=upstox_instrument_key("NSE_INDEX", symbol),
        lot_size=1,
        tick_size=Decimal("0.05"),
        multiplier=1,
    )


# Registry keyed by the short names users pass to the acquisition CLI. The
# ``symbol`` is the exact Upstox display symbol carried in the instrument key.
RESEARCH_INSTRUMENTS: dict[str, Instrument] = {
    "NIFTY 50": _index("Nifty 50"),
    "BANKNIFTY": _index("Nifty Bank"),
    "FINNIFTY": _index("Nifty Financial Services"),
}


def get_research_instruments() -> list[Instrument]:
    """Return all curated research instruments (deterministic order)."""
    return list(RESEARCH_INSTRUMENTS.values())


def get_research_instrument(name: str) -> Instrument:
    """Return the curated instrument registered under ``name`` (case-insensitive key).

    Raises :class:`KeyError` when ``name`` is not a registered research
    instrument.
    """
    match = next(
        (i for key, i in RESEARCH_INSTRUMENTS.items() if key.lower() == (name or "").strip().lower()),
        None,
    )
    if match is None:
        raise KeyError(
            f"unknown research instrument {name!r}; registered: "
            f"{sorted(RESEARCH_INSTRUMENTS)}"
        )
    return match


def instrument_from_upstox_key(key: str) -> Instrument:
    """Build an ``Instrument`` from a raw Upstox ``SEGMENT|SYMBOL`` key.

    Index segments (``NSE_INDEX``) map to :class:`InstrumentType.INDEX`; all
    other segments map to :class:`InstrumentType.FUTURE` as a nominal type —
    the upstream payload is still normalized OHLCV either way.

    Raises :class:`ValueError` for a missing or malformed key (same rules as
    :func:`upstox_instrument_key`).
    """
    try:
        segment, symbol = key.split("|", 1)
    except ValueError as exc:
        raise ValueError(f"instrument key must be SEGMENT|SYMBOL, got {key!r}") from exc
    return _instrument_for_segment(segment, symbol.strip())


def _instrument_for_segment(segment: str, symbol: str) -> Instrument:
    normalized = upstox_instrument_key(segment, symbol)
    parts = normalized.split("|", 1)
    instrument_type = (
        InstrumentType.INDEX if parts[0] == "NSE_INDEX" else InstrumentType.FUTURE
    )
    return Instrument(
        symbol=symbol,
        instrument_type=instrument_type,
        underlying_symbol=symbol,
        exchange=parts[0].split("_")[0],
        exchange_token=normalized,
        lot_size=1,
        tick_size=Decimal("0.05"),
        multiplier=1,
    )