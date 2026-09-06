"""Offline backtest demo: ``python -m fno_ai_paper_trading.backtest``.

Runs the moving-average crossover strategy over a deterministic dataset and
prints a concise performance summary. Everything is simulated — no network,
no credentials, no live orders.
"""
from __future__ import annotations

from decimal import Decimal

from fno_ai_paper_trading.backtest.config import BacktestConfig
from fno_ai_paper_trading.backtest.datasets import build_profitable_series
from fno_ai_paper_trading.backtest.engine import BacktestEngine
from fno_ai_paper_trading.data.mock_provider import build_sample_instruments
from fno_ai_paper_trading.strategies import MovingAverageCrossStrategy

_DEFAULT_CONFIG = BacktestConfig(
    initial_capital=Decimal("100000"),
    quantity=10,
)


def main() -> None:
    instrument = build_sample_instruments()[0]  # NIFTY1 future
    bars = build_profitable_series(instrument)
    strategy = MovingAverageCrossStrategy(fast=2, slow=3)

    result = BacktestEngine().run(bars, strategy, _DEFAULT_CONFIG)

    lines = [
        "F&O AI Paper Trading — Backtest Demo",
        f"strategy      : {strategy.name} (fast={strategy.fast}, slow={strategy.slow})",
        f"bars          : {result.num_bars_processed}",
        f"initial capital: {result.initial_capital}",
        f"final equity  : {result.final_equity}",
        f"total P&L     : {result.total_pnl}",
        f"total return  : {result.total_return_pct:f}%",
        f"max drawdown  : {result.max_drawdown} ({result.max_drawdown_pct:f}%)",
        f"trades        : {result.num_trades}",
        f"win rate      : {result.win_rate:f}%",
        f"gross profit  : {result.gross_profit}",
        f"gross loss    : {result.gross_loss}",
        f"total commission: {result.total_commission}",
        f"profit factor : {result.profit_factor}",
    ]
    print("\n".join(lines))
    print("Backtest demo complete - simulated paper execution only.")


if __name__ == "__main__":
    main()