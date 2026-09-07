"""Research experiment structure.

A single, self-describing record of one backtest experiment so results are
reproducible: strategy, parameters, dataset, date range, capital, cost and
slippage assumptions, metrics, and a version/hash. No database is used — the
record is a plain dataclass that can be serialized into a report or a JSON
artifact.

``config_hash`` is a stable SHA-256 over the deterministic configuration
fields, so two identical experiments produce the same hash (and any change to
costs, execution, parameters, or dataset changes it).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Callable, Mapping

from fno_ai_paper_trading.backtest.config import BacktestConfig
from fno_ai_paper_trading.backtest.engine import BacktestEngine
from fno_ai_paper_trading.backtest.result import BacktestResult
from fno_ai_paper_trading.models.market import MarketPrice
from fno_ai_paper_trading.research.benchmark import BenchmarkResult, buy_and_hold
from fno_ai_paper_trading.research.metrics import PerformanceMetrics, compute_metrics
from fno_ai_paper_trading.strategies.base import Strategy

FRAMEWORK_VERSION = "research-1.0"


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _period(bars: list[MarketPrice]) -> tuple[date | None, date | None]:
    if not bars:
        return None, None
    return bars[0].timestamp.date(), bars[-1].timestamp.date()


@dataclass(frozen=True)
class ExperimentConfig:
    """Immutable provenance for one experiment."""

    name: str
    strategy_name: str
    strategy_params: Mapping[str, Any]
    dataset_name: str
    start_date: date | None = None
    end_date: date | None = None
    initial_capital: Decimal = Decimal("0")
    cost_assumptions: str = ""
    slippage_assumptions: str = ""
    quantity: int = 0
    bars_per_year: int = 252
    risk_free_rate: Decimal = Decimal("0")
    created_at: str = field(default_factory=_now_iso)
    framework_version: str = FRAMEWORK_VERSION

    def canonical_json(self) -> str:
        def _default(value: Any) -> str:
            if isinstance(value, Decimal):
                return str(value)
            if isinstance(value, (date, datetime)):
                return value.isoformat()
            return value

        payload = {
            "name": self.name,
            "strategy_name": self.strategy_name,
            "strategy_params": dict(self.strategy_params),
            "dataset_name": self.dataset_name,
            "start_date": self.start_date.isoformat() if self.start_date else None,
            "end_date": self.end_date.isoformat() if self.end_date else None,
            "initial_capital": str(self.initial_capital),
            "cost_assumptions": self.cost_assumptions,
            "slippage_assumptions": self.slippage_assumptions,
            "quantity": self.quantity,
            "bars_per_year": self.bars_per_year,
            "risk_free_rate": str(self.risk_free_rate),
            "framework_version": self.framework_version,
        }
        return json.dumps(payload, sort_keys=True, default=_default)

    @property
    def config_hash(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class ExperimentResult:
    """One fully executed experiment: config + metrics + benchmark."""

    config: ExperimentConfig
    result: BacktestResult
    metrics: PerformanceMetrics
    benchmark: BenchmarkResult | None = None

    @property
    def config_hash(self) -> str:
        return self.config.config_hash


def run_experiment(
    bars: list[MarketPrice],
    strategy: Strategy,
    backtest_config: BacktestConfig,
    *,
    name: str,
    strategy_params: Mapping[str, Any],
    dataset_name: str,
    cost_assumptions: str = "",
    slippage_assumptions: str = "",
    benchmark_quantity: int | None = None,
    bars_per_year: int = 252,
    risk_free_rate: Decimal = Decimal("0"),
    engine: BacktestEngine | None = None,
) -> ExperimentResult:
    """Run one experiment and compute metrics (+ an optional benchmark)."""
    start, end = _period(bars)
    config = ExperimentConfig(
        name=name,
        strategy_name=strategy.name,
        strategy_params=dict(strategy_params),
        dataset_name=dataset_name,
        start_date=start,
        end_date=end,
        initial_capital=backtest_config.initial_capital,
        cost_assumptions=cost_assumptions,
        slippage_assumptions=slippage_assumptions,
        quantity=backtest_config.quantity,
        bars_per_year=bars_per_year,
        risk_free_rate=risk_free_rate,
    )
    engine = engine or BacktestEngine()
    result = engine.run(bars, strategy, backtest_config)
    metrics = compute_metrics(
        result,
        bars_per_year=bars_per_year,
        risk_free_rate=risk_free_rate,
    )

    benchmark: BenchmarkResult | None = None
    if benchmark_quantity is not None and bars:
        try:
            benchmark = buy_and_hold(
                bars,
                backtest_config.initial_capital,
                benchmark_quantity,
                bars_per_year=bars_per_year,
            )
        except ValueError:
            benchmark = None
    return ExperimentResult(config=config, result=result, metrics=metrics, benchmark=benchmark)