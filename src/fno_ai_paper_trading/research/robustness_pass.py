"""Algorithm robustness and strengthening research pass (2026-09-28).

Analytical only. This module studies the FROZEN champion ``v1-baseline-ma521``
(``moving_average_cross`` fast=5 / slow=21, strategy_version 1.0.0) over the
approved development-research window (every trading day strictly *before*
``PROTECTED_OOS_START`` == 2025-10-06) using the existing deterministic
evaluation machinery:

* :func:`~fno_ai_paper_trading.evaluation.model_performance.evaluate_continuous`
  for the champion baseline, the pre-declared parameter neighbourhood, and the
  bounded cost/slippage scenarios (single continuous session, default
  ``BacktestConfig`` paper cost model);
* :func:`~fno_ai_paper_trading.evaluation.algorithm_health.compute_trade_metrics`
  over recorded ``TradeRecord`` rows for the health-bucket metric set;
* the per-day walk-forward style replay (``StrategyEngine`` + ``BacktestEngine``,
  one session per trading day) for the repo's frozen catalog challengers only.

Guards (never relaxed here): protected out-of-sample days
``2025-10-06 .. 2026-09-11`` are never loaded or read (dev bars are filtered to
``day < PROTECTED_OOS_START`` up front); no parameter is tuned on out-of-sample
evidence; the champion is never modified; nothing here places an order or
touches a live broker. Every analysis function is pure over its inputs.

Descriptive forensics (regime breakdown, failure-category labels, loss-cluster
statistics) are reported as associations over the single historical sample, in
the same spirit as ``research/iteration012_trade_forensics.py`` — never as
causal claims.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Mapping, Sequence

from fno_ai_paper_trading.backtest.config import BacktestConfig
from fno_ai_paper_trading.backtest.engine import BacktestEngine
from fno_ai_paper_trading.evaluation.algorithm_health import (
    TradeRecord,
    compute_trade_metrics,
)
from fno_ai_paper_trading.evaluation.model_performance import (
    ContinuousResult,
    RoundTrip,
    evaluate_continuous,
)
from fno_ai_paper_trading.learning.capture import pair_round_trips
from fno_ai_paper_trading.models.market import MarketPrice
from fno_ai_paper_trading.options_research.windows import (
    PROTECTED_OOS_START,
    classify_day,
    verify_no_protected_reuse,
)
from fno_ai_paper_trading.research.costs import IndiaCostSchedule
from fno_ai_paper_trading.strategies.engine import StrategyEngine
from fno_ai_paper_trading.strategies.base import Signal as TradeSignal

_ZERO = Decimal("0")

# Champion identity (mirrors scripts/build_daily_performance.py attribution).
STRATEGY_NAME = "moving_average_cross"
STRATEGY_VERSION = "v1-baseline-ma521"
CONFIGURATION_VERSION = "v1-paper-defaults"

# Pre-declared neighbourhood (never optimized): fast 4..7 x slow 19..23 (20
# combinations including the champion 5/21).
NEIGHBOURHOOD_FAST = (4, 5, 6, 7)
NEIGHBOURHOOD_SLOW = (19, 20, 21, 22, 23)
CHAMPION_FAST = 5
CHAMPION_SLOW = 21

# Base paper friction (BacktestConfig defaults).
BASE_COMMISSION_RATE = Decimal("0.0003")
BASE_SLIPPAGE_RATE = Decimal("0.001")

# Pre-declared holding-duration buckets (75 bars per day), matching the
# research convention in research/iteration012_trade_forensics.py.
HOLD_BUCKETS = (
    ("intraday_or_1d", 0, 75),
    ("2d_5d", 76, 375),
    ("6d_15d", 376, 1125),
    ("16d_25d", 1126, 1875),
)
OVER_25D_LABEL = "over_25d"

# Failure-category forensics constants (descriptive, causal bars only).
WHIPSAW_LOOKBACK_BARS = 25  # ~ one trading day
STOP_BAND_TOLERANCE = Decimal("0.95")  # adverse move >= stop_pct * 0.95 counts as a stop hit

MAX_POSITION_QUANTITY = 75
MAX_ORDER_NOTIONAL = Decimal("250000")
MAX_DAILY_LOSS = Decimal("10000")


def research_bars(bars: Sequence[MarketPrice]) -> list[MarketPrice]:
    """Chronological dev bars strictly before the protected OOS start."""
    return [b for b in bars if b.timestamp.date() < PROTECTED_OOS_START]


def research_days(bars: Sequence[MarketPrice]) -> tuple[date, ...]:
    """Distinct trading days in the dev window, chronologically ordered."""
    return tuple(sorted({b.timestamp.date() for b in bars}))


def assert_clean_dev_domain(days: Sequence[date]) -> tuple[str, ...]:
    """Every supplied day must classify as RESEARCH (never touches protected)."""
    protected = verify_no_protected_reuse(tuple(days))
    if protected:
        raise ValueError(
            f"protected-window days must never enter the research pass: {protected}"
        )
    mislabelled = [d for d in days if classify_day(d) != "RESEARCH"]
    if mislabelled:
        raise ValueError(f"days not classified RESEARCH: {mislabelled}")
    return ("RESEARCH",) * len(days)


def trips_to_trade_records(
    trips: Sequence[Mapping[str, Any]],
    *,
    bucket: str,
    strategy_name: str = STRATEGY_NAME,
    algorithm_version: str = STRATEGY_VERSION,
    configuration_version: str = CONFIGURATION_VERSION,
) -> list[TradeRecord]:
    """Convert canonical round-trip rows into health-ledger TradeRecords.

    ``trips`` entries carry the ``trades.csv`` column convention: entry_time,
    exit_time, side, entry_price, exit_price, price_pnl, commission, net_pnl
    (``price_pnl = exit realized``; ``commission`` is entry+exit;
    ``net_pnl = price_pnl - commission``).
    """
    records: list[TradeRecord] = []
    for row in trips:
        records.append(
            TradeRecord(
                bucket=bucket,
                strategy_name=str(strategy_name),
                algorithm_version=str(algorithm_version),
                configuration_version=str(configuration_version),
                entry_time=row["entry_time"],
                exit_time=row["exit_time"],
                side=str(row["side"]),
                entry_price=Decimal(str(row["entry_price"])),
                exit_price=Decimal(str(row["exit_price"])),
                price_pnl=Decimal(str(row["price_pnl"])),
                commission=Decimal(str(row["commission"])),
                net_pnl=Decimal(str(row["net_pnl"])),
                confidence=None,
                exit_reason="",
            )
        )
    return records


def metrics_snapshot(
    trips: Sequence[Mapping[str, Any]],
    *,
    bucket: str,
    today_date: date | None = None,
) -> dict[str, Any]:
    """Health-bucket metric snapshot over round-trip rows (dict form)."""
    records = trips_to_trade_records(trips, bucket=bucket)
    return compute_trade_metrics(records, bucket=bucket, today_date=today_date).to_dict()


def round_trip_rows(run: ContinuousResult) -> list[dict[str, Any]]:
    """Canonical round-trip rows from a continuous replay (trades.csv shape).

    Adds decision-time context used by forensics: ``entry_index`` (bar-index of
    the entry, from the *same* replay) and ``entry_regime``/``exit_regime``.
    """
    rows: list[dict[str, Any]] = []
    for trip in run.round_trips:
        rows.append(
            {
                "entry_time": trip.entry_trade.executed_at,
                "exit_time": trip.exit_trade.executed_at,
                "side": trip.entry_trade.side.value,
                "quantity": str(trip.entry_trade.quantity),
                "entry_price": trip.entry_trade.price,
                "exit_price": trip.exit_trade.price,
                "entry_regime": trip.entry_regime or "",
                "exit_regime": trip.exit_regime or "",
                "holding_bars": str(trip.holding_bars),
                "entry_index": int(trip.entry_bar_index),
                "price_pnl": trip.price_pnl,
                "commission": trip.commission,
                "net_pnl": trip.net_pnl,
            }
        )
    return rows


def champion_run(
    bars: Sequence[MarketPrice],
    *,
    config: BacktestConfig | None = None,
) -> ContinuousResult:
    """Reproduce the champion MA(5,21) on the dev window as a continuous run."""
    config = config or BacktestConfig()
    return evaluate_continuous(
        list(bars), fast=CHAMPION_FAST, slow=CHAMPION_SLOW, config=config
    )


def baseline_from_run(run: ContinuousResult) -> dict[str, Any]:
    """Derive the baseline bundle (summary/metrics/trade rows) from a run."""
    rows = round_trip_rows(run)
    return {
        "strategy": {
            "name": run.strategy_name,
            "fast": CHAMPION_FAST,
            "slow": CHAMPION_SLOW,
        },
        "config": config_words(run.config),
        "continuous": run.summary_dict(),
        "metrics": metrics_snapshot(rows, bucket="backtest"),
        "trade_rows": rows,
        "round_trip_count": len(run.round_trips),
        "open_trades": run.open_trades,
        "reconciliation_ok": run.reconciliation_ok,
    }


def config_words(config: BacktestConfig) -> dict[str, str | None]:
    return {
        "initial_capital": str(config.initial_capital),
        "quantity": str(config.quantity),
        "commission_rate": str(config.commission_rate),
        "commission_fixed": str(config.commission_fixed),
        "slippage_rate": str(config.slippage_rate),
        "cost_schedule": (
            config.cost_schedule.__class__.__name__ if config.cost_schedule else None
        ),
        "enable_risk_manager": str(config.enable_risk_manager),
        "max_position_quantity": str(config.max_position_quantity),
        "max_order_notional": str(config.max_order_notional),
        "max_daily_loss": str(config.max_daily_loss),
        "enable_stop_loss": str(config.enable_stop_loss),
        "stop_loss_pct": str(config.stop_loss_pct),
    }


def _scenario_config(
    commission: Decimal, slippage: Decimal, *, statutory: bool = False
) -> BacktestConfig:
    return BacktestConfig(
        commission_rate=commission,
        commission_fixed=Decimal("0"),
        slippage_rate=slippage,
        cost_schedule=IndiaCostSchedule.nse_fo_illustrative() if statutory else None,
        enable_risk_manager=True,
        max_position_quantity=MAX_POSITION_QUANTITY,
        max_order_notional=MAX_ORDER_NOTIONAL,
        max_daily_loss=MAX_DAILY_LOSS,
        enable_stop_loss=True,
        stop_loss_pct=Decimal("0.02"),
    )


def parameter_neighbourhood(
    bars: Sequence[MarketPrice],
    *,
    config: BacktestConfig | None = None,
) -> dict[str, Any]:
    """Pre-declared MA neighbourhood sweep; never an optimizer."""
    config = config or BacktestConfig()
    rows: list[dict[str, Any]] = []
    champion_expectancy: Decimal | None = None
    champion_key: tuple[int, int] | None = None
    for slow in NEIGHBOURHOOD_SLOW:
        for fast in NEIGHBOURHOOD_FAST:
            is_champion = fast == CHAMPION_FAST and slow == CHAMPION_SLOW
            run = evaluate_continuous(list(bars), fast=fast, slow=slow, config=config)
            m = run.metrics
            rows.append(
                {
                    "fast": fast,
                    "slow": slow,
                    "is_champion": is_champion,
                    "num_trades": m.num_trades,
                    "winning": m.winning_trades,
                    "losing": m.losing_trades,
                    "win_rate_pct": str(m.win_rate),
                    "net_pnl": str(m.net_pnl),
                    "expectancy": str(m.expectancy) if m.expectancy is not None else None,
                    "profit_factor": str(m.profit_factor) if m.profit_factor is not None else None,
                    "max_drawdown_pct": str(m.max_drawdown_pct),
                    "transaction_costs": str(m.transaction_costs),
                }
            )
            if is_champion:
                champion_expectancy = m.expectancy
                champion_key = (fast, slow)
    neighbours_better = None
    stability: dict[str, Any] | None = None
    if champion_expectancy is not None and champion_key is not None:
        neighbours = [
            r for r in rows if not r["is_champion"] and r["expectancy"] is not None
        ]
        neighbours_better = sum(
            1 for r in neighbours if Decimal(str(r["expectancy"])) > champion_expectancy
        )
        if neighbours:
            values = [Decimal(str(r["expectancy"])) for r in neighbours]
            ordered = sorted(values)
            mean = sum(ordered) / Decimal(len(ordered))
            best = ordered[-1]
            best_row = next(r for r in neighbours if Decimal(str(r["expectancy"])) == best)
            stability = {
                "neighbours": len(neighbours),
                "mean": str(mean),
                "median": str(ordered[len(ordered) // 2]),
                "min": str(ordered[0]),
                "max": str(best),
                "best": {"fast": best_row["fast"], "slow": best_row["slow"]},
            }
    return {
        "neighbourhood": {
            "fast_values": list(NEIGHBOURHOOD_FAST),
            "slow_values": list(NEIGHBOURHOOD_SLOW),
            "champion": {"fast": CHAMPION_FAST, "slow": CHAMPION_SLOW},
        },
        "rows": rows,
        "still_best_champion": neighbours_better == 0,
        "neighbours_better_than_champion": neighbours_better,
        "neighbour_expectancy_stability": stability,
        "note": (
            "pre-declared grid ('research_window' only); serially evaluated; "
            "no parameter was fitted and no protected day was read"
        ),
    }


def cost_sensitivity(
    bars: Sequence[MarketPrice],
    *,
    fast: int = CHAMPION_FAST,
    slow: int = CHAMPION_SLOW,
) -> dict[str, Any]:
    """Bounded, explicit cost scenarios around the base paper friction."""
    scenarios = [
        ("zero_cost", Decimal("0"), Decimal("0"), False),
        ("low_cost", BASE_COMMISSION_RATE / 2, BASE_SLIPPAGE_RATE / 2, False),
        ("base", BASE_COMMISSION_RATE, BASE_SLIPPAGE_RATE, False),
        ("high_cost", BASE_COMMISSION_RATE * 2, BASE_SLIPPAGE_RATE * 2, False),
        ("statutory_nse_fo_illustrative", BASE_COMMISSION_RATE, BASE_SLIPPAGE_RATE, True),
    ]
    rows: list[dict[str, Any]] = []
    for name, commission, slippage, statutory in scenarios:
        config = _scenario_config(commission, slippage, statutory=statutory)
        run = evaluate_continuous(list(bars), fast=fast, slow=slow, config=config)
        m = run.metrics
        rows.append(
            {
                "scenario": name,
                "commission_rate": str(commission),
                "slippage_rate": str(slippage),
                "cost_schedule": "nse_fo_illustrative" if statutory else None,
                "num_trades": m.num_trades,
                "net_pnl": str(m.net_pnl),
                "net_return_pct": str(m.net_return_pct),
                "transaction_costs": str(m.transaction_costs),
                "max_drawdown_pct": str(m.max_drawdown_pct),
                "final_equity": str(m.final_equity),
            }
        )
    return {
        "base": {
            "commission_rate": str(BASE_COMMISSION_RATE),
            "slippage_rate": str(BASE_SLIPPAGE_RATE),
        },
        "rows": rows,
    }


def regime_breakdown(trips: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Champion results grouped by the decision-time entry regime label."""
    buckets: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for trip in trips:
        buckets.setdefault(str(trip.get("entry_regime") or ""), []).append(trip)
    rows: list[dict[str, Any]] = []
    for label, group in sorted(buckets.items()):
        nets = [Decimal(str(t["net_pnl"])) for t in group]
        wins = sum(1 for n in nets if n > 0)
        rows.append(
            {
                "regime": label or "UNKNOWN",
                "num_trades": len(group),
                "winning": wins,
                "losing": len(group) - wins,
                "win_rate_pct": str(Decimal(wins) / Decimal(len(group)) * Decimal("100")),
                "net_pnl": str(sum(nets, _ZERO)),
                "avg_net_pnl": str(sum(nets, _ZERO) / Decimal(len(group))),
            }
        )
    return rows


def _adverse_move_pct(trip: Mapping[str, Any]) -> Decimal | None:
    """Adverse price move between entry and exit as a fraction of entry price."""
    entry = Decimal(str(trip["entry_price"]))
    if entry == 0:
        return None
    exit_price = Decimal(str(trip["exit_price"]))
    side = str(trip.get("side") or "").upper()
    if side in ("BUY", "LONG"):
        return (entry - exit_price) / entry
    return (exit_price - entry) / entry


def _signal_value(signal: Any) -> str | None:
    """Normalise a SignalResult/plain object to its UPPER signal name, if any."""
    value = getattr(signal, "signal", None)
    if value is None:
        return None
    raw = getattr(value, "value", value)
    return raw.upper() if isinstance(raw, str) else None


def classify_failures(
    trips: Sequence[Mapping[str, Any]],
    signals: Sequence[Any],
    *,
    stop_pct: Decimal = Decimal("0.02"),
    whipsaw_lookback: int = WHIPSAW_LOOKBACK_BARS,
    stop_band_tolerance: Decimal = STOP_BAND_TOLERANCE,
) -> dict[str, Any]:
    """Mutually-exclusive failure categories using causal bars/signals only.

    Descriptive taxonomy (labels are associations, not causal claims):
      winner            — net_pnl > 0.
      cost_flip         — price_pnl > 0 but net_pnl <= 0 (friction erased the edge).
      whipsaw           — a loss where the opposite crossover fired within
                          ``whipsaw_lookback`` bars after entry (and not an
                          exit-beating price move into the stop band).
      stop_hit          — a loss whose adverse move reached the stop-loss band
                          (>= stop_pct * tolerance) without a whipsaw crossover.
      adverse_hold      — any other loss (held against the trend, no stop/reverse).

    ``signals`` must be the signal list produced by the *same* continuous replay
    whose round trips are classified (bar-index correspondence via
    ``entry_index`` on each row).
    """
    signals_list = list(signals)
    counts: Counter[str] = Counter()
    nets: dict[str, Decimal] = defaultdict(lambda: _ZERO)
    wins: dict[str, int] = defaultdict(int)
    detailed: list[dict[str, Any]] = []
    for trip in trips:
        net = Decimal(str(trip["net_pnl"]))
        if net > _ZERO:
            category = "winner"
        else:
            price_pnl = Decimal(str(trip["price_pnl"]))
            side = str(trip.get("side") or "").upper()
            entry_index = int(trip.get("entry_index", -1))
            reverse_value = (
                TradeSignal.SELL.value if side in ("BUY", "LONG") else TradeSignal.BUY.value
            )
            whipsaw = False
            if entry_index >= 0 and entry_index + 1 < len(signals_list):
                window = signals_list[entry_index + 1: entry_index + 1 + whipsaw_lookback]
                whipsaw = any(_signal_value(s) == reverse_value for s in window)
            adverse = _adverse_move_pct(trip)
            stop_hit = adverse is not None and adverse >= stop_pct * stop_band_tolerance
            if price_pnl > _ZERO:
                category = "cost_flip"
            elif stop_hit:
                category = "stop_hit"
            elif whipsaw:
                category = "whipsaw"
            else:
                category = "adverse_hold"
        counts[category] += 1
        nets[category] += net
        if net > 0:
            wins[category] += 1
        detailed.append({**{k: str(v) for k, v in trip.items()}, "category": category})

    rows: list[dict[str, Any]] = []
    for label in ("winner", "cost_flip", "whipsaw", "stop_hit", "adverse_hold"):
        n = counts[label]
        rows.append(
            {
                "category": label,
                "num_trades": n,
                "winning": wins[label],
                "net_pnl": str(nets[label]),
                "avg_net_pnl": str(nets[label] / Decimal(n)) if n else None,
            }
        )
    return {
        "categories": rows,
        "details_count": len(detailed),
        "note": (
            "mutually exclusive priority cost_flip > stop_hit > whipsaw > adverse_hold; "
            "stop_hit measured against the paper stop-loss band (2%) with a 5% band "
            "tolerance; whipsaw uses the opposite crossover within "
            f"{whipsaw_lookback} bars (~1 trading day); descriptive only"
        ),
    }


def holding_bucket_rows(trips: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Pre-declared holding-duration buckets by round-trip bar count."""
    buckets: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for trip in trips:
        holding_bars = int(trip.get("holding_bars", 0))
        label = None
        for name, lo, hi in HOLD_BUCKETS:
            if lo <= holding_bars <= hi:
                label = name
                break
        buckets[label or OVER_25D_LABEL].append(trip)
    total_net = sum((Decimal(str(t["net_pnl"])) for t in trips), _ZERO)
    rows: list[dict[str, Any]] = []
    for label in ("intraday_or_1d", "2d_5d", "6d_15d", "16d_25d", OVER_25D_LABEL):
        group = buckets[label]
        if not group:
            rows.append({"bucket": label, "num_trades": 0, "skipped": True})
            continue
        nets = [Decimal(str(t["net_pnl"])) for t in group]
        total = sum(nets, _ZERO)
        wins = sum(1 for n in nets if n > 0)
        costs = sum((Decimal(str(t["commission"])) for t in group), _ZERO)
        rows.append(
            {
                "bucket": label,
                "num_trades": len(group),
                "winning": wins,
                "losing": len(group) - wins,
                "win_rate_pct": str(Decimal(wins) / Decimal(len(group)) * Decimal("100")),
                "net_pnl": str(total),
                "avg_net_pnl": str(total / Decimal(len(group))),
                "commission": str(costs),
                "contribution_pct": (
                    str(total / total_net * Decimal("100")) if total_net else None
                ),
            }
        )
    return rows


def loss_cluster_forensics(trips: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Drawdown / loss-cluster statistics over the closed-trade curve."""
    ordered = sorted(trips, key=lambda t: t["entry_time"])
    nets = [Decimal(str(t["net_pnl"])) for t in ordered]
    cumulative: list[Decimal] = []
    running = _ZERO
    for n in nets:
        running += n
        cumulative.append(running)
    peak = _ZERO
    max_dd = _ZERO
    for eq in cumulative:
        peak = max(peak, eq)
        max_dd = max(max_dd, peak - eq)
    runs: list[int] = []
    current = 0
    for n in nets:
        if n < _ZERO:
            current += 1
        else:
            if current:
                runs.append(current)
            current = 0
    if current:
        runs.append(current)
    losses = [n for n in nets if n < _ZERO]
    total_loss = sum(losses, _ZERO)
    sorted_losses = sorted(losses)
    worst_windows: dict[str, str | None] = {}
    for k in (1, 5, 10, 20):
        if len(nets) < k:
            worst_windows[str(k)] = None
            continue
        worst_windows[str(k)] = str(
            min(sum(nets[i: i + k]) for i in range(len(nets) - k + 1))
        )
    loss_concentrations: dict[str, str] = {}
    for k in (10, 25, 50):
        n = max(1, int(len(losses) * k / 100))
        loss_concentrations[f"top_{k}_pct_losses"] = str(sum(sorted_losses[:n]))
    return {
        "trades": len(nets),
        "net_pnl": str(sum(nets, _ZERO)),
        "max_drawdown_amount": str(max_dd),
        "max_drawdown_pct": str(max_dd / peak * Decimal("100")) if peak > _ZERO else None,
        "longest_consecutive_losses": max(runs, default=0),
        "losing_run_lengths": runs,
        "worst_k_trade_windows": worst_windows,
        "worst_single_trade": str(min(nets)) if nets else None,
        "best_single_trade": str(max(nets)) if nets else None,
        "loss_concentration": loss_concentrations,
        "note": (
            "drawdown measured on the closed-trade cumulative net curve (equity "
            "proxy); same simplification as the health monitor"
        ),
    }


def temporal_segments(trips: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Yearly/monthly by exit period plus first/second-half by entry order."""

    def _rows(groups: Mapping[str, list[Mapping[str, Any]]], labels: Sequence[str]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for label in labels:
            group = groups.get(label, [])
            if not group:
                continue
            nets = [Decimal(str(t["net_pnl"])) for t in group]
            total = sum(nets, _ZERO)
            wins = sum(1 for n in nets if n > 0)
            out.append(
                {
                    "period": label,
                    "num_trades": len(group),
                    "winning": wins,
                    "losing": len(group) - wins,
                    "win_rate_pct": str(Decimal(wins) / Decimal(len(group)) * Decimal("100")),
                    "net_pnl": str(total),
                    "avg_net_pnl": str(total / Decimal(len(group))),
                }
            )
        return out

    by_exit_year: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    by_exit_month: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for trip in trips:
        ts = trip["exit_time"]
        by_exit_year[f"{ts.year}"].append(trip)
        by_exit_month[f"{ts.year}-{ts.month:02d}"].append(trip)

    ordered = sorted(trips, key=lambda t: t["entry_time"])
    mid = len(ordered) // 2
    halves: list[dict[str, Any]] = []
    for name, group in (("first_half", ordered[:mid]), ("second_half", ordered[mid:])):
        if not group:
            continue
        nets = [Decimal(str(t["net_pnl"])) for t in group]
        total = sum(nets, _ZERO)
        wins = sum(1 for n in nets if n > 0)
        halves.append(
            {
                "half": name,
                "num_trades": len(group),
                "winning": wins,
                "losing": len(group) - wins,
                "win_rate_pct": str(Decimal(wins) / Decimal(len(group)) * Decimal("100")),
                "net_pnl": str(total),
                "avg_net_pnl": str(total / Decimal(len(group))),
                "first_entry": group[0]["entry_time"].isoformat(),
                "last_entry": group[-1]["entry_time"].isoformat(),
            }
        )
    return {
        "years": _rows(by_exit_year, sorted(by_exit_year)),
        "months": _rows(by_exit_month, sorted(by_exit_month)),
        "first_second_half": halves,
    }


def _day_replay_metrics(
    bars_by_day: Mapping[date, list[MarketPrice]],
    strategy,
    *,
    config: BacktestConfig,
) -> dict[str, Any]:
    """Per-day replay (portfolio resets daily) for one strategy.

    Mirrors the walk-forward challenger machinery: StrategyEngine.evaluate per
    day, BacktestEngine.run per day, FIFO pair_round_trips per day, then totals
    summed across days. Deterministic; no protected day is ever fed in.
    """
    engine = BacktestEngine()
    rows: list[dict[str, Any]] = []
    for day in sorted(bars_by_day):
        bars = bars_by_day[day]
        signals = StrategyEngine(strategy).evaluate(list(bars))
        result = engine.run(list(bars), strategy, config, signals=signals)
        trips, _open = pair_round_trips(result.trades)
        for entry, exit_ in trips:
            commission = entry.commission + exit_.commission
            rows.append(
                {
                    "entry_time": entry.executed_at,
                    "exit_time": exit_.executed_at,
                    "side": entry.side.value,
                    "entry_price": entry.price,
                    "exit_price": exit_.price,
                    "price_pnl": exit_.realized_pnl,
                    "commission": commission,
                    "net_pnl": exit_.realized_pnl - commission,
                }
            )
    return metrics_snapshot(rows, bucket="backtest")


def alternative_strategies(
    bars_by_day: Mapping[date, list[MarketPrice]],
    builders: Sequence[tuple[str, dict[str, Any]]],
    *,
    config: BacktestConfig | None = None,
) -> dict[str, Any]:
    """Limited evaluation of the repo's FROZEN catalog challenger variants.

    Only existing ``walkforward.catalog`` challenger parameter sets are evaluated
    (regime-filtered MA(5,21) variants) — never a tuned zoo. Each is replayed
    day-by-day exactly like the walk-forward engine does, with the same default
    paper cost model, on the dev window only.
    """
    from fno_ai_paper_trading.strategies.regime_filtered import (
        RegimeFilteredMovingAverageCross,
    )

    config = config or BacktestConfig()
    results: list[dict[str, Any]] = []
    for key, params in builders:
        strategy = RegimeFilteredMovingAverageCross(**params)
        metrics = _day_replay_metrics(bars_by_day, strategy, config=config)
        results.append(
            {
                "key": key,
                "strategy_params": params,
                "metrics": metrics,
            }
        )
    return {
        "framework": (
            "per-day replay, portfolio resets daily "
            "(walk-forward challenger semantics)"
        ),
        "candidates": results,
        "note": (
            "all candidates are the repo's frozen walk-forward catalog variants; "
            "no new strategy was introduced and no parameter was fitted"
        ),
    }


def recorded_backtest_cross_check(
    trade_rows: Sequence[Mapping[str, Any]],
    recorded_assessment: Mapping[str, Any],
    *,
    oos_cutoff: datetime = datetime(2026, 1, 1, 0, 0, 0),
) -> dict[str, Any]:
    """Recompute the recorded backtest-bucket metrics and diff vs assessment.

    ``trade_rows`` are the recorded champion rows (trades.csv convention). The
    recorded bucket split (entry_time < 2026-01-01) is reproduced exactly so the
    recomputation can be verified against the persisted health assessment.
    """
    backtest = [r for r in trade_rows if r["entry_time"] < oos_cutoff]
    metrics = metrics_snapshot(backtest, bucket="backtest")

    def _normalize(recorded_key: str, recomputed_value) -> tuple[str, str]:
        recorded_value = recorded_assessment.get(recorded_key)
        if recorded_key in ("total_closed", "winning", "losing"):
            return str(int(recomputed_value)), str(int(recorded_value))
        return str(Decimal(str(recomputed_value))), str(
            Decimal(str(recorded_value))
        )

    diffs: dict[str, Any] = {}
    for key in ("total_closed", "winning", "losing", "net_pnl", "expectancy"):
        recomputed, recorded = _normalize(key, metrics.get(key))
        diffs[key] = {
            "recomputed": recomputed,
            "recorded": recorded,
            "match": recomputed == recorded,
        }
    return {
        "recomputed_backtest_bucket": metrics,
        "recorded_assessment_backtest": {k: recorded_assessment.get(k) for k in ("total_closed", "winning", "losing", "win_rate_pct", "net_pnl", "profit_factor", "avg_win", "avg_loss", "expectancy")},
        "diffs": diffs,
        "match": all(d["match"] for d in diffs.values()),
        "rows_used": len(backtest),
    }


def determinism_probe(
    bars: Sequence[MarketPrice],
    *,
    config: BacktestConfig | None = None,
) -> dict[str, Any]:
    """Two identical champion replays must produce identical summaries."""
    config = config or BacktestConfig()
    first = evaluate_continuous(
        list(bars), fast=CHAMPION_FAST, slow=CHAMPION_SLOW, config=config
    )
    second = evaluate_continuous(
        list(bars), fast=CHAMPION_FAST, slow=CHAMPION_SLOW, config=config
    )
    same = first.summary_dict() == second.summary_dict()
    return {
        "deterministic": same,
        "note": "identical inputs => identical full summary dict",
    }