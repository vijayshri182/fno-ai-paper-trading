"""Strategy research & robustness evaluation framework.

Deterministic, paper-only research tooling built on top of the backtest
harness: configurable Indian cost schedules, execution assumptions, market
regime datasets, in-sample/out-of-sample splits, walk-forward evaluation,
parameter sensitivity, a buy-and-hold benchmark, robust performance metrics,
self-describing experiments, and HTML reporting.

Nothing in this package places orders or requires credentials. All datasets
are synthetic and deterministic; any result shown here is historical/synthetic
evidence only.
"""
from fno_ai_paper_trading.research.benchmark import BenchmarkResult, buy_and_hold
from fno_ai_paper_trading.research.costs import ChargeBreakdown, IndiaCostSchedule
from fno_ai_paper_trading.research.execution import ExecutionAssumptions
from fno_ai_paper_trading.research.experiment import (
    ExperimentConfig,
    ExperimentResult,
    run_experiment,
)
from fno_ai_paper_trading.research.metrics import PerformanceMetrics, compute_metrics
from fno_ai_paper_trading.research.regimes import REGIME_BUILDERS
from fno_ai_paper_trading.research.sensitivity import (
    SensitivityResult,
    SensitivityRow,
    run_parameter_sensitivity,
    validate_ma_pairs,
)
from fno_ai_paper_trading.research.split import Split, SplitScheme, split_bars, split_indices
from fno_ai_paper_trading.research.walkforward import (
    WalkForwardResult,
    WalkForwardStep,
    plan_windows,
    run_walk_forward,
)

__all__ = [
    "BenchmarkResult",
    "buy_and_hold",
    "ChargeBreakdown",
    "IndiaCostSchedule",
    "ExecutionAssumptions",
    "ExperimentConfig",
    "ExperimentResult",
    "run_experiment",
    "PerformanceMetrics",
    "compute_metrics",
    "REGIME_BUILDERS",
    "SensitivityResult",
    "SensitivityRow",
    "run_parameter_sensitivity",
    "validate_ma_pairs",
    "Split",
    "SplitScheme",
    "split_bars",
    "split_indices",
    "WalkForwardResult",
    "WalkForwardStep",
    "plan_windows",
    "run_walk_forward",
]