"""Portfolio accounting: cash, positions, realized/unrealized P&L.

All money values are :class:`decimal.Decimal`. Quantities are managed as signed
positions (long = positive, short = negative) and updated from broker fills.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Mapping

from fno_ai_paper_trading.models.enums import OrderSide
from fno_ai_paper_trading.models.order import Fill
from fno_ai_paper_trading.models.position import Position, Trade
from fno_ai_paper_trading.utils.functions import new_id, positive_decimal


@dataclass
class Portfolio:
    """Tracks cash, open positions, trade history and P&L.

    All money values are :class:`decimal.Decimal`. Quantities are managed as
    signed positions (long = positive, short = negative) and updated from
    broker fills.

    One opt-in account-policy guard is enforced inside :meth:`apply_fill`
    (off by default to preserve the existing generic behavior):

    - ``long_only``: a ``SELL`` fill that would open or increase a short
      position (resulting quantity < 0) is rejected.

    The guard is off by default so the generic :class:`Portfolio` keeps its
    existing short-capable behavior. A V1 paper account enables it.

    Guard violations raise ``ValueError`` *before* any state is mutated, so a
    rejected fill leaves cash, positions and trade history unchanged.
    """

    cash: Decimal
    positions: dict[str, Position] = field(default_factory=dict)
    trade_history: list[Trade] = field(default_factory=list)
    realized_pnl: Decimal = Decimal("0")
    initial_cash: Decimal = field(init=False)
    long_only: bool = False

    def __post_init__(self) -> None:
        self.cash = positive_decimal(self.cash, "cash")
        self.initial_cash = self.cash
        self.realized_pnl = Decimal(self.realized_pnl)

    def position_for(self, symbol: str) -> Position | None:
        return self.positions.get(symbol)

    def current_quantity(self, symbol: str) -> int:
        position = self.positions.get(symbol)
        return position.quantity if position else 0

    def open_positions(self) -> dict[str, Position]:
        return {symbol: pos for symbol, pos in self.positions.items() if not pos.is_flat}

    def apply_fill(self, fill: Fill) -> Trade:
        """Apply a fill to cash and positions, recording a trade and P&L."""
        multiplier = fill.instrument.multiplier
        notional = fill.quantity * fill.price * multiplier
        symbol = fill.instrument.symbol

        if fill.side == OrderSide.SELL and self.long_only:
            resulting = self.current_quantity(symbol) - fill.quantity
            if resulting < 0:
                raise ValueError(
                    "long-only portfolio rejects a SELL fill that would open or increase a short position "
                    f"({self.current_quantity(symbol)} -> {resulting})"
                )

        if fill.side == OrderSide.BUY:
            self.cash -= notional + fill.commission
        else:
            self.cash += notional - fill.commission

        realized = self._update_position(fill)
        trade = Trade(
            trade_id=new_id("TRD"),
            instrument=fill.instrument,
            side=fill.side,
            quantity=fill.quantity,
            price=fill.price,
            commission=fill.commission,
            executed_at=fill.filled_at,
            realized_pnl=realized,
        )
        self.trade_history.append(trade)
        self.realized_pnl += realized
        return trade

    def _update_position(self, fill: Fill) -> Decimal:
        symbol = fill.instrument.symbol
        multiplier = fill.instrument.multiplier
        delta = fill.quantity if fill.side == OrderSide.BUY else -fill.quantity
        current = self.positions.get(symbol)
        old_qty = current.quantity if current else 0
        new_qty = old_qty + delta
        realized = Decimal("0")

        if old_qty == 0:
            self.positions[symbol] = Position(
                instrument=fill.instrument,
                quantity=new_qty,
                average_entry_price=fill.price,
                opened_at=fill.filled_at,
            )
            return realized

        entry = current.average_entry_price
        if (old_qty > 0) == (delta > 0):
            # Same direction: blend average entry price.
            new_entry = (entry * abs(old_qty) + fill.price * abs(delta)) / abs(new_qty)
            current.quantity = new_qty
            current.average_entry_price = new_entry
            return realized

        # Opposite direction: closes (or reverses) part of the existing position.
        per_unit = (fill.price - entry) if old_qty > 0 else (entry - fill.price)
        closing_qty = min(abs(old_qty), abs(delta))
        realized = per_unit * closing_qty * multiplier
        current.realized_pnl += realized
        current.quantity = new_qty

        if abs(delta) >= abs(old_qty) and new_qty != 0:
            # Reversal: the remaining new position enters at the fill price.
            current.average_entry_price = fill.price
        return realized

    def unrealized_pnl(self, market_prices: Mapping[str, Decimal]) -> Decimal:
        """Sum of unrealized P&L over open positions at given close prices."""
        total = Decimal("0")
        for symbol, position in self.open_positions().items():
            price = market_prices.get(symbol)
            if price is None:
                continue
            total += position.unrealized_pnl(price)
        return total

    def market_value(self, market_prices: Mapping[str, Decimal]) -> Decimal:
        """Current market value of all open positions at given close prices."""
        total = Decimal("0")
        for symbol, position in self.open_positions().items():
            price = market_prices.get(symbol)
            if price is None:
                continue
            total += position.market_value(price)
        return total

    def total_value(self, market_prices: Mapping[str, Decimal]) -> Decimal:
        """Cash plus current market value of open positions."""
        return self.cash + self.market_value(market_prices)

    def realized_pnl_today(self, today: date | None = None) -> Decimal:
        """Sum of realized P&L from trades executed on ``today`` (default: now)."""
        day = today if today is not None else datetime.now().date()
        return sum((t.realized_pnl for t in self.trade_history if t.executed_at.date() == day), Decimal("0"))