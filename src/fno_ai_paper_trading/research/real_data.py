"""Real-data research pipeline for the NIFTY 50 index (Upstox, read-only).

This module wires the existing data, backtest and research packages into a
single reproducible real-data study. It deliberately does **not** fetch market
data itself — that is the caller's responsibility (usually ``scripts/
research_real_data.py``) — so tests stay offline and deterministic.

Design choices
--------------
* One instrument, one interval, one baseline strategy: NIFTY 50 daily bars
  replayed through the existing ``MovingAverageCrossStrategy`` with its default
  fast=5 / slow=21 parameters.
* Costs and execution assumptions are the existing illustrative research
  schedule. For a broad index like NIFTY 50 these are **not** an exact F&O cost
  match; the report documents that limitation rather than hiding it.
* Out-of-sample honesty: the dataset is split 60/20/20. Parameter sensitivity is
  run only on the in-sample (train+validation) bars; the OOS segment and the
  walk-forward windows are evaluated with the locked baseline parameters.
* Sensitivity is explicitly enumerated (not optimized) and limited to a small
  grid so the question stays "is the baseline robust?", not "which parameters
  maximize return?".
* Regime analysis uses simple date-based slices that correspond to broadly
  recognised market periods (pre-COVID, COVID, post-COVID). Periods that fall
  outside the available data are skipped.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Mapping

from fno_ai_paper_trading.backtest.config import BacktestConfig
from fno_ai_paper_trading.backtest.engine import BacktestEngine
from fno_ai_paper_trading.backtest.result import BacktestResult
from fno_ai_paper_trading.data.dataset_store import StoredDataset
from fno_ai_paper_trading.data.validation import ValidationReport, validate_bars, validation_to_dict
from fno_ai_paper_trading.models.market import MarketPrice
from fno_ai_paper_trading.research.benchmark import BenchmarkResult, buy_and_hold
from fno_ai_paper_trading.research.costs import IndiaCostSchedule
from fno_ai_paper_trading.research.execution import ExecutionAssumptions
from fno_ai_paper_trading.research.experiment import ExperimentResult, run_dataset_experiment, run_experiment
from fno_ai_paper_trading.research.sensitivity import SensitivityResult, run_parameter_sensitivity
from fno_ai_paper_trading.research.split import split_bars
from fno_ai_paper_trading.research.walkforward import WalkForwardResult, run_walk_forward
from fno_ai_paper_trading.strategies import MovingAverageCrossStrategy

# Default research period covers ~10 years of NSE daily data while staying well
# inside Upstox's documented daily-candle depth (days since Jan 2000).
DEFAULT_START_DATE = date(2015, 1, 1)
DEFAULT_END_DATE = date(2024, 12, 31)

# Bars-per-year for daily data.
DAILY_BARS_PER_YEAR = 252

# Locked baseline MA-cross parameters (not tuned during this milestone).
DEFAULT_STRATEGY_PARAMS: dict[str, int] = {"fast": 5, "slow": 21}


@dataclass(frozen=True)
class DatasetStatistics:
    """Descriptive statistics computed before any backtest runs."""

    observations: int
    start: date
    end: date
    open_min: Decimal
    open_max: Decimal
    high_min: Decimal
    high_max: Decimal
    low_min: Decimal
    low_max: Decimal
    close_min: Decimal
    close_max: Decimal
    avg_volume: Decimal
    avg_daily_return_pct: Decimal | None
    daily_return_volatility_pct: Decimal | None
    min_daily_return_pct: Decimal | None
    max_daily_return_pct: Decimal | None
    largest_daily_return_abs_pct: Decimal | None
    missing_calendar_dates: int
    duplicate_timestamps: int
    invalid_ohlc_bars: int


@dataclass(frozen=True)
class RegimeResult:
    """Backtest result for one named market segment."""

    name: str
    bars: list[MarketPrice]
    backtest: BacktestResult
    benchmark: BenchmarkResult


@dataclass(frozen=True)
class RealDataResearchResult:
    """Complete reproducible result of one real-data research run."""

    dataset_hash: str
    validation: dict[str, Any]
    stats: DatasetStatistics
    strategy_params: Mapping[str, int]
    full_experiment: ExperimentResult
    is_experiment: ExperimentResult
    oos_experiment: ExperimentResult
    sensitivity: SensitivityResult
    walk_forward: WalkForwardResult
    regimes: list[RegimeResult] = field(default_factory=list)
    cost_assumptions: str = "nse_fo_illustrative"
    slippage_assumptions: str = "execution total_adverse_rate=0.001"
    initial_capital: Decimal = Decimal("250000")
    quantity: int = 5
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))


def _research_config(
    *,
    initial_capital: Decimal = Decimal("250000"),
    quantity: int = 5,
) -> BacktestConfig:
    """Backtest assumptions used for every experiment in this pipeline."""
    return BacktestConfig(
        initial_capital=initial_capital,
        quantity=quantity,
        cost_schedule=IndiaCostSchedule.nse_fo_illustrative(),
        execution=ExecutionAssumptions(
            slippage_rate=Decimal("0.0005"),
            half_spread_rate=Decimal("0.0002"),
            impact_rate=Decimal("0.0003"),
        ),
    )


def _median(values: list[Decimal]) -> Decimal:
    """Median of a non-empty list (population view, deterministic)."""
    sorted_values = sorted(values)
    n = len(sorted_values)
    if n % 2 == 1:
        return sorted_values[n // 2]
    return (sorted_values[n // 2 - 1] + sorted_values[n // 2]) / Decimal("2")


def _percent_return_series(bars: list[MarketPrice]) -> list[Decimal]:
    """Close-to-close percentage returns for a chronological series."""
    returns: list[Decimal] = []
    for prev, cur in zip(bars, bars[1:]):
        if prev.close == 0:
            continue
        returns.append((cur.close - prev.close) / prev.close * Decimal("100"))
    return returns


def compute_dataset_stats(bars: list[MarketPrice], report: ValidationReport) -> DatasetStatistics:
    """Compute descriptive statistics for a validated daily OHLCV dataset."""
    if not bars:
        raise ValueError("compute_dataset_stats requires at least one bar")

    starts = [bar.timestamp.date() for bar in bars]
    start, end = min(starts), max(starts)
    observations = len(bars)

    duplicate_timestamps = observations - len({bar.timestamp for bar in bars})
    invalid_ohlc_bars = sum(
        1
        for issue in report.issues
        if issue.level == "error" and issue.index is not None and "timestamp" not in issue.message
    )

    # Calendar days between first and last bar inclusive, minus observed distinct
    # calendar days. Weekends/holidays show up here as expected "missing" days.
    expected_calendar_days = (end - start).days + 1
    missing_calendar_dates = expected_calendar_days - len({s for s in starts})

    returns_pct = _percent_return_series(bars)
    avg_return = sum(returns_pct, Decimal("0")) / len(returns_pct) if returns_pct else None
    vol = None
    min_ret = None
    max_ret = None
    largest_abs = None
    if returns_pct:
        min_ret = min(returns_pct)
        max_ret = max(returns_pct)
        largest_abs = max(abs(min_ret), abs(max_ret))
        if len(returns_pct) >= 2:
            mean = avg_return
            variance = sum((r - mean) ** 2 for r in returns_pct) / len(returns_pct)
            if variance > 0:
                vol = variance.sqrt()

    return DatasetStatistics(
        observations=observations,
        start=start,
        end=end,
        open_min=min(bar.open for bar in bars),
        open_max=max(bar.open for bar in bars),
        high_min=min(bar.high for bar in bars),
        high_max=max(bar.high for bar in bars),
        low_min=min(bar.low for bar in bars),
        low_max=max(bar.low for bar in bars),
        close_min=min(bar.close for bar in bars),
        close_max=max(bar.close for bar in bars),
        avg_volume=sum(Decimal(bar.volume) for bar in bars) / Decimal(observations),
        avg_daily_return_pct=avg_return,
        daily_return_volatility_pct=vol,
        min_daily_return_pct=min_ret,
        max_daily_return_pct=max_ret,
        largest_daily_return_abs_pct=largest_abs,
        missing_calendar_dates=missing_calendar_dates,
        duplicate_timestamps=duplicate_timestamps,
        invalid_ohlc_bars=invalid_ohlc_bars,
    )


def _window_sizes(n_bars: int) -> tuple[int, int, int]:
    """Choose sensible daily walk-forward window sizes for ``n_bars``."""
    if n_bars < 90:
        # Tiny datasets (smoke tests) get small windows so the pipeline still runs.
        test_size = max(10, n_bars // 6)
        train_size = max(30, n_bars // 3)
        step = test_size
    elif n_bars < DAILY_BARS_PER_YEAR * 3:
        test_size = max(20, n_bars // 10)
        train_size = max(50, n_bars // 5)
        step = test_size
    else:
        # ~2-year train, ~1-year test, non-overlapping advance for daily data.
        train_size = DAILY_BARS_PER_YEAR * 2
        test_size = DAILY_BARS_PER_YEAR
        step = test_size
    return train_size, test_size, step


def evaluate_regimes(
    bars: list[MarketPrice],
    config: BacktestConfig,
    strategy_params: Mapping[str, int],
) -> list[RegimeResult]:
    """Run the baseline strategy on named date-based market segments.

    Periods that do not overlap the available data are skipped rather than
    fabricated.
    """
    if not bars:
        return []

    data_start = min(bar.timestamp.date() for bar in bars)
    data_end = max(bar.timestamp.date() for bar in bars)

    slices = [
        ("Pre-COVID (2015-2019)", date(2015, 1, 1), date(2019, 12, 31)),
        ("COVID era (2020-2021)", date(2020, 1, 1), date(2021, 12, 31)),
        ("Post-COVID (2022-2024)", date(2022, 1, 1), date(2024, 12, 31)),
    ]

    engine = BacktestEngine()
    strategy = MovingAverageCrossStrategy(**strategy_params)
    results: list[RegimeResult] = []
    for name, slice_start, slice_end in slices:
        if slice_end < data_start or slice_start > data_end:
            continue
        segment = [
            bar
            for bar in bars
            if slice_start <= bar.timestamp.date() <= slice_end
        ]
        if len(segment) < strategy.slow + 1:
            continue
        backtest = engine.run(segment, strategy, config)
        benchmark = buy_and_hold(segment, config.initial_capital, config.quantity, name=f"buy_and_hold_{name}")
        results.append(RegimeResult(name=name, bars=segment, backtest=backtest, benchmark=benchmark))
    return results


def run_real_data_research(
    dataset: StoredDataset,
    *,
    strategy_params: Mapping[str, int] | None = None,
    initial_capital: Decimal = Decimal("250000"),
    quantity: int = 5,
    train_size: int | None = None,
    test_size: int | None = None,
    step: int | None = None,
) -> RealDataResearchResult:
    """Run the full real-data research pipeline on a persisted dataset.

    The dataset is validated before use. The pipeline returns a fully
    reproducible result including in-sample / out-of-sample experiments,
    walk-forward evaluation, parameter sensitivity on the in-sample data, and
    regime-segment backtests.
    """
    params = dict(strategy_params) if strategy_params is not None else dict(DEFAULT_STRATEGY_PARAMS)
    if "fast" not in params or "slow" not in params:
        raise ValueError("strategy_params must contain 'fast' and 'slow'")

    # Validate once; the full pipeline will raise on validation failure.
    validation_report = validate_bars(dataset.bars, allow_empty=False)
    stats = compute_dataset_stats(dataset.bars, validation_report)

    config = _research_config(initial_capital=initial_capital, quantity=quantity)
    strategy = MovingAverageCrossStrategy(**params)

    # Full-period experiment with benchmark.
    full_experiment = run_dataset_experiment(
        dataset,
        strategy,
        config,
        name="ma_cross_full",
        strategy_params=params,
        cost_assumptions="nse_fo_illustrative",
        slippage_assumptions="execution total_adverse_rate=0.001",
        benchmark_quantity=config.quantity,
        bars_per_year=DAILY_BARS_PER_YEAR,
    )

    # Chronological IS/OOS split.
    split = split_bars(dataset.bars)
    is_bars = split.train + split.validation
    oos_bars = split.test

    if not oos_bars:
        raise ValueError("dataset is too short for an out-of-sample split")

    is_experiment = run_experiment(
        is_bars,
        strategy,
        config,
        name="ma_cross_is",
        strategy_params=params,
        dataset_name=f"{dataset.path.stem}_is",
        cost_assumptions="nse_fo_illustrative",
        slippage_assumptions="execution total_adverse_rate=0.001",
        benchmark_quantity=config.quantity,
        bars_per_year=DAILY_BARS_PER_YEAR,
    )
    oos_experiment = run_experiment(
        oos_bars,
        strategy,
        config,
        name="ma_cross_oos",
        strategy_params=params,
        dataset_name=f"{dataset.path.stem}_oos",
        cost_assumptions="nse_fo_illustrative",
        slippage_assumptions="execution total_adverse_rate=0.001",
        benchmark_quantity=config.quantity,
        bars_per_year=DAILY_BARS_PER_YEAR,
    )

    # Sensitivity is run only on in-sample bars so OOS remains untouched.
    sensitivity = run_parameter_sensitivity(
        is_bars,
        config,
        fast_values=[5, 10, 15],
        slow_values=[21, 50],
    )

    # Walk-forward uses the full series but each test window is strictly OOS.
    n_bars = len(dataset.bars)
    auto_train, auto_test, auto_step = _window_sizes(n_bars)
    walk_forward = run_walk_forward(
        dataset.bars,
        lambda train_bars: MovingAverageCrossStrategy(**params),
        config,
        train_size=train_size if train_size is not None else auto_train,
        test_size=test_size if test_size is not None else auto_test,
        step=step if step is not None else auto_step,
    )

    regimes = evaluate_regimes(dataset.bars, config, params)

    return RealDataResearchResult(
        dataset_hash=dataset.data_hash,
        validation=validation_to_dict(validation_report),
        stats=stats,
        strategy_params=params,
        full_experiment=full_experiment,
        is_experiment=is_experiment,
        oos_experiment=oos_experiment,
        sensitivity=sensitivity,
        walk_forward=walk_forward,
        regimes=regimes,
        cost_assumptions="nse_fo_illustrative",
        slippage_assumptions="execution total_adverse_rate=0.001",
        initial_capital=initial_capital,
        quantity=quantity,
    )
