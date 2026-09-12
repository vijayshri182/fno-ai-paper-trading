"""Performance evaluation of a frozen champion strategy over a continuous bar series.

A reproducible, public audit of how the strategy under test would have *paper
traded* over an actual bar history using exactly the deterministic engine,
broker, portfolio and risk controls that live paper trading uses. Everything is
decision-time: the engine (and the fast signal provider in ``fast_signal``)
only ever exposes ``bars[:i+1]`` at bar ``i``, and regime labels use
``RegimeDetector.detect_prefix`` at the entry bar, so no metric can read a
future bar or a future regime.

This module is evaluation only: it never places an order, never touches a live
broker, and never mutates strategy code.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Callable, Sequence

from fno_ai_paper_trading.backtest.config import BacktestConfig
from fno_ai_paper_trading.backtest.engine import BacktestEngine
from fno_ai_paper_trading.backtest.result import BacktestResult
from fno_ai_paper_trading.evaluation.fast_signal import moving_average_cross_signals
from fno_ai_paper_trading.learning.capture import pair_round_trips
from fno_ai_paper_trading.models.enums import OrderSide
from fno_ai_paper_trading.models.market import MarketPrice
from fno_ai_paper_trading.models.position import Trade
from fno_ai_paper_trading.regime.detector import RegimeDetector
from fno_ai_paper_trading.research.benchmark import BenchmarkResult, buy_and_hold
from fno_ai_paper_trading.research.metrics import PerformanceMetrics, compute_metrics
from fno_ai_paper_trading.research.split import SplitScheme, split_bars
from fno_ai_paper_trading.research.walkforward import WalkForwardStep, plan_windows
from fno_ai_paper_trading.strategies.base import SignalResult
from fno_ai_paper_trading.strategies.moving_average_cross import MovingAverageCrossStrategy
from fno_ai_paper_trading.utils.functions import positive_int


def _bars_per_day(bars: Sequence[MarketPrice]) -> int:
    if not bars:
        return 1
    counts: dict[str, int] = {}
    for bar in bars:
        key = bar.timestamp.date().isoformat()
        counts[key] = counts.get(key, 0) + 1
    sorted_counts = sorted(counts.values())
    median = sorted_counts[len(sorted_counts) // 2]
    return positive_int(max(1, median), "bars_per_day")


def bars_per_year_for(bars: Sequence[MarketPrice], per_year: int = 252) -> int:
    """Annualization cadence for a bar series (median bars per day * per_year)."""
    return positive_int(_bars_per_day(bars) * per_year, "bars_per_year")


def _timestamp_index_map(bars: Sequence[MarketPrice]) -> dict[str, int]:
    mapping: dict[str, int] = {}
    for index, bar in enumerate(bars):
        mapping[bar.timestamp.isoformat()] = index
    return mapping


@dataclass(frozen=True)
class RoundTrip:
    """One completed entry/exit pair with decision-time context."""

    index: int
    entry_trade: Trade
    exit_trade: Trade
    entry_bar_index: int
    exit_bar_index: int
    entry_regime: str | None
    exit_regime: str | None
    holding_bars: int
    price_pnl: Decimal
    commission: Decimal
    net_pnl: Decimal


@dataclass(frozen=True)
class ContinuousResult:
    """Outcome of one continuous strategy replay plus derived analytics."""

    strategy_name: str
    bars: list[MarketPrice]
    bars_per_year: int
    config: BacktestConfig
    result: BacktestResult
    metrics: PerformanceMetrics
    signals: list[SignalResult]
    round_trips: list[RoundTrip]
    open_trades: int
    reconciled_pnl: Decimal | None
    reconciliation_ok: bool
    min_equity: Decimal

    @property
    def initial_capital(self) -> Decimal:
        return self.config.initial_capital

    def summary_dict(self) -> dict[str, object]:
        m = self.metrics
        return {
            "strategy_name": self.strategy_name,
            "bars": len(self.bars),
            "bars_per_year": self.bars_per_year,
            "start": self.bars[0].timestamp.isoformat(),
            "end": self.bars[-1].timestamp.isoformat(),
            "initial_capital": str(m.initial_capital),
            "final_equity": str(m.final_equity),
            "net_pnl": str(m.net_pnl),
            "net_return_pct": str(m.net_return_pct),
            "cagr_pct": str(m.cagr_pct) if m.cagr_pct is not None else None,
            "max_drawdown": str(m.max_drawdown),
            "max_drawdown_pct": str(m.max_drawdown_pct),
            "drawdown_duration_bars": m.drawdown_duration_bars,
            "num_trades": m.num_trades,
            "winning_trades": m.winning_trades,
            "losing_trades": m.losing_trades,
            "win_rate": str(m.win_rate),
            "profit_factor": str(m.profit_factor),
            "expectancy": str(m.expectancy) if m.expectancy is not None else None,
            "total_commission": str(m.total_commission),
            "slippage_cost": str(m.slippage_cost),
            "transaction_costs": str(m.transaction_costs),
            "gross_profit": str(m.gross_profit),
            "gross_loss": str(m.gross_loss),
            "annualized_volatility": str(m.annualized_volatility)
            if m.annualized_volatility is not None
            else None,
            "sharpe_ratio": str(m.sharpe_ratio) if m.sharpe_ratio is not None else None,
            "sortino_ratio": str(m.sortino_ratio) if m.sortino_ratio is not None else None,
            "exposure_pct": str(m.exposure_pct),
            "min_equity": str(self.min_equity),
            "round_trips": len(self.round_trips),
            "open_trades": self.open_trades,
            "reconciled_pnl": str(self.reconciled_pnl)
            if self.reconciled_pnl is not None
            else None,
            "reconciliation_ok": self.reconciliation_ok,
        }


def _regime_label_between(
    detector: RegimeDetector,
    bars: Sequence[MarketPrice],
    index_map: dict[str, int],
    timestamp,
) -> tuple[int | None, str | None]:
    index = index_map.get(timestamp.isoformat())
    if index is None:
        return None, None
    regime = detector.detect_prefix(bars, index)
    return index, (regime.label if regime is not None else None)


def _build_round_trips(
    bars: Sequence[MarketPrice],
    trades: Sequence[Trade],
    detector: RegimeDetector,
) -> tuple[list[RoundTrip], int]:
    paired, open_count = pair_round_trips(list(trades))
    index_map = _timestamp_index_map(bars)
    trips: list[RoundTrip] = []
    for ordinal, (entry, exit_) in enumerate(paired):
        entry_index, entry_regime = _regime_label_between(
            detector, bars, index_map, entry.executed_at
        )
        exit_index, exit_regime = _regime_label_between(
            detector, bars, index_map, exit_.executed_at
        )
        price_pnl = exit_.realized_pnl
        commission = entry.commission + exit_.commission
        net_pnl = price_pnl - commission
        holding_bars = (exit_index - entry_index) if (exit_index and entry_index) else 0
        trips.append(
            RoundTrip(
                index=ordinal,
                entry_trade=entry,
                exit_trade=exit_,
                entry_bar_index=entry_index if entry_index is not None else -1,
                exit_bar_index=exit_index if exit_index is not None else -1,
                entry_regime=entry_regime,
                exit_regime=exit_regime,
                holding_bars=holding_bars,
                price_pnl=price_pnl,
                commission=commission,
                net_pnl=net_pnl,
            )
        )
    return trips, open_count


def evaluate_continuous(
    bars: Sequence[MarketPrice],
    *,
    fast: int = 5,
    slow: int = 21,
    config: BacktestConfig | None = None,
    strategy_name: str = "moving_average_cross",
    engine: BacktestEngine | None = None,
    detector: RegimeDetector | None = None,
) -> ContinuousResult:
    """Replay a frozen MA-cross strategy over ``bars`` in one continuous run.

    The full series is replayed in a *single* engine session so the strategy and
    portfolio state persist across days, exactly like a live paper session — no
    per-day portfolio resets. Signals are precomputed with ``fast_signal`` and
    fed to the engine, preserving the no-look-ahead contract at O(n) cost.
    """
    engine = engine or BacktestEngine()
    config = config or BacktestConfig()
    detector = detector or RegimeDetector(fast=fast, slow=slow)

    sequence = list(bars)
    signals = moving_average_cross_signals(sequence, fast=fast, slow=slow)
    strategy = MovingAverageCrossStrategy(fast=fast, slow=slow)
    result = engine.run(sequence, strategy, config, signals=signals)

    bpy = bars_per_year_for(sequence)
    metrics = compute_metrics(result, bars_per_year=bpy)

    trips, open_count = _build_round_trips(sequence, result.trades, detector)
    if open_count == 0 and trips:
        reconciled = sum((trip.net_pnl for trip in trips), Decimal("0"))
        reconciliation_ok = reconciled == result.total_pnl
    else:
        reconciled = None
        reconciliation_ok = False

    min_equity = min((point.equity for point in result.equity_curve), default=Decimal("0"))

    return ContinuousResult(
        strategy_name=strategy_name,
        bars=sequence,
        bars_per_year=bpy,
        config=config,
        result=result,
        metrics=metrics,
        signals=signals,
        round_trips=trips,
        open_trades=open_count,
        reconciled_pnl=reconciled,
        reconciliation_ok=reconciliation_ok,
        min_equity=min_equity,
    )


# ---------------------------------------------------------------------------
# Benchmark
# ---------------------------------------------------------------------------


def benchmark_over(
    bars: Sequence[MarketPrice],
    initial_capital: Decimal,
    *,
    name: str = "buy_and_hold",
) -> BenchmarkResult:
    """Fully-funded buy-and-hold over ``bars`` (gross of costs), largest lot that fits."""
    sequence = list(bars)
    entry = sequence[0].close
    multiplier = sequence[0].instrument.multiplier
    unit = entry * multiplier
    quantity = max(1, int(initial_capital // unit))
    return buy_and_hold(
        sequence,
        initial_capital,
        quantity,
        name=name,
        bars_per_year=bars_per_year_for(sequence),
    )


def index_price_return(bars: Sequence[MarketPrice]) -> dict[str, str]:
    """Gross close-to-close price return of ``bars`` (no costs)."""
    first = bars[0].close
    last = bars[-1].close
    return {
        "entry_price": str(first),
        "final_price": str(last),
        "return_pct": str((last - first) / first * Decimal("100")),
    }


# ---------------------------------------------------------------------------
# Time aggregation
# ---------------------------------------------------------------------------


def _period_key(timestamp, granularity: str) -> str:
    if granularity == "year":
        return str(timestamp.year)
    return f"{timestamp.year}-{timestamp.month:02d}"


def aggregate_by_period(
    round_trips: Sequence[RoundTrip],
    *,
    granularity: str = "year",
) -> list[dict[str, object]]:
    """Net P&L and trade counts grouped by the exit time's period."""
    rows: dict[str, dict[str, object]] = {}
    for trip in round_trips:
        key = _period_key(trip.exit_trade.executed_at, granularity)
        row = rows.setdefault(
            key,
            {
                "period": key,
                "num_trades": 0,
                "winning": 0,
                "losing": 0,
                "net_pnl": Decimal("0"),
                "commission": Decimal("0"),
                "price_pnl": Decimal("0"),
            },
        )
        row["num_trades"] = int(row["num_trades"]) + 1
        row["winning"] = int(row["winning"]) + (1 if trip.net_pnl > 0 else 0)
        row["losing"] = int(row["losing"]) + (1 if trip.net_pnl < 0 else 0)
        row["net_pnl"] = Decimal(row["net_pnl"]) + trip.net_pnl
        row["commission"] = Decimal(row["commission"]) + trip.commission
        row["price_pnl"] = Decimal(row["price_pnl"]) + trip.price_pnl

    ordered = sorted(rows.values(), key=lambda r: r["period"])
    result: list[dict[str, object]] = []
    for row in ordered:
        trades = int(row["num_trades"]) or 1
        budget = Decimal(row["net_pnl"]) / Decimal(trades)
        result.append(
            {
                "period": row["period"],
                "num_trades": row["num_trades"],
                "winning": row["winning"],
                "losing": row["losing"],
                "net_pnl": str(row["net_pnl"]),
                "price_pnl": str(row["price_pnl"]),
                "commission": str(row["commission"]),
                "avg_net_pnl": str(budget),
            }
        )
    return result


# ---------------------------------------------------------------------------
# Regime analysis
# ---------------------------------------------------------------------------


def aggregate_by_regime(round_trips: Sequence[RoundTrip]) -> list[dict[str, object]]:
    """Net P&L and trade stats grouped by the decision-time entry regime."""
    rows: dict[str, dict[str, object]] = {}
    for trip in round_trips:
        label = trip.entry_regime or "unknown"
        row = rows.setdefault(
            label,
            {
                "regime": label,
                "num_trades": 0,
                "winning": 0,
                "losing": 0,
                "net_pnl": Decimal("0"),
            },
        )
        row["num_trades"] = int(row["num_trades"]) + 1
        row["winning"] = int(row["winning"]) + (1 if trip.net_pnl > 0 else 0)
        row["losing"] = int(row["losing"]) + (1 if trip.net_pnl < 0 else 0)
        row["net_pnl"] = Decimal(row["net_pnl"]) + trip.net_pnl

    ordered = sorted(rows.values(), key=lambda r: str(r["regime"]))
    result: list[dict[str, object]] = []
    for row in ordered:
        trades = int(row["num_trades"]) or 1
        wins = int(row["winning"])
        result.append(
            {
                "regime": row["regime"],
                "num_trades": row["num_trades"],
                "winning": row["winning"],
                "losing": row["losing"],
                "win_rate_pct": str(Decimal(wins) / Decimal(trades) * Decimal("100")),
                "net_pnl": str(row["net_pnl"]),
                "avg_net_pnl": str(Decimal(row["net_pnl"]) / Decimal(trades)),
            }
        )
    return result


# ---------------------------------------------------------------------------
# Period equity from the curve
# ---------------------------------------------------------------------------


def period_equities(
    curve_points,
    granularity: str = "year",
) -> list[dict[str, object]]:
    """Last reported equity per calendar period across the equity curve."""
    rows: dict[str, Decimal] = {}
    for point in curve_points:
        key = _period_key(point.timestamp, granularity)
        rows[key] = point.equity
    return [
        {"period": key, "end_equity": str(value)} for key, value in sorted(rows.items())
    ]


# ---------------------------------------------------------------------------
# Splits and walk-forward
# ---------------------------------------------------------------------------


def evaluate_splits(
    bars: Sequence[MarketPrice],
    *,
    fast: int = 5,
    slow: int = 21,
    config: BacktestConfig | None = None,
    scheme: SplitScheme | None = None,
) -> list[dict[str, object]]:
    """Evaluate each chronological train/validation/test segment in isolation."""
    config = config or BacktestConfig()
    split = split_bars(list(bars), scheme=scheme)
    segments = [
        ("train", split.train, split.train_range),
        ("validation", split.validation, split.validation_range),
        ("test", split.test, split.test_range),
    ]
    outputs: list[dict[str, object]] = []
    for name, segment, span in segments:
        if not segment:
            outputs.append({"segment": name, "bars": 0, "skipped": True})
            continue
        run = evaluate_continuous(segment, fast=fast, slow=slow, config=config)
        m = run.metrics
        outputs.append(
            {
                "segment": name,
                "start": span[0].isoformat(),
                "end": span[1].isoformat(),
                "bars": len(segment),
                "net_return_pct": str(m.net_return_pct),
                "net_pnl": str(m.net_pnl),
                "num_trades": m.num_trades,
                "winning": m.winning_trades,
                "losing": m.losing_trades,
                "win_rate_pct": str(m.win_rate),
                "max_drawdown_pct": str(m.max_drawdown_pct),
                "transaction_costs": str(m.transaction_costs),
                "final_equity": str(m.final_equity),
                "open_trades": run.open_trades,
                "reconciliation_ok": run.reconciliation_ok,
            }
        )
    return outputs


def walk_forward(
    bars: Sequence[MarketPrice],
    *,
    train_size: int,
    test_size: int,
    step: int | None = None,
    fast: int = 5,
    slow: int = 21,
    config: BacktestConfig | None = None,
    engine: BacktestEngine | None = None,
) -> dict[str, object]:
    """Non-overlapping walk-forward over the fixed champion (no parameter refit).

    The strategy is the same frozen MA(5,21) in every window — ``train_size``
    does not change it. The test windows are therefore a stability check across
    time, not evidence of parameter-fitting on the training walk. Signals are
    computed per test window, so a test window never sees a later bar.
    """
    sequence = list(bars)
    for size in (train_size, test_size):
        positive_int(size, "size")
    step = test_size if step is None else step
    if step < test_size:
        raise ValueError("walk-forward step must be >= test_size")
    config = config or BacktestConfig()
    engine = engine or BacktestEngine()

    windows: list[WalkForwardStep] = plan_windows(len(sequence), train_size, test_size, step)
    steps: list[dict[str, object]] = []
    cum_factor = Decimal("1")
    total_trades = 0
    total_pnl = Decimal("0")
    total_costs = Decimal("0")
    for window in windows:
        test_bars = sequence[window.test_start : window.test_end]
        run = evaluate_continuous(test_bars, fast=fast, slow=slow, config=config, engine=engine)
        m = run.metrics
        total_trades += m.num_trades
        total_pnl += m.net_pnl
        total_costs += m.transaction_costs
        if m.net_return_pct.is_finite():
            cum_factor *= Decimal("1") + m.net_return_pct / Decimal("100")
        steps.append(
            {
                "window": window.index,
                "train_range": f"{sequence[window.train_start].timestamp.isoformat()}"
                f"..{sequence[window.train_end - 1].timestamp.isoformat()}",
                "test_range": f"{sequence[window.test_start].timestamp.isoformat()}"
                f"..{sequence[window.test_end - 1].timestamp.isoformat()}",
                "test_bars": len(test_bars),
                "num_trades": m.num_trades,
                "winning": m.winning_trades,
                "losing": m.losing_trades,
                "net_return_pct": str(m.net_return_pct),
                "net_pnl": str(m.net_pnl),
                "max_drawdown_pct": str(m.max_drawdown_pct),
            }
        )

    combined_return_pct = (cum_factor - Decimal("1")) * Decimal("100")
    return {
        "train_size": train_size,
        "test_size": test_size,
        "step": step,
        "num_windows": len(windows),
        "combined_return_pct": str(combined_return_pct),
        "total_trades": total_trades,
        "total_pnl": str(total_pnl),
        "total_transaction_costs": str(total_costs),
        "steps": steps,
    }


# ---------------------------------------------------------------------------
# Cost sensitivity
# ---------------------------------------------------------------------------


def cost_sensitivity(
    bars: Sequence[MarketPrice],
    *,
    fast: int = 5,
    slow: int = 21,
    base_config: BacktestConfig | None = None,
) -> list[dict[str, object]]:
    """Bounded, explicit cost scenarios around the configured friction.

    Strategy decisions do not read costs, so entry/exit times are identical
    across scenarios (unless the daily-loss risk gate trips); the table isolates
    the effect of friction on net P&L and drawdown.
    """
    base_config = base_config or BacktestConfig()
    scenarios: dict[str, tuple[Decimal, Decimal]] = {
        "zero_cost": (Decimal("0"), Decimal("0")),
        "low_cost": (base_config.commission_rate / 2, base_config.slippage_rate / 2),
        "base": (base_config.commission_rate, base_config.slippage_rate),
        "high_cost": (base_config.commission_rate * 2, base_config.slippage_rate * 2),
    }
    results: list[dict[str, object]] = []
    base_trades: int | None = None
    for name, (commission, slippage) in scenarios.items():
        config = BacktestConfig(
            initial_capital=base_config.initial_capital,
            quantity=base_config.quantity,
            commission_rate=commission,
            commission_fixed=base_config.commission_fixed,
            slippage_rate=slippage,
            cost_schedule=base_config.cost_schedule,
            execution=base_config.execution,
            enable_risk_manager=base_config.enable_risk_manager,
            max_position_quantity=base_config.max_position_quantity,
            max_order_notional=base_config.max_order_notional,
            max_daily_loss=base_config.max_daily_loss,
            enable_stop_loss=base_config.enable_stop_loss,
            stop_loss_pct=base_config.stop_loss_pct,
        )
        run = evaluate_continuous(bars, fast=fast, slow=slow, config=config)
        m = run.metrics
        if base_trades is None:
            base_trades = m.num_trades
        results.append(
            {
                "scenario": name,
                "commission_rate": str(commission),
                "slippage_rate": str(slippage),
                "num_trades": m.num_trades,
                "net_pnl": str(m.net_pnl),
                "net_return_pct": str(m.net_return_pct),
                "transaction_costs": str(m.transaction_costs),
                "max_drawdown_pct": str(m.max_drawdown_pct),
                "final_equity": str(m.final_equity),
                "same_trades_as_base": m.num_trades == (base_trades or -1),
            }
        )
    return results