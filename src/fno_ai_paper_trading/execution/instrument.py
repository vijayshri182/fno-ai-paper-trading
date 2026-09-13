"""F&O instrument resolution and client-side margin sanity check (WS 7.9).

The live experiment trades exactly one option contract on tomorrow's expiry.
Before any order the manager must have a fully-validated option instrument and
an explicit lot size, and must verify the required margin is within a
configured cap. All computations are conservative client-side estimates — the
exchange remains the final authority; a failed/premature check aborts the run.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from fno_ai_paper_trading.execution.errors import InstrumentValidationError
from fno_ai_paper_trading.models.enums import InstrumentType
from fno_ai_paper_trading.models.instruments import Instrument
from fno_ai_paper_trading.utils.functions import positive_decimal, positive_int

#: Conservative margin buffer to apply on top of the raw option premium cost.
MARGIN_BUFFER = Decimal("1.15")


def resolve_fno_instrument(
    *,
    underlying: str,
    expiry: date,
    strike: Decimal,
    option_type: str,
    exchange_token: str,
    lot_size: int,
    multiplier: int = 1,
    tick_size: Decimal = Decimal("0.05"),
    today: date | None = None,
) -> Instrument:
    """Build and validate the single option contract to trade.

    Raises :class:`InstrumentValidationError` for any missing or structurally
    invalid detail — an invalid instrument means *no order* is placed.
    """
    underlying = (underlying or "").strip()
    option_type = (option_type or "").strip().upper()
    exchange_token = (exchange_token or "").strip()

    if not underlying:
        raise InstrumentValidationError("an underlying symbol is required")
    if not exchange_token:
        raise InstrumentValidationError(
            "an Upstox instrument key (SEGMENT|SYMBOL) is required for execution"
        )
    if "|" not in exchange_token:
        raise InstrumentValidationError(
            f"expected an Upstox instrument key SEGMENT|SYMBOL, got {exchange_token!r}"
        )
    if expiry is None or expiry <= (today or datetime.now().date()):
        raise InstrumentValidationError(
            f"expiry must be strictly after today; got {expiry}"
        )
    try:
        strike_value = positive_decimal(strike, "strike")
        lot = positive_int(int(lot_size), "lot_size")
        mult = positive_int(int(multiplier), "multiplier")
    except ValueError as exc:
        raise InstrumentValidationError(str(exc)) from exc
    if option_type not in ("CE", "PE"):
        raise InstrumentValidationError("option_type must be 'CE' or 'PE'")

    instrument_type = (
        InstrumentType.OPTION_CE if option_type == "CE" else InstrumentType.OPTION_PE
    )
    return Instrument(
        symbol=exchange_token,
        instrument_type=instrument_type,
        underlying_symbol=underlying,
        expiry=expiry,
        strike=strike_value,
        option_type=option_type,
        exchange="NSE",
        exchange_token=exchange_token,
        lot_size=lot,
        tick_size=tick_size,
        multiplier=mult,
    )


def estimate_required_margin(instrument: Instrument, premium: Decimal) -> Decimal:
    """Conservative client-side margin estimate for one option lot.

    ``max(premium * lot_size * multiplier * buffer, lot_size * tick_size)`` keeps
    the estimate bounded and raw: option premium cost (subject to exchange
    margins) with a buffer, never a fabricated precise number. A zero/None quote
    raises — the manager must not assume affordability without a quote.
    """
    if instrument is None or not instrument.is_option():
        raise InstrumentValidationError("margin requires a validated option instrument")
    price = positive_decimal(premium, "premium")
    lot = positive_int(instrument.lot_size, "lot_size")
    mult = positive_int(instrument.multiplier, "multiplier")
    tick = positive_decimal(instrument.tick_size, "tick_size")
    premium_cost = price * lot * mult
    floor = Decimal(lot) * tick
    return max(premium_cost * MARGIN_BUFFER, floor)