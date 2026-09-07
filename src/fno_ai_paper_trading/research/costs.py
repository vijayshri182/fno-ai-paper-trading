"""Configurable Indian trading-cost model for research.

Splits charges into the separate components that apply on Indian exchanges:

* brokerage (flat and/or fraction of notional)
* securities transaction tax (STT) — fraction of notional, buy vs sell rate
* exchange/transaction charges — fraction of notional
* SEBI charges — fraction of notional
* stamp duty — fraction of notional, buy side only
* GST — rate applied to the taxable base (brokerage + exchange + SEBI)
* other configurable charges (flat and/or fraction)

IMPORTANT — documented assumptions
----------------------------------
The defaults in :meth:`IndiaCostSchedule.nse_fo_illustrative` are *illustrative
example values* used to make research deterministic and comparable. They are
NOT a statement of any broker's or regulator's current fees. Indian fees
change frequently and differ by broker, segment, and product. Before drawing
conclusions from a backtest, replace this schedule with the actual schedule
that applies to your broker and instrument.

Charges are computed without per-fill rounding (exact ``Decimal`` arithmetic)
so results are deterministic; settlement rounding is left to the accounting
layer.

Compute contract (duck-typed)
-----------------------------
The backtest engine accepts any object exposing::

    compute(side: OrderSide, notional: Decimal, quantity: int) -> ChargeBreakdown

and reads ``.total`` from the returned object.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from fno_ai_paper_trading.models.enums import OrderSide
from fno_ai_paper_trading.utils.functions import non_negative_decimal, positive_decimal, positive_int


@dataclass(frozen=True)
class ChargeBreakdown:
    """Detailed charges computed for a single fill, plus the total."""

    side: OrderSide
    notional: Decimal
    quantity: int
    brokerage: Decimal
    stt: Decimal
    exchange_charges: Decimal
    sebi_charges: Decimal
    stamp_duty: Decimal
    gst: Decimal
    other_charges: Decimal

    @property
    def total(self) -> Decimal:
        return (
            self.brokerage
            + self.stt
            + self.exchange_charges
            + self.sebi_charges
            + self.stamp_duty
            + self.gst
            + self.other_charges
        )

    def as_dict(self) -> dict[str, str]:
        """Structured, stringified view for logging/reports."""
        return {
            "side": self.side.value,
            "notional": str(self.notional),
            "quantity": str(self.quantity),
            "brokerage": str(self.brokerage),
            "stt": str(self.stt),
            "exchange_charges": str(self.exchange_charges),
            "sebi_charges": str(self.sebi_charges),
            "stamp_duty": str(self.stamp_duty),
            "gst": str(self.gst),
            "other_charges": str(self.other_charges),
            "total": str(self.total),
        }


@dataclass(frozen=True)
class IndiaCostSchedule:
    """A fee schedule split into Indian cost components (all fractions of notional)."""

    brokerage_fraction: Decimal = Decimal("0")  # fraction of notional
    brokerage_per_order: Decimal = Decimal("0")  # flat charge per fill
    stt_buy_fraction: Decimal = Decimal("0")  # STT fraction of notional on BUY
    stt_sell_fraction: Decimal = Decimal("0")  # STT fraction of notional on SELL
    exchange_txn_fraction: Decimal = Decimal("0")  # exchange/transaction charges
    sebi_fraction: Decimal = Decimal("0")  # SEBI regulatory charge
    stamp_duty_buy_fraction: Decimal = Decimal("0")  # stamp duty on buy notional
    gst_rate: Decimal = Decimal("0")  # GST rate applied to the taxable base
    other_fraction: Decimal = Decimal("0")  # any other fraction of notional
    other_per_order: Decimal = Decimal("0")  # any other flat charge per fill

    def __post_init__(self) -> None:
        for name in (
            "brokerage_fraction",
            "brokerage_per_order",
            "stt_buy_fraction",
            "stt_sell_fraction",
            "exchange_txn_fraction",
            "sebi_fraction",
            "stamp_duty_buy_fraction",
            "gst_rate",
            "other_fraction",
            "other_per_order",
        ):
            object.__setattr__(self, name, non_negative_decimal(getattr(self, name), name))
        if self.gst_rate > Decimal("1"):
            raise ValueError("gst_rate must be <= 1")

    @classmethod
    def nse_fo_illustrative(cls) -> "IndiaCostSchedule":
        """Illustrative example schedule for NSE derivatives research.

        These values are example inputs only — see the module docstring. Replace
        with the actual schedule applicable to your broker/instrument.
        """
        return cls(
            brokerage_per_order=Decimal("0"),
            brokerage_fraction=Decimal("0"),
            stt_buy_fraction=Decimal("0"),
            stt_sell_fraction=Decimal("0.0000125"),
            exchange_txn_fraction=Decimal("0.00002"),
            sebi_fraction=Decimal("0.000001"),
            stamp_duty_buy_fraction=Decimal("0.000002"),
            gst_rate=Decimal("0.18"),
        )

    def compute(self, side: OrderSide, notional: Decimal, quantity: int) -> ChargeBreakdown:
        """Compute the full charge breakdown for one fill (exact Decimal math)."""
        notional = positive_decimal(notional, "notional")
        quantity = positive_int(quantity, "quantity")

        brokerage = self.brokerage_fraction * notional + self.brokerage_per_order

        stt_rate = self.stt_buy_fraction if side == OrderSide.BUY else self.stt_sell_fraction
        stt = notional * stt_rate

        exchange_charges = notional * self.exchange_txn_fraction
        sebi_charges = notional * self.sebi_fraction

        stamp_duty = notional * self.stamp_duty_buy_fraction if side == OrderSide.BUY else Decimal("0")

        taxable_base = brokerage + exchange_charges + sebi_charges
        gst = self.gst_rate * taxable_base

        other_charges = notional * self.other_fraction + self.other_per_order

        return ChargeBreakdown(
            side=side,
            notional=notional,
            quantity=quantity,
            brokerage=brokerage,
            stt=stt,
            exchange_charges=exchange_charges,
            sebi_charges=sebi_charges,
            stamp_duty=stamp_duty,
            gst=gst,
            other_charges=other_charges,
        )