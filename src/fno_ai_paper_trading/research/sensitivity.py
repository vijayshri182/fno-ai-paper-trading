"""Parameter sensitivity research utility.

Evaluates a strategy at several *explicitly supplied* parameter combinations.
This is deliberately NOT an optimizer: the caller enumerates the combinations
to try, and the utility reports each result side by side. The research
question is "does performance survive reasonable parameter changes?", never
"which parameters maximize backtest return?".

For the moving-average crossover, valid combinations require ``fast < slow``.
Invalid combinations supplied by the caller are validated and reported as
skipped instead of silently run.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from fno_ai_paper_trading.backtest.config import BacktestConfig
from fno_ai_paper_trading.backtest.engine import BacktestEngine
from fno_ai_paper_trading.models.market import MarketPrice
from fno_ai_paper_trading.strategies import MovingAverageCrossStrategy


@dataclass(frozen=True)
class SensitivityRow:
    """One evaluated (fast, slow) combination."""

    fast: int
    slow: int
    net_pnl: Decimal
    total_return_pct: Decimal
    final_equity: Decimal
    max_drawdown_pct: Decimal
    num_trades: int
    win_rate: Decimal
    profit_factor: Decimal


@dataclass(frozen=True)
class SensitivityRowSkipped:
    """A supplied combination that was not run (e.g. fast >= slow)."""

    fast: int
    slow: int
    reason: str


@dataclass
class SensitivityResult:
    """Ordered results for every supplied combination."""

    rows: list[SensitivityRow] = field(default_factory=list)
    skipped: list[SensitivityRowSkipped] = field(default_factory=list)

    @property
    def num_runs(self) -> int:
        return len(self.rows)

    @property
    def num_skipped(self) -> int:
        return len(self.skipped)


def validate_ma_pairs(fast_values: list[int], slow_values: list[int]) -> list[tuple[int, int]]:
    """Return valid (fast, slow) pairs; caller keeps them explicitly enumerated."""
    pairs: list[tuple[int, int]] = []
    for fast in fast_values:
        for slow in slow_values:
            if fast < slow:
                pairs.append((fast, slow))
    return pairs


def run_parameter_sensitivity(
    bars: list[MarketPrice],
    config: BacktestConfig,
    fast_values: list[int],
    slow_values: list[int],
    engine: object | None = None,  # BacktestEngine
) -> SensitivityResult:
    """Run every explicitly supplied (fast, slow) combination (fast < slow).

    Combinations where ``fast >= slow`` are logged in ``skipped`` and never
    executed. Results preserve the caller's enumeration order.
    """
    engine = engine or BacktestEngine()
    result = SensitivityResult()

    for pair in validate_ma_pairs(fast_values, slow_values):
        fast, slow = pair
        strategy = MovingAverageCrossStrategy(fast=fast, slow=slow)
        run = engine.run(bars, strategy, config)  # type: ignore[arg-type]
        result.rows.append(
            SensitivityRow(
                fast=fast,
                slow=slow,
                net_pnl=run.total_pnl,
                total_return_pct=run.total_return_pct,
                final_equity=run.final_equity,
                max_drawdown_pct=run.max_drawdown_pct,
                num_trades=run.num_trades,
                win_rate=run.win_rate,
                profit_factor=run.profit_factor,
            )
        )
        del strategy

    for fast in fast_values:
        for slow in slow_values:
            if fast >= slow:
                result.skipped.append(
                    SensitivityRowSkipped(fast=fast, slow=slow, reason="fast must be < slow")
                )

    return result