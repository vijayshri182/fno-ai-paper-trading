"""Historical strategy evaluation over validated datasets (WS 7.4).

Replays the frozen MA(5,21) baseline (or any strategy) deterministically over
one or more validated datasets with identical cost/execution assumptions and
produces the standardized evaluation records. This is the shared machinery later
used by five-year replay (WS 7.5) and champion/challenger comparison (WS 7.11).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Mapping, Sequence

from fno_ai_paper_trading.backtest.engine import BacktestEngine
from fno_ai_paper_trading.backtest.result import BacktestResult
from fno_ai_paper_trading.data.dataset_store import StoredDataset
from fno_ai_paper_trading.evaluation.records import (
    EvaluationAggregate,
    EvaluationConfig,
    EvaluationRun,
    SessionEvaluation,
    composite_curve,
    losing_streak_of,
    max_drawdown_of,
)
from fno_ai_paper_trading.models.market import MarketPrice
from fno_ai_paper_trading.research.experiment import run_dataset_experiment, run_experiment
from fno_ai_paper_trading.strategies.base import Strategy


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


@dataclass(frozen=True)
class _SessionRun:
    """Internal per-session summary used to build records and the aggregate."""

    session: SessionEvaluation
    curve: tuple
    result: BacktestResult


class HistoricalEvaluator:
    """Deterministic multi-session evaluator for a single strategy."""

    def __init__(
        self,
        *,
        config: EvaluationConfig | None = None,
        engine: BacktestEngine | None = None,
    ) -> None:
        self.config = config or EvaluationConfig()
        self._engine = engine or BacktestEngine()

    def evaluate_strategy(
        self,
        datasets: Sequence[StoredDataset],
        strategy: Strategy,
        *,
        name: str = "historical-evaluation",
        strategy_params: Mapping[str, Any] | None = None,
        baseline: bool = False,
    ) -> EvaluationRun:
        """Evaluate ``strategy`` over every dataset (validated before replay)."""
        params = dict(strategy_params or {})
        runs: list[_SessionRun] = []
        for dataset in datasets:
            experiment = run_dataset_experiment(
                dataset,
                strategy,
                self.config.backtest(),
                name=f"{name}:{dataset.path.stem}",
                strategy_params=params,
                cost_assumptions=(
                    f"rate={self.config.commission_rate} fixed={self.config.commission_fixed}"
                ),
                slippage_assumptions=f"rate={self.config.slippage_rate}",
            )
            runs.append(self._session_run(experiment.result, strategy.name, params,
                                          dataset.path.stem, experiment.config.dataset_hash,
                                          experiment.config.start_date,
                                          experiment.config.end_date,
                                          experiment.metrics.exposure_pct))
        return self._finish(runs, name=name, strategy_name=strategy.name,
                            strategy_params=params, baseline=baseline)

    def evaluate_bars(
        self,
        bars: Sequence[MarketPrice],
        strategy: Strategy,
        *,
        name: str = "historical-evaluation",
        dataset_name: str = "inline",
        dataset_hash: str = "",
        strategy_params: Mapping[str, Any] | None = None,
        baseline: bool = False,
    ) -> EvaluationRun:
        """Evaluate over an inline bar series (used by tests and demos)."""
        params = dict(strategy_params or {})
        experiment = run_experiment(
            list(bars),
            strategy,
            self.config.backtest(),
            name=f"{name}:{dataset_name}",
            strategy_params=params,
            dataset_name=dataset_name,
            dataset_hash=dataset_hash,
            cost_assumptions=(
                f"rate={self.config.commission_rate} fixed={self.config.commission_fixed}"
            ),
            slippage_assumptions=f"rate={self.config.slippage_rate}",
        )
        run = self._session_run(experiment.result, strategy.name, params,
                                dataset_name, dataset_hash,
                                experiment.config.start_date,
                                experiment.config.end_date,
                                experiment.metrics.exposure_pct)
        return self._finish([run], name=name, strategy_name=strategy.name,
                            strategy_params=params, baseline=baseline)

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    @staticmethod
    def _session_run(
        result: BacktestResult,
        strategy_name: str,
        strategy_params: Mapping[str, Any],
        dataset_name: str,
        dataset_hash: str,
        start_date,
        end_date,
        exposure_pct: Decimal,
    ) -> _SessionRun:
        session = SessionEvaluation(
            dataset_name=dataset_name,
            dataset_hash=dataset_hash,
            start_date=start_date,
            end_date=end_date,
            bars_processed=len(result.equity_curve),
            strategy_name=strategy_name,
            strategy_params=dict(strategy_params),
            net_pnl=result.total_pnl,
            net_return_pct=result.total_return_pct,
            win_rate=result.win_rate,
            num_trades=result.num_trades,
            total_commission=result.total_commission,
            slippage_cost=result.slippage_cost,
            max_drawdown=result.max_drawdown,
            max_drawdown_pct=result.max_drawdown_pct,
            exposure_pct=exposure_pct,
        )
        return _SessionRun(session=session, curve=tuple(result.equity_curve), result=result)

    def _finish(
        self,
        runs: Sequence[_SessionRun],
        *,
        name: str,
        strategy_name: str,
        strategy_params: Mapping[str, Any],
        baseline: bool,
    ) -> EvaluationRun:
        aggregate = self._aggregate(runs)
        return EvaluationRun(
            name=name,
            strategy_name=strategy_name,
            strategy_params=dict(strategy_params),
            config=self.config,
            created_at=_now_iso(),
            baseline=baseline,
            sessions=tuple(run.session for run in runs),
            aggregate=aggregate,
        )

    def _aggregate(self, runs: Sequence[_SessionRun]) -> EvaluationAggregate:
        initial = self.config.initial_capital
        bars_processed = sum(r.session.bars_processed for r in runs)
        num_trades = sum(r.result.num_trades for r in runs)
        winning = sum(r.result.winning_trades for r in runs)
        losing = sum(r.result.losing_trades for r in runs)
        gross_profit = sum(r.result.gross_profit for r in runs)
        gross_loss = sum(r.result.gross_loss for r in runs)
        total_commission = sum(r.result.total_commission for r in runs)
        slippage = sum(r.result.slippage_cost for r in runs)

        curve = composite_curve([r.session for r in runs], [r.curve for r in runs], initial)
        final_equity = curve[-1].equity if curve else initial
        total_pnl = final_equity - initial
        total_return_pct = (total_pnl / initial * Decimal("100")) if initial > 0 else Decimal("0")
        max_dd = max_drawdown_of(curve)
        peak = max((p.equity for p in curve), default=initial)
        max_dd_pct = (max_dd / peak * Decimal("100")) if peak > 0 else Decimal("0")
        losing_streak = losing_streak_of(curve)

        win_rate = (Decimal(winning) / Decimal(num_trades) * Decimal("100")) if num_trades > 0 else Decimal("0")
        profit_factor = _profit_factor(gross_profit, gross_loss)
        avg_trade = (total_pnl / Decimal(num_trades)) if num_trades > 0 else None
        exposure = _combined_exposure(runs, bars_processed)

        return EvaluationAggregate(
            sessions=len(runs),
            bars_processed=bars_processed,
            num_trades=num_trades,
            winning_trades=winning,
            losing_trades=losing,
            win_rate=win_rate,
            total_pnl=total_pnl,
            total_return_pct=total_return_pct,
            transaction_costs=total_commission + slippage,
            max_drawdown=max_dd,
            max_drawdown_pct=max_dd_pct,
            avg_trade=avg_trade,
            losing_streak_bars=losing_streak,
            exposure_pct=exposure,
            profit_factor=profit_factor,
            gross_profit=gross_profit,
            gross_loss=gross_loss,
        )


def _combined_exposure(runs: Sequence[_SessionRun], bars_processed: int) -> Decimal:
    if bars_processed <= 0:
        return Decimal("0")
    bars = sum(
        (r.session.exposure_pct / Decimal("100") * Decimal(str(r.session.bars_processed))
         for r in runs),
        Decimal("0"),
    )
    return bars / Decimal(bars_processed) * Decimal("100")


def _profit_factor(gross_profit: Decimal, gross_loss: Decimal) -> Decimal:
    if gross_loss < 0 and gross_profit > 0:
        return gross_profit / abs(gross_loss)
    if gross_loss >= 0 and gross_profit > 0:
        return Decimal("Infinity")
    return Decimal("0")