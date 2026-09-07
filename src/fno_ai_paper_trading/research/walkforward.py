"""Deterministic walk-forward evaluation.

Walk-forward simulates what a rolling research process would do: fit/select on
a training window, evaluate on the immediately following test window, then
advance. Each test window is out-of-sample with respect to the strategy built
from the training window that precedes it.

Windows are contiguous and non-overlapping:
::

    [ train ][ test ] -> advance -> [ train2 ][ test2 ] -> ...

The strategy for each window is produced by a caller-supplied factory
``build_strategy(train_bars)``, which is exactly where a "fit on train" step
would live. Nothing is fitted on a test window here, and no window ever uses a
bar from a later window. Fully deterministic.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from fno_ai_paper_trading.backtest.config import BacktestConfig
from fno_ai_paper_trading.backtest.engine import BacktestEngine
from fno_ai_paper_trading.backtest.result import BacktestResult
from fno_ai_paper_trading.models.market import MarketPrice
from fno_ai_paper_trading.strategies.base import Strategy
from fno_ai_paper_trading.utils.functions import positive_int


@dataclass(frozen=True)
class WalkForwardStep:
    """One train/test window boundary (bar indices, half-open)."""

    index: int
    train_start: int
    train_end: int
    test_start: int
    test_end: int


@dataclass
class WalkForwardResult:
    """Aggregated walk-forward outcome over all test windows."""

    train_size: int
    test_size: int
    step: int
    steps: list[WalkForwardStep]
    per_step: list[BacktestResult] = field(default_factory=list)
    total_oos_bars: int = 0
    total_trades: int = 0
    total_wins: int = 0
    total_losses: int = 0
    gross_profit: Decimal = Decimal("0")
    gross_loss: Decimal = Decimal("0")
    total_commission: Decimal = Decimal("0")
    combined_return_pct: Decimal | None = None


def plan_windows(n_bars: int, train_size: int, test_size: int, step: int) -> list[WalkForwardStep]:
    """Build the train/test index windows for ``n_bars`` bars."""
    n_bars = positive_int(n_bars, "n_bars")
    train_size = positive_int(train_size, "train_size")
    test_size = positive_int(test_size, "test_size")
    step = positive_int(step, "step")
    if step < test_size:
        raise ValueError("walk-forward step must be >= test_size (windows must not overlap)")

    windows: list[WalkForwardStep] = []
    start = 0
    index = 0
    while start + train_size + test_size <= n_bars:
        windows.append(
            WalkForwardStep(
                index=index,
                train_start=start,
                train_end=start + train_size,
                test_start=start + train_size,
                test_end=start + train_size + test_size,
            )
        )
        start += step
        index += 1
    return windows


def run_walk_forward(
    bars: list[MarketPrice],
    build_strategy: object,  # Callable[[list[MarketPrice]], Strategy]
    config: BacktestConfig,
    *,
    train_size: int,
    test_size: int,
    step: int | None = None,
    engine: object | None = None,  # BacktestEngine
) -> WalkForwardResult:
    """Run one strategy-per-window over contiguous train/test windows.

    ``build_strategy(train_bars)`` must return a :class:`Strategy`. Each test
    window is evaluated by running the backtest engine over the test bars only,
    so the strategy never sees any bar from a later window.
    """
    step = test_size if step is None else step
    windows = plan_windows(len(bars), train_size, test_size, step)
    engine = engine or BacktestEngine()

    result = WalkForwardResult(train_size=train_size, test_size=test_size, step=step, steps=windows)

    cum_factor = Decimal("1")
    for window in windows:
        train_bars = bars[window.train_start : window.train_end]
        test_bars = bars[window.test_start : window.test_end]
        strategy = build_strategy(train_bars)  # type: ignore[call-arg]
        if not isinstance(strategy, Strategy):
            raise TypeError("walk-forward build_strategy must return a Strategy")

        step_result = engine.run(test_bars, strategy, config)  # type: ignore[arg-type]
        result.per_step.append(step_result)
        result.total_oos_bars += len(test_bars)
        result.total_trades += step_result.num_trades
        result.total_wins += step_result.winning_trades
        result.total_losses += step_result.losing_trades
        result.gross_profit += step_result.gross_profit
        result.gross_loss += step_result.gross_loss
        result.total_commission += step_result.total_commission
        if step_result.total_return_pct.is_finite():
            cum_factor *= Decimal("1") + step_result.total_return_pct / Decimal("100")

    result.combined_return_pct = (cum_factor - Decimal("1")) * Decimal("100")
    return result