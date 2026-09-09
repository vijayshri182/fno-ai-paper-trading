"""V1 risk-based position sizing.

A pure, deterministic computation of a single-instrument ``BUY`` entry quantity
that risks ``risk_per_trade_pct`` of *current* equity over a fixed
``stop_loss_pct`` stop distance:

    risk_amount   = equity * risk_per_trade_pct
    stop_distance = entry_price * stop_loss_pct
    stop_price    = entry_price * (1 - stop_loss_pct)
    raw_quantity  = risk_amount / (stop_distance * instrument.multiplier)
    quantity      = floor(raw_quantity / instrument.lot_size) * instrument.lot_size

The sizer never touches a portfolio, an account, a broker or a settings object
and never performs accounting. Callers supply the decision-time numbers
(equity, available cash, entry price), so sizing cannot read future or stale
state and cannot leak look-ahead equity.

Semantics:

- ``BUY`` only: a ``SELL`` request never passes through sizing (a short position
  is never proposed by this component).
- Round **down** only, in increments of ``Instrument.lot_size`` (the NIFTY 50
  index uses ``lot_size=1``/``multiplier=1`` in this repo's registry; no
  futures/options lot size is invented here).
- Cash / no-leverage guard: a sized entry is capped by available cash so that
  ``entry notional + estimated entry commission <= available_cash``, using the
  project's existing commission convention
  ``commission = notional * commission_rate + commission_fixed``. The sizer
  duplicates no portfolio accounting logic — it only *estimates* the
  commission that the cash constraint must leave room for.
- Single-position V1: sizing is skipped when ``current_quantity != 0``.
- Every skip (invalid input, below-increment, insufficient cash, position
  already open) returns a :class:`SizingResult` with ``approved=False`` and a
  recorded ``skip_reason`` instead of raising, so callers never construct an
  invalid order.

Static ``RiskManager`` caps (``max_position_quantity`` / ``max_order_notional`` /
``max_daily_loss``) are **not** duplicated here; ``RiskManager`` remains the
authoritative final gate over the resulting order. All money math is ``Decimal``.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from fno_ai_paper_trading.models.instruments import Instrument
from fno_ai_paper_trading.utils.functions import (
    non_negative_decimal,
    positive_decimal,
    positive_int,
    to_decimal,
)


@dataclass(frozen=True)
class SizerConfig:
    """Fixed sizing parameters; mirror the project's paper risk/cost defaults."""

    risk_per_trade_pct: Decimal = Decimal("0.01")  # 1% of current equity risked
    stop_loss_pct: Decimal = Decimal("0.02")  # stop distance below the entry price
    commission_rate: Decimal = Decimal("0.0003")  # fraction of notional (paper cost model)
    commission_fixed: Decimal = Decimal("0")  # flat fee per fill

    def __post_init__(self) -> None:
        risk = positive_decimal(self.risk_per_trade_pct, "risk_per_trade_pct")
        stop = positive_decimal(self.stop_loss_pct, "stop_loss_pct")
        if stop >= 1:
            raise ValueError("stop_loss_pct must be < 1")
        rate = non_negative_decimal(self.commission_rate, "commission_rate")
        fixed = non_negative_decimal(self.commission_fixed, "commission_fixed")
        object.__setattr__(self, "risk_per_trade_pct", risk)
        object.__setattr__(self, "stop_loss_pct", stop)
        object.__setattr__(self, "commission_rate", rate)
        object.__setattr__(self, "commission_fixed", fixed)


@dataclass(frozen=True)
class SizingResult:
    """Outcome of a single sizing attempt.

    ``quantity`` is the sized whole-lot quantity when ``approved`` is ``True``
    (zero otherwise). ``risk_amount`` / ``stop_distance`` / ``stop_price`` are
    the computed risk basis on an approved result. A rejection carries a
    human-readable ``skip_reason``.
    """

    approved: bool
    quantity: int = 0
    risk_amount: Decimal = Decimal("0")
    stop_distance: Decimal = Decimal("0")
    stop_price: Decimal = Decimal("0")
    skip_reason: str = ""


class RiskBasedPositionSizer:
    """Sizes a V1 ``BUY`` entry from current equity and a fixed stop distance."""

    def __init__(self, config: SizerConfig | None = None) -> None:
        self.config = config if config is not None else SizerConfig()

    def size(
        self,
        *,
        equity: Decimal | int | float | str,
        available_cash: Decimal | int | float | str,
        entry_price: Decimal | int | float | str,
        instrument: Instrument,
        current_quantity: int = 0,
    ) -> SizingResult:
        """Compute the V1 entry quantity for ``instrument``.

        ``equity`` and ``available_cash`` must be the decision-time values
        (current equity at the completed-bar close, and ``portfolio.cash``);
        ``entry_price`` is the execution reference close. All are explicit
        inputs — the sizer never reads portfolio/account state.
        """
        if not isinstance(instrument, Instrument):
            raise TypeError("instrument must be an Instrument")

        if not isinstance(current_quantity, int) or isinstance(current_quantity, bool):
            return self._skipped("current_quantity must be an integer")
        if current_quantity != 0:
            return self._skipped("V1 is single-position: a position is already open")

        equity_dec = self._finite(equity, "equity")
        if equity_dec is None or equity_dec <= 0:
            return self._skipped("equity must be a positive finite number")

        entry = self._finite(entry_price, "entry_price")
        if entry is None or entry <= 0:
            return self._skipped("entry_price must be a positive finite number")

        cash = self._finite(available_cash, "available_cash")
        if cash is None:
            return self._skipped("available_cash must be a finite number")

        lot = positive_int(instrument.lot_size, "instrument.lot_size")
        multiplier = positive_int(instrument.multiplier, "instrument.multiplier")

        risk_amount = equity_dec * self.config.risk_per_trade_pct
        stop_distance = entry * self.config.stop_loss_pct
        stop_price = entry * (Decimal("1") - self.config.stop_loss_pct)
        denominator = stop_distance * multiplier

        if risk_amount <= 0 or denominator <= 0:
            return self._skipped("risk/stop configuration yields a non-positive basis")

        raw_quantity = risk_amount / denominator
        quantity = int(raw_quantity // lot) * lot

        if quantity <= 0:
            return self._skipped(
                "computed quantity is below the minimum quantity increment "
                f"(lot_size={instrument.lot_size})"
            )

        quantity = self._fit_to_cash(quantity, entry, multiplier, lot, cash)
        if quantity <= 0:
            return self._skipped(
                "insufficient available cash for the minimum quantity increment"
            )

        return SizingResult(
            approved=True,
            quantity=quantity,
            risk_amount=risk_amount,
            stop_distance=stop_distance,
            stop_price=stop_price,
        )

    def _fit_to_cash(
        self,
        quantity: int,
        entry: Decimal,
        multiplier: int,
        lot: int,
        cash: Decimal,
    ) -> int:
        """Largest whole-lot ``quantity`` whose notional + estimated commission fits cash.

        Constraint: ``qty * entry * mult * (1 + commission_rate) + commission_fixed
        <= available_cash``. This uses the project's commission convention
        without applying any accounting (the sizer never moves money).
        """
        unit_cost = entry * multiplier * (Decimal("1") + self.config.commission_rate)
        max_units = (cash - self.config.commission_fixed) // unit_cost
        max_quantity = int(max_units // lot) * lot
        if quantity > max_quantity:
            quantity = max_quantity
        return quantity

    @staticmethod
    def _finite(value, name: str) -> Decimal | None:
        try:
            result = to_decimal(value)
        except (InvalidOperation, TypeError, ValueError):
            return None
        if not result.is_finite():
            return None
        return result

    @staticmethod
    def _skipped(reason: str) -> SizingResult:
        return SizingResult(approved=False, skip_reason=reason)