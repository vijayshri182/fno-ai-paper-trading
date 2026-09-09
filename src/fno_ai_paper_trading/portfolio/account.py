"""Virtual paper-trading account model.

A :class:`PaperAccount` is the explicit "virtual capital + policy" object for a
V1-style paper session. It owns an identity, an immutable starting-capital
snapshot and a :class:`Portfolio` that performs all accounting. The account
adds the V1 long-only policy (``long_only``) and exposes the portfolio's state
without duplicating accounting logic.

No persistence or session loop is implemented here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Mapping

from fno_ai_paper_trading.models.order import Fill
from fno_ai_paper_trading.models.position import Position, Trade
from fno_ai_paper_trading.portfolio.portfolio import Portfolio
from fno_ai_paper_trading.utils.functions import positive_decimal


@dataclass
class PaperAccount:
    """Virtual paper-trading account state.

    The embedded :class:`Portfolio` holds cash, positions, trades and realized
    P&L. ``long_only`` is the account-level V1 direction policy: it is passed
    through to the portfolio, which rejects any ``SELL`` fill that would create
    or increase a short position.
    """

    account_id: str
    initial_capital: Decimal
    long_only: bool = True
    created_at: datetime = field(default_factory=datetime.now)
    portfolio: Portfolio = field(init=False)

    def __post_init__(self) -> None:
        account_id = self.account_id.strip()
        if not account_id:
            raise ValueError("account_id must not be empty")
        self.account_id = account_id
        self.initial_capital = positive_decimal(self.initial_capital, "initial_capital")
        self.portfolio = Portfolio(
            self.initial_capital,
            long_only=self.long_only,
        )

    @property
    def cash(self) -> Decimal:
        """Available cash in the virtual account."""
        return self.portfolio.cash

    @property
    def realized_pnl(self) -> Decimal:
        """Cumulative realized P&L from closed positions."""
        return self.portfolio.realized_pnl

    @property
    def positions(self) -> dict[str, Position]:
        """Current open positions keyed by symbol."""
        return self.portfolio.open_positions()

    def apply_fill(self, fill: Fill) -> Trade:
        """Apply a broker fill to the account's portfolio."""
        return self.portfolio.apply_fill(fill)

    def equity(self, market_prices: Mapping[str, Decimal]) -> Decimal:
        """Cash + market value of open positions at the given prices."""
        return self.portfolio.total_value(market_prices)

    def current_quantity(self, symbol: str) -> int:
        return self.portfolio.current_quantity(symbol)
