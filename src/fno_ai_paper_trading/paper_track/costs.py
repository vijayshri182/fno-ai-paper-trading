"""Transparent statutory cost schedule and gross-to-net attribution.

The paper broker simulates execution with two knobs: per-fill slippage
(``slippage_rate``) and an aggregate per-fill charge (``commission``). The
reconciliation identity enforced by :mod:`fno_ai_paper_trading.paper_track.accounting`
is frozen on that single commission bucket::

    cash = initial_cash + gross_realized_pnl - total_commission

This module does NOT change how commissions are charged or cash is reconciled.
It only makes the *composition* of the cost bucket legible for reporting:

* exactly how much of the day's realised loss against the pre-slippage
  reference price is the modelled slippage (recomputable from ``slippage_rate``),
  and
* a PROVISIONAL, clearly-labelled statutory breakdown of the aggregate charge
  (SEBI turnover fees, exchange transaction charges, stamp duty, STT) using
  ASSUMED rates for listed equity derivatives. The residual after the statutory
  components is reported as the brokerage-model balance. Nothing here is used
  to alter execution; the figures are informational and ignored for money math.

Every function is a pure, deterministic computation over the fill ledger, so
the schedule can be re-derived from any persisted report.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from fno_ai_paper_trading.models.enums import OrderSide
from fno_ai_paper_trading.models.order import Fill

__all__ = [
    "STATUTORY_SCHEDULE",
    "StatutoryRates",
    "reference_price",
    "slippage_amount",
    "turnover_by_side",
    "estimate_statutory_costs",
    "cost_attribution",
]


@dataclass(frozen=True)
class StatutoryRates:
    """PROVISIONAL assumed statutory rates for listed equity derivatives.

    All values are fractions of notional turnover. They are DOCUMENTED
    ASSUMPTIONS used only to decompose the day's aggregate commission for
    reporting; they are not a substitute for a broker-invoice and must never
    be used to compute real cash.
    """

    sebi_turnover_fee: Decimal = Decimal("0.000001")  # SEBI ₹10/crore (0.0001% of turnover)
    exchange_tc: Decimal = Decimal("0.000019")  # NSE transaction charge, futures ₹1.90/lakh
    stt_futures_sell: Decimal = Decimal("0.000125")  # STT 0.0125% on sell-side turnover
    stamp_duty_futures_buy: Decimal = Decimal("0.00002")  # stamp duty 0.002% on buy turnover
    gst_on_brokerage: Decimal = Decimal("0.18")  # GST 18% on the brokerage-model balance


STATUTORY_SCHEDULE: StatutoryRates = StatutoryRates()


def reference_price(fill_price: Decimal, side: OrderSide, slippage_rate: Decimal) -> Decimal:
    """The pre-slippage price implied by the fill price.

    ``PaperBroker._apply_slippage`` computes ``fill = ref * (1 + rate)`` for a
    buy and ``fill = ref * (1 - rate)`` for a sell, so the reference is the
    inverse of that transform. Buy/sell are decided by the side of the fill.
    """
    factor = Decimal("1") + slippage_rate if side is OrderSide.BUY else Decimal("1") - slippage_rate
    return fill_price / factor


def slippage_amount(
    fills: list[Fill] | tuple[Fill, ...],
    slippage_rate: Decimal,
) -> Decimal:
    """Total cash value of modelled slippage across ``fills``.

    For each fill the slippage is the gap between the reference (pre-slippage)
    notional and the executed notional, so a buy pays up and a sell receives
    less by exactly ``qty * multiplier * |fill_price - reference_price|``.
    """
    total = Decimal("0")
    for fill in fills:
        ref = reference_price(fill.price, fill.side, slippage_rate)
        gap = abs(fill.price - ref)
        total += Decimal(fill.quantity) * fill.instrument.multiplier * gap
    return total


def turnover_by_side(
    fills: list[Fill] | tuple[Fill, ...],
) -> tuple[Decimal, Decimal]:
    """(buy_turnover, sell_turnover) in money, summed over the fill ledger."""
    buy = Decimal("0")
    sell = Decimal("0")
    for fill in fills:
        notional = Decimal(fill.quantity) * fill.instrument.multiplier * fill.price
        if fill.side is OrderSide.BUY:
            buy += notional
        else:
            sell += notional
    return buy, sell


def estimate_statutory_costs(
    buy_turnover: Decimal,
    sell_turnover: Decimal,
    rates: StatutoryRates = STATUTORY_SCHEDULE,
) -> dict[str, Decimal]:
    """PROVISIONAL statutory breakdown of a day's statutory-levied turnover."""
    total_turnover = buy_turnover + sell_turnover
    sebi = total_turnover * rates.sebi_turnover_fee
    exchange = total_turnover * rates.exchange_tc
    stt = sell_turnover * rates.stt_futures_sell
    stamp = buy_turnover * rates.stamp_duty_futures_buy
    two = Decimal("0.01")
    sebi = sebi.quantize(two)
    exchange = exchange.quantize(two)
    stt = stt.quantize(two)
    stamp = stamp.quantize(two)
    return {
        "sebi_turnover_fee": sebi,
        "exchange_transaction_charges": exchange,
        "stt_sell_side": stt,
        "stamp_duty_buy_side": stamp,
        "total": (sebi + exchange + stt + stamp).quantize(two),
    }


def cost_attribution(
    fills: list[Fill] | tuple[Fill, ...],
    *,
    commission_total: Decimal,
    slippage_rate: Decimal,
    rates: StatutoryRates = STATUTORY_SCHEDULE,
) -> dict:
    """Gross-to-net attribution for the cost bucket of one day (reporting only).

    Returns a plain dict intended directly for the daily report. The
    ``charges`` section decomposes the day's aggregate commission; if the
    PROVISIONAL statutory estimate exceeds the aggregate charge, the balance is
    clamped at zero and ``under_modeled`` is raised so a reader can see the
    modelled commission is too thin to cover the assumed statutory levies.
    """
    buy_turnover, sell_turnover = turnover_by_side(fills)
    statutory = estimate_statutory_costs(buy_turnover, sell_turnover, rates=rates)
    statutory_total = statutory["total"]
    balance = commission_total - statutory_total
    under_modeled = statutory_total > commission_total
    holding_balance = Decimal("0.00") if under_modeled else balance
    gst_on_brokerage = (holding_balance * rates.gst_on_brokerage).quantize(Decimal("0.01"))
    slippage = slippage_amount(fills, slippage_rate).quantize(Decimal("0.01"))

    return {
        "provisional": True,
        "note": (
            "PROVISIONAL, ASSUMED statutory rates for listed equity derivatives; "
            "informational only and never used for real cash. Broker-invoice and "
            "NSE SEBI schedules are authoritative in production."
        ),
        "slippage": {
            "model_rate": str(slippage_rate),
            "amount": str(slippage),
        },
        "charges": {
            "modeled_total_commission": str(commission_total),
            "statutory_estimated": {k: str(v) for k, v in statutory.items()},
            "brokerage_model_balance": str(holding_balance),
            "gst_estimated_on_brokerage_balance": str(gst_on_brokerage),
            "under_modeled": under_modeled,
        },
    }