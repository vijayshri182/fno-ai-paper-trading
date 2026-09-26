"""Deterministic, versioned fill references for the options paper simulation.

A fill is only ever derived from *observed* market data (bid/ask/last) on a
quote explicitly supplied by the caller. Missing or invalid references are
unavailable (never fabricated); crossed markets (``bid > ask``) are unavailable.
Slippage and commission follow the existing ``PaperBroker`` conventions, and
rupee figures use the Phase 10 exposure basis ``quantity x lot_size x
multiplier``.

Premium exposure is **not** stop-loss risk; this module never invents options
stop-loss semantics.
"""
from __future__ import annotations

from decimal import Decimal
from enum import Enum

from fno_ai_paper_trading.models.enums import OrderSide
from fno_ai_paper_trading.research.options.models import OptionQuote

__all__ = [
    "FILL_MODEL_VERSION",
    "MarkBasis",
    "fill_reference",
    "mark_reference",
    "premium_exposure",
    "crossed_quote",
]

#: Versioned fill model: bump only for a deliberate, documented behaviour change.
FILL_MODEL_VERSION = "options-paper-fill-1"


class MarkBasis(str, Enum):
    """Which observed quote field a deterministic reference came from."""

    BID = "bid"
    ASK = "ask"
    LAST = "last_price"
    MID = "mid"


def crossed_quote(quote: OptionQuote) -> bool:
    """True when both sides are present and the market is crossed (no fill)."""
    return quote.bid is not None and quote.ask is not None and quote.bid > quote.ask


def fill_reference(
    quote: OptionQuote, side: OrderSide
) -> tuple[str | None, Decimal | None, str | None]:
    """Deterministic fill reference from observed data (never fabricated).

    * BUY (entry): ``ask``, else ``last_price``.
    * SELL (exit): ``bid``, else ``last_price``.
    * Crossed markets, missing references -> ``(None, None, reason)``.

    Returns ``(reference_used, reference_price, unavailable_reason)``; exactly
    one of ``reference_used``/``unavailable_reason`` is set.
    """
    if crossed_quote(quote):
        return None, None, "crossed market (bid > ask); no fill"
    if side is OrderSide.BUY:
        if quote.ask is not None:
            return MarkBasis.ASK.value, quote.ask, None
        if quote.last_price is not None:
            return MarkBasis.LAST.value, quote.last_price, None
        return None, None, "no entry fill reference (no ask, no last_price)"
    if quote.bid is not None:
        return MarkBasis.BID.value, quote.bid, None
    if quote.last_price is not None:
        return MarkBasis.LAST.value, quote.last_price, None
    return None, None, "no exit fill reference (no bid, no last_price)"


def mark_reference(
    quote: OptionQuote,
) -> tuple[str | None, Decimal | None, str | None]:
    """Deterministic mark reference for unrealized P&L (reliable data only).

    Preference: ``last_price``, else ``mid`` of a valid bid/ask, else a single
    available side. Crossed markets and fully-missing quotes are unreliable.
    """
    if crossed_quote(quote):
        return None, None, "unreliable mark (crossed market)"
    if quote.last_price is not None:
        return MarkBasis.LAST.value, quote.last_price, None
    if quote.bid is not None and quote.ask is not None:
        return MarkBasis.MID.value, (quote.bid + quote.ask) / 2, None
    if quote.bid is not None:
        return MarkBasis.BID.value, quote.bid, None
    if quote.ask is not None:
        return MarkBasis.ASK.value, quote.ask, None
    return None, None, "unreliable mark (no quoted price)"


def premium_exposure(premium: Decimal, quantity: int, lot_size: int, multiplier: int) -> Decimal:
    """Phase 10 exposure basis: ``premium x quantity x lot_size x multiplier``.

    Matches the risk engine's ``unit_exposure * requested`` formula. This is
    premium exposure — never a stop-loss and never equated with risk-of-loss.
    """
    return premium * quantity * lot_size * multiplier