"""Record types for historical strategy evaluation (WS 7.4).

These are plain, deterministic, serializable records. They standardize the
evaluation metric set (§17e / §18 of ``PROJECT_PLAN.md``): P&L, return %,
win rate, round trips, average trade, transaction costs, max drawdown (+ %),
exposure, and losing streak.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Mapping, Sequence

from fno_ai_paper_trading.backtest.config import BacktestConfig
from fno_ai_paper_trading.backtest.result import EquityPoint
from fno_ai_paper_trading.utils.functions import positive_decimal, positive_int


@dataclass(frozen=True)
class EvaluationConfig:
    """Immutable evaluation assumptions (wraps :class:`BacktestConfig`)."""

    initial_capital: Decimal = Decimal("100000")
    quantity: int = 1
    commission_rate: Decimal = Decimal("0.0003")
    commission_fixed: Decimal = Decimal("0")
    slippage_rate: Decimal = Decimal("0.001")
    enable_risk_manager: bool = True
    max_position_quantity: int = 75
    max_order_notional: Decimal = Decimal("250000")
    max_daily_loss: Decimal = Decimal("10000")
    enable_stop_loss: bool = True
    stop_loss_pct: Decimal = Decimal("0.02")
    cost_schedule: object | None = None
    execution: object | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "initial_capital", positive_decimal(self.initial_capital, "initial_capital"))
        object.__setattr__(self, "quantity", positive_int(self.quantity, "quantity"))

    def backtest(self) -> BacktestConfig:
        """Build the :class:`BacktestConfig` for one replay run."""
        return BacktestConfig(
            initial_capital=self.initial_capital,
            quantity=self.quantity,
            commission_rate=self.commission_rate,
            commission_fixed=self.commission_fixed,
            slippage_rate=self.slippage_rate,
            enable_risk_manager=self.enable_risk_manager,
            max_position_quantity=self.max_position_quantity,
            max_order_notional=self.max_order_notional,
            max_daily_loss=self.max_daily_loss,
            enable_stop_loss=self.enable_stop_loss,
            stop_loss_pct=self.stop_loss_pct,
            cost_schedule=self.cost_schedule,
            execution=self.execution,
        )


@dataclass(frozen=True)
class SessionEvaluation:
    """Standardized per-session evaluation record."""

    dataset_name: str
    dataset_hash: str
    start_date: date | None
    end_date: date | None
    bars_processed: int
    strategy_name: str
    strategy_params: Mapping[str, Any]
    net_pnl: Decimal
    net_return_pct: Decimal
    win_rate: Decimal
    num_trades: int
    total_commission: Decimal
    slippage_cost: Decimal
    max_drawdown: Decimal
    max_drawdown_pct: Decimal
    exposure_pct: Decimal


@dataclass(frozen=True)
class EvaluationAggregate:
    """Standardized aggregate across a run of evaluated sessions."""

    sessions: int
    bars_processed: int
    num_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: Decimal
    total_pnl: Decimal
    total_return_pct: Decimal
    transaction_costs: Decimal
    max_drawdown: Decimal
    max_drawdown_pct: Decimal
    avg_trade: Decimal | None
    losing_streak_bars: int
    exposure_pct: Decimal
    profit_factor: Decimal
    gross_profit: Decimal
    gross_loss: Decimal


@dataclass(frozen=True)
class EvaluationRun:
    """One complete historical evaluation over one or more sessions."""

    name: str
    strategy_name: str
    strategy_params: Mapping[str, Any]
    config: EvaluationConfig
    created_at: str
    baseline: bool
    sessions: tuple[SessionEvaluation, ...]
    aggregate: EvaluationAggregate


# ---------------------------------------------------------------------------
# aggregation helpers (pure, deterministic)
# ---------------------------------------------------------------------------

def composite_curve(
    sessions: Sequence[SessionEvaluation],
    curves: Sequence[Sequence[EquityPoint]],
    initial_capital: Decimal,
) -> list[EquityPoint]:
    """Chain per-session equity curves into one continuous curve.

    Every session replayed with the same ``initial_capital``, so each session's
    incremental change is ``point.equity - initial_capital``; the composite adds
    that change onto a running total. Points keep their original timestamps.
    """
    running = initial_capital
    merged: list[EquityPoint] = []
    for curve in curves:
        if not curve:
            continue
        for point in curve:
            incremental = point.equity - initial_capital
            merged.append(
                EquityPoint(
                    timestamp=point.timestamp,
                    bar_index=point.bar_index,
                    equity=running + incremental,
                    cash=point.cash,
                    unrealized_pnl=point.unrealized_pnl,
                    drawdown_from_peak=Decimal("0"),
                )
            )
    return merged


def max_drawdown_of(curve: Sequence[EquityPoint]) -> Decimal:
    """Largest peak-to-trough equity decline along a curve."""
    peak = Decimal("0")
    worst = Decimal("0")
    for point in curve:
        peak = max(peak, point.equity)
        depth = peak - point.equity
        worst = max(worst, depth)
    return worst


def losing_streak_of(curve: Sequence[EquityPoint]) -> int:
    """Longest consecutive run of equity declines (bar-to-bar)."""
    longest = 0
    current = 0
    previous: Decimal | None = None
    for point in curve:
        if previous is not None:
            if point.equity < previous:
                current += 1
                longest = max(longest, current)
            else:
                current = 0
        previous = point.equity
    return longest