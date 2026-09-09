"""Automatic fixed-percentage stop-loss enforcement (WS 6.4).

A pure, deterministic protective-exit rule for LONG positions plus the single
authoritative executor shared by every runtime.

Rule (evaluated on each completed candle that is *after* the entry candle):

    stop        = average_entry_price * (1 - stop_loss_pct)
    open <= stop -> exit at the candle open          (gap / open-through)
    low  <= stop -> exit at exactly ``stop``          (intrabar breach)
    otherwise    -> no exit

Equality is inclusive (``open == stop`` and ``low == stop`` both exit at the
stop price). LONG-ONLY: flat and short positions are ignored. The entry candle
itself is never evaluated (``position.opened_at >= bar.timestamp``), so a BUY
that fills at a bar's close can never be stopped out on the same candle. The
rule consumes nothing beyond the completed candle and the position's entry — no
look-ahead.

:func:`enforce_stop` is the one place a stop decision becomes an order. It
builds a full-close ``SELL`` :class:`~fno_ai_paper_trading.models.order.Order`
labelled ``OrderType.STOP``, routes it through the existing broker (a
deterministic single-price bar supplies the decision reference), applies the
resulting fill through :meth:`Portfolio.apply_fill`, and never consults the
``RiskManager`` — a protective exit must remain executable after the daily-loss
limit is reached. No alternate P&L/commission logic exists here; the standard
broker slippage and commission model prices every stop fill identically to any
other order.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from fno_ai_paper_trading.broker.base import Broker
from fno_ai_paper_trading.models.enums import OrderSide, OrderType
from fno_ai_paper_trading.models.market import MarketPrice
from fno_ai_paper_trading.models.order import Fill, Order
from fno_ai_paper_trading.models.position import Position, Trade
from fno_ai_paper_trading.portfolio.portfolio import Portfolio
from fno_ai_paper_trading.utils.functions import positive_decimal


@dataclass(frozen=True)
class StopDecision:
    """A deterministic protective-exit decision for one (position, candle)."""

    exit_price: Decimal  # decision reference price: the candle open or the stop
    branch: str          # "open" (gap/open-through) or "stop" (intrabar breach)

    def __post_init__(self) -> None:
        object.__setattr__(self, "exit_price", positive_decimal(self.exit_price, "exit_price"))
        if self.branch not in ("open", "stop"):
            raise ValueError("branch must be either 'open' or 'stop'")


@dataclass(frozen=True)
class StopExitResult:
    """Everything produced by a protective stop execution attempt."""

    order: Order
    fill: Fill | None = None
    trade: Trade | None = None
    reference_price: Decimal | None = None  # decision price used for the fill

    def __post_init__(self) -> None:
        if self.reference_price is not None:
            object.__setattr__(
                self, "reference_price", positive_decimal(self.reference_price, "reference_price")
            )


class StopLossPolicy:
    """Fixed-percentage stop rule positioned below the filled entry price.

    The stop is anchored to ``Position.average_entry_price`` (which already
    includes BUY slippage), so the risk distance is exactly ``stop_loss_pct``
    below the actual fill, not below a pre-slippage reference close.
    """

    def __init__(self, stop_loss_pct: Decimal = Decimal("0.02")) -> None:
        pct = positive_decimal(stop_loss_pct, "stop_loss_pct")
        if pct >= 1:
            raise ValueError("stop_loss_pct must be < 1")
        self.stop_loss_pct = pct

    def stop_price(self, position: Position) -> Decimal:
        """Deterministic stop level for ``position`` below its average entry."""
        return position.average_entry_price * (Decimal("1") - self.stop_loss_pct)

    def evaluate(self, *, position: Position, bar: MarketPrice) -> StopDecision | None:
        """Return a :class:`StopDecision` for a completed candle, or ``None``.

        LONG-ONLY: flat/short positions are ignored. The entry candle is
        excluded (``position.opened_at >= bar.timestamp``), so no position can
        be stopped out before a later completed candle exists.
        """
        if not position.is_long:
            return None
        if position.opened_at >= bar.timestamp:
            return None
        stop = self.stop_price(position)
        if bar.open <= stop:
            return StopDecision(exit_price=bar.open, branch="open")
        if bar.low <= stop:
            return StopDecision(exit_price=stop, branch="stop")
        return None


def enforce_stop(
    *,
    broker: Broker,
    portfolio: Portfolio,
    position: Position,
    bar: MarketPrice,
    policy: StopLossPolicy | None = None,
) -> StopExitResult | None:
    """Evaluate the stop and, if triggered, execute the protective exit.

    The single authoritative executor: builds a full-close ``SELL`` order of
    ``OrderType.STOP``, places it through ``broker`` (existing slippage and
    commission model), and applies the fill through :meth:`Portfolio.apply_fill`
    — the only accounting path. The ``RiskManager`` is never consulted, so a
    protective exit always remains executable. Returns ``None`` when the policy
    produces no decision, or a :class:`StopExitResult` describing the attempt.
    """
    policy = policy if policy is not None else StopLossPolicy()
    decision = policy.evaluate(position=position, bar=bar)
    if decision is None:
        return None

    order = Order(
        instrument=position.instrument,
        side=OrderSide.SELL,
        quantity=position.quantity,
        order_type=OrderType.STOP,
    )

    # The existing broker interface fills against ``market_price.close``. A
    # deterministic single-price candle carries the decision reference through
    # the standard slippage/commission math (validation: o=h=l=c).
    exit_bar = MarketPrice(
        instrument=bar.instrument,
        timestamp=bar.timestamp,
        open=decision.exit_price,
        high=decision.exit_price,
        low=decision.exit_price,
        close=decision.exit_price,
    )

    fill = broker.place_order(order, exit_bar)
    reference_price = decision.exit_price
    if fill is None:
        return StopExitResult(order=order, reference_price=reference_price)

    trade = portfolio.apply_fill(fill)
    return StopExitResult(order=order, fill=fill, trade=trade, reference_price=reference_price)