"""Backtest result models: equity curve point and full run report."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from fno_ai_paper_trading.models.position import Trade


@dataclass(frozen=True)
class EquityPoint:
    """One snapshot on the equity curve after processing a bar."""

    timestamp: datetime
    bar_index: int
    equity: Decimal
    cash: Decimal
    unrealized_pnl: Decimal
    drawdown_from_peak: Decimal


@dataclass(frozen=True)
class BacktestResult:
    """Complete report produced by :class:`BacktestEngine`.

    ``gross_profit`` / ``gross_loss`` are raw price-based P&L components
    (including slippage but excluding commission). ``slippage_cost`` is the
    total adverse price gap paid across all fills
    (``abs(fill_price - bar_close) * qty * multiplier``); ``total_commission``
    is the sum of all fill commissions. ``transaction_costs`` is the broker
    price for trading (slippage + commission). ``total_pnl`` is the net P&L
    (final equity minus initial capital), so it captures both slippage and
    commission effects.
    """

    initial_capital: Decimal
    final_equity: Decimal
    total_pnl: Decimal
    total_return_pct: Decimal
    num_bars_processed: int
    signals_generated: int
    orders_submitted: int
    orders_filled: int
    num_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: Decimal
    gross_profit: Decimal
    gross_loss: Decimal
    total_commission: Decimal
    profit_factor: Decimal
    max_drawdown: Decimal
    max_drawdown_pct: Decimal
    slippage_cost: Decimal = Decimal("0")
    equity_curve: list[EquityPoint] = field(default_factory=list)
    trades: list[Trade] = field(default_factory=list)

    @property
    def transaction_costs(self) -> Decimal:
        """Total broker price of trading: slippage + commission."""
        return self.total_commission + self.slippage_cost
