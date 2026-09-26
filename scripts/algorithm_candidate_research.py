"""Candidate algorithm research harness — NIFTY 50 5m, fresh Upstox snapshot.

Deterministic diagnostic replay of research candidates on the latest N trading
days of a freshly-fetched Upstox NIFTY 50 snapshot. Reproduces the established
per-day replay contract (fresh Portfolio each day, fills at bar close with
slippage, WalkForwardConfig default economics) and reports an honest
head-to-head metrics table. This harness is READ-ONLY research: it places no
orders, posts no data, and never reads the protected out-of-sample period.

Candidates
----------
* model_0  moving_average_cross (fast=5, slow=21) — day-local signals, the
           documented baseline (reference-domain net -80,269.64 / 1,293 RT).
* model_1  regime_pullback (RegimePullbackParams frozen defaults) — decisions
           computed over the CONTINUOUS series (warm-up + diagnostic) so EMA200
           / ADX / ATR-ratio features are decision-time correct across days,
           then sliced per diagnostic day for the same daily-reset replay.

Artifacts (never overwriting a prior algorithm *definition*)
------------------------------------------------------------
* reports/algorithm_research_history.md / .html   master run log (refreshed)
* reports/algorithm_research_history.json         run registry (refreshed row)
* reports/algorithms/<id>.md / .html              refreshed run view per model
* reports/algorithms/<id>.json                    write-once definition + results
* reports/algorithms/<id>.run_<ts>.json           one immutable snapshot per run

Every run carries: run_id, generated_at, data hash, instrument, diagnostic day
list, params, headline net/gross/trades/PF/expectancy/maxDD, CALL/PUT/NO_TRADE
counts, regime/volatility/time-of-day buckets, robustness grid (--robustness),
cost scan (--cost-scan), and the honest option-profitability caveat.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from itertools import product
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from fno_ai_paper_trading.backtest.config import BacktestConfig  # noqa: E402
from fno_ai_paper_trading.backtest.engine import BacktestEngine  # noqa: E402
from fno_ai_paper_trading.backtest.result import BacktestResult  # noqa: E402
from fno_ai_paper_trading.data.dataset_store import (  # noqa: E402
    load_dataset,
)
from fno_ai_paper_trading.data.validation import (  # noqa: E402
    validate_bars,
    validation_to_dict,
)
from fno_ai_paper_trading.evaluation.fast_signal import (  # noqa: E402
    moving_average_cross_signals,
)
from fno_ai_paper_trading.learning.capture import pair_round_trips  # noqa: E402
from fno_ai_paper_trading.models.market import MarketPrice  # noqa: E402
from fno_ai_paper_trading.research.report import (  # noqa: E402
    build_research_html,
    card,
    escape,
    kv_rows,
    table,
)
from fno_ai_paper_trading.strategies.base import SignalResult, Strategy  # noqa: E402
from fno_ai_paper_trading.strategies.moving_average_cross import MovingAverageCrossStrategy  # noqa: E402
from fno_ai_paper_trading.strategies.regime_pullback import (  # noqa: E402
    RegimePullbackParams,
    RegimePullbackStrategy,
    regime_pullback_signals,
)
from fno_ai_paper_trading.strategies.trend_breakout import (  # noqa: E402
    TrendBreakoutParams,
    TrendBreakoutStrategy,
    trend_breakout_signals,
)
from fno_ai_paper_trading.strategies.end_of_day import (  # noqa: E402
    EndOfDayParams,
    EndOfDayStrategy,
    end_of_day_signals,
)
from fno_ai_paper_trading.walkforward.config import WalkForwardConfig  # noqa: E402

DEFAULT_DATASET = "datasets/nifty50_5m_research_snapshot.csv"
DEFAULT_DIAGNOSTIC_DAYS = 10
MODEL_0_META = {
    "algorithm_id": "model_0",
    "parent": None,
    "family": "moving_average_cross",
    "description": "documented baseline: MA(5,21) crossover, both sides, "
                   "day-local signals on the diagnostic window",
    "params": {"fast": 5, "slow": 21},
}
MODEL_1_META = {
    "algorithm_id": "model_1",
    "parent": None,
    "family": "regime_pullback",
    "description": "regime-aware pullback + momentum + volatility + cost filter "
                   "(frozen RegimePullbackParams defaults; continuous warm-up; "
                   "entry window 09:25-14:30, force exit 15:15, stop 1.5 ATR, "
                   "target 3 ATR, max hold 12 bars, confidence>=60)",
    "params": RegimePullbackParams().as_dict(),
}
MODEL_2_META = {
    "algorithm_id": "model_2",
    "parent": "model_1",
    "family": "trend_breakout",
    "description": "session opening-range breakout with multi-day-EMA bias, "
                   "ADX/RSI momentum, optional range-expansion filter and trailing "
                   "ATR ride to the session close (frozen TrendBreakoutParams "
                   "defaults; entry window 09:45-14:00, force exit 15:10, "
                   "stop 1.5 ATR, trail 2 ATR, no take profit, confidence>=60)",
    "params": TrendBreakoutParams().as_dict(),
}
MODEL_3_META = {
    "algorithm_id": "model_3",
    "parent": "model_2",
    "family": "end_of_day",
    "description": "low-frequency late-session trend capture (entries 13:00-14:45, "
                   "at most one per day, ride to 15:20 with an initial ATR stop; "
                   "multi-day-EMA bias + rolling 5-day daily-range conviction + "
                   "close on the range-bearing side). NOTED: at documented defaults "
                   "it never opens a position on the 10-day diagnostic window "
                   "(lagging bias + late-day midpoint rule), so no grid was run.",
    "params": EndOfDayParams().as_dict(),
}
META_BY_ID = {
    "model_0": MODEL_0_META,
    "model_1": MODEL_1_META,
    "model_2": MODEL_2_META,
    "model_3": MODEL_3_META,
}

# Robustness grids: diagnostic-only parameter sweeps, NEVER used to tune.
_MODEL_1_GRID_DOMAINS = {
    "adx_trend": [18, 20, 22, 25],
    "rsi_long_enter": [40.0, 45.0, 50.0],
    "rsi_short_enter": [50.0, 55.0, 60.0],
    "pullback_zone_atr": [0.25, 0.50, 0.75],
    "stop_atr": [1.0, 1.5, 2.0],
    "target_atr": [2, 3, 4],
}
_MODEL_2_GRID_DOMAINS = {
    "or_minutes": [15, 30, 60],
    "breakout_atr": [0.5, 1.0, 1.5],
    "stop_atr": [1.5, 2.0, 2.5],
    "trail_atr": [1.5, 2.5, 3.5],
    "adx_trend": [18, 20, 22],
    "max_hold_bars": [0, 45],
}
GENERIC_GRID_SPECS = {
    "model_1": {
        "domains": _MODEL_1_GRID_DOMAINS,
        "base_params": RegimePullbackParams,
        "signals_fn": lambda bars, params: regime_pullback_signals(bars, params),
        "strategy": RegimePullbackStrategy,
    },
    "model_2": {
        "domains": _MODEL_2_GRID_DOMAINS,
        "base_params": TrendBreakoutParams,
        "signals_fn": lambda bars, params: trend_breakout_signals(bars, params),
        "strategy": TrendBreakoutStrategy,
    },
}

_REPORTS = REPO_ROOT / "reports"
_ALGO_DIR = _REPORTS / "algorithms"
_HISTORY_MD = _REPORTS / "algorithm_research_history.md"
_HISTORY_HTML = _REPORTS / "algorithm_research_history.html"
_HISTORY_JSON = _REPORTS / "algorithm_research_history.json"

_ZERO = Decimal("0")

_CAVEAT = (
    "DIAGNOSTIC ONLY: decisions are replayed over the latest trading days of a "
    "freshly fetched Upstox NIFTY 50 5m snapshot. No option chain was used "
    "(chain/expiry data is NOT_AVAILABLE in this environment); profitability "
    "here is DIRECTIONAL index-point P&L under a deterministic paper fill "
    "model (bar-close fills + adverse slippage), NOT actual option P&L. "
    "These results are in-sample research evidence only."
)

_HUMAN = {
    "adx_trend": "ADX",
    "rsi_long_enter": "RSI-L",
    "rsi_short_enter": "RSI-S",
    "pullback_zone_atr": "Zone",
    "stop_atr": "Stop",
    "target_atr": "Target",
    "or_minutes": "OR(min)",
    "breakout_atr": "BRK-ATR",
    "trail_atr": "Trail",
    "max_hold_bars": "Hold",
}


# ---------------------------------------------------------------------------
# domain helpers
# ---------------------------------------------------------------------------


def _money(value: Decimal | str | float | None) -> str:
    if value is None:
        return "—"
    if isinstance(value, Decimal):
        return format(value, "f")
    return str(value)


def _pct(value: Decimal | None, digits: int = 2) -> str:
    if value is None:
        return "—"
    return f"{float(value):.{digits}f}%"


def _now_stamp() -> datetime:
    return datetime.now()


def group_by_day(bars: list[MarketPrice]) -> list[tuple[date, list[MarketPrice]]]:
    groups: list[tuple[date, list[MarketPrice]]] = []
    for bar in bars:
        day = bar.timestamp.date()
        if not groups or groups[-1][0] != day:
            groups.append((day, []))
        groups[-1][1].append(bar)
    return groups


def _day_signal_slices(
    full_signals: list[SignalResult], bars: list[MarketPrice]
) -> dict[date, list[SignalResult]]:
    slices: dict[date, list[SignalResult]] = {}
    for bar, signal in zip(bars, full_signals):
        slices.setdefault(bar.timestamp.date(), []).append(signal)
    return slices


def _signals_by_timestamp(
    day_signals: list[SignalResult],
) -> dict[datetime, SignalResult]:
    return {s.timestamp: s for s in day_signals if s.timestamp is not None}


# ---------------------------------------------------------------------------
# per-day replay
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DayReplay:
    day: date
    result: BacktestResult
    trips: tuple[tuple, ...]
    day_signals: tuple[SignalResult, ...]
    signal_by_ts: dict[datetime, SignalResult]

    @property
    def net_pnl(self) -> Decimal:
        return self.result.total_pnl

    @property
    def costs(self) -> Decimal:
        return self.result.transaction_costs


def replay_day(
    day_bars: list[MarketPrice],
    signals: list[SignalResult],
    strategy: Strategy,
    config: BacktestConfig,
) -> DayReplay:
    """Replay one trading day with a fresh portfolio (same contract as the
    walk-forward engine). ``signals`` must be exactly decision-time signals for
    ``day_bars`` (one per bar)."""
    result = BacktestEngine().run(
        day_bars, strategy, config, signals=signals
    )
    trips, _open = pair_round_trips(result.trades)
    return DayReplay(
        day=day_bars[0].timestamp.date(),
        result=result,
        trips=tuple(trips),
        day_signals=tuple(signals),
        signal_by_ts=_signals_by_timestamp(signals),
    )


# ---------------------------------------------------------------------------
# aggregate metrics
# ---------------------------------------------------------------------------


def _excursion_pct(day_bars: list[MarketPrice], entry, exit_, long: bool) -> tuple[Decimal, Decimal]:
    """Approximate MAE/MFE % of a round trip using bar extremes between fills."""
    clock = [b.timestamp for b in day_bars]
    i0 = next((i for i, ts in enumerate(clock) if ts == entry.executed_at), None)
    i1 = next((i for i, ts in enumerate(clock) if ts == exit_.executed_at), None)
    if i0 is None or i1 is None or i1 <= i0:
        return _ZERO, _ZERO
    price = entry.price
    if price <= 0:
        return _ZERO, _ZERO
    lows = [float(b.low) for b in day_bars[i0 + 1 : i1]]
    highs = [float(b.high) for b in day_bars[i0 + 1 : i1]]
    if not lows:
        return _ZERO, _ZERO
    p = float(price)
    if long:
        mae = (min(lows) - p) / p * 100.0
        mfe = (max(highs) - p) / p * 100.0
    else:
        mae = (p - max(highs)) / p * 100.0
        mfe = (p - min(lows)) / p * 100.0
    return Decimal(f"{mae:.6f}"), Decimal(f"{mfe:.6f}")


def _time_bucket(minute: int) -> str:
    if minute < 9 * 60 + 45:
        return "OPEN_915-945"
    if minute < 11 * 60:
        return "MORNING_945-1100"
    if minute < 13 * 60 + 30:
        return "LUNCH_1100-1330"
    if minute < 15 * 60:
        return "AFTERNOON_1330-1500"
    return "CLOSE_1500-1525"


def _bucket_totals(items: list, key_fn) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for item in items:
        key = key_fn(item)
        bucket = out.setdefault(key, {"trades": 0, "wins": 0, "realized_pnl": _ZERO})
        bucket["trades"] += 1
        bucket["wins"] += 1 if item["realized_pnl"] > _ZERO else 0
        bucket["realized_pnl"] += item["realized_pnl"]
    ordered = sorted(out.items(), key=lambda kv: (-kv[1]["trades"], str(kv[0])))
    return {
        key: {
            "trades": int(chunk["trades"]),
            "wins": int(chunk["wins"]),
            "realized_pnl": _money(chunk["realized_pnl"]),
        }
        for key, chunk in ordered
    }


@dataclass
class CandidateRun:
    algorithm_id: str
    strategy_name: str
    params: dict
    days: list[date]
    replays: list[DayReplay] = field(default_factory=list)
    signals_per_day: dict[date, list[SignalResult]] = field(default_factory=dict)
    strategy: Strategy | None = None

    # -- headline totals ----------------------------------------------------
    @property
    def net_pnl(self) -> Decimal:
        return sum((r.net_pnl for r in self.replays), _ZERO)

    @property
    def costs(self) -> Decimal:
        return sum((r.costs for r in self.replays), _ZERO)

    @property
    def slippage(self) -> Decimal:
        return sum((r.result.slippage_cost for r in self.replays), _ZERO)

    @property
    def commission(self) -> Decimal:
        return sum((r.result.total_commission for r in self.replays), _ZERO)

    @property
    def all_trips(self) -> list[tuple]:
        return [t for r in self.replays for t in r.trips]

    @property
    def num_trades(self) -> int:
        return len(self.all_trips)

    @property
    def wins(self) -> int:
        return sum(1 for e, x in self.all_trips if x.realized_pnl > _ZERO)

    @property
    def losses(self) -> int:
        return sum(1 for e, x in self.all_trips if x.realized_pnl < _ZERO)

    @property
    def gross_profit(self) -> Decimal:
        return sum((x.realized_pnl for _, x in self.all_trips if x.realized_pnl > _ZERO), _ZERO)

    @property
    def gross_loss(self) -> Decimal:
        return -sum((x.realized_pnl for _, x in self.all_trips if x.realized_pnl < _ZERO), _ZERO)

    @property
    def win_rate(self) -> Decimal:
        if not self.num_trades:
            return _ZERO
        return Decimal(self.wins) / Decimal(self.num_trades) * Decimal("100")

    @property
    def profit_factor(self) -> Decimal:
        if self.gross_loss == 0:
            return Decimal("Infinity") if self.gross_profit > 0 else Decimal("0")
        return self.gross_profit / self.gross_loss

    @property
    def expectancy(self) -> Decimal:
        if not self.num_trades:
            return _ZERO
        return self.net_pnl / Decimal(self.num_trades)

    @property
    def max_day_dd_pct(self) -> Decimal:
        values = [r.result.max_drawdown_pct for r in self.replays]
        return max(values, default=_ZERO)

    @property
    def mean_mae_pct(self) -> Decimal:
        values = self._excursions()[0]
        return sum(values, _ZERO) / Decimal(len(values)) if values else _ZERO

    @property
    def mean_mfe_pct(self) -> Decimal:
        values = self._excursions()[1]
        return sum(values, _ZERO) / Decimal(len(values)) if values else _ZERO

    def _excursions(self) -> tuple[list[Decimal], list[Decimal]]:
        maes: list[Decimal] = []
        mfes: list[Decimal] = []
        for replay in self.replays:
            day_bars = [b for b in _DAY_BARS_CACHE[replay.day]]
            for entry, exit_ in replay.trips:
                long = entry.side.value == "BUY"
                mae, mfe = _excursion_pct(day_bars, entry, exit_, long)
                maes.append(mae)
                mfes.append(mfe)
        return maes, mfes

    def signal_counts(self) -> dict[str, int]:
        counts = {"CALL": 0, "PUT": 0, "NO_TRADE": 0}
        for signals in self.signals_per_day.values():
            for signal in signals:
                key = signal.signal.value
                counts["CALL" if key == "BUY" else "PUT" if key == "SELL" else "NO_TRADE"] += 1
        return counts

    def trade_buckets(self) -> dict[str, dict]:
        rows = []
        for replay in self.replays:
            bucket_map = _time_bucket
            signal_by_ts = replay.signal_by_ts
            for entry, exit_ in replay.trips:
                signal = signal_by_ts.get(entry.executed_at)
                meta = signal.meta if signal is not None else {}
                rows.append({
                    "realized_pnl": exit_.realized_pnl,
                    "regime": str(meta.get("regime") or "UNKNOWN"),
                    "vol_bucket": str(meta.get("volatility_bucket") or "UNKNOWN"),
                    "time_bucket": bucket_map(entry.executed_at.hour * 60 + entry.executed_at.minute),
                })
        return {
            "regime": _bucket_totals(rows, lambda r: r["regime"]),
            "volatility": _bucket_totals(rows, lambda r: r["vol_bucket"]),
            "time_of_day": _bucket_totals(rows, lambda r: r["time_bucket"]),
        }

    def no_trade_reason_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for signals in self.signals_per_day.values():
            for signal in signals:
                if signal.signal.value != "HOLD":
                    continue
                primary = signal.meta.get("no_trade_primary")
                if primary:
                    counts[primary] = counts.get(primary, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: -kv[1]))

    def to_metrics_dict(self) -> dict[str, object]:
        return {
            "algorithm_id": self.algorithm_id,
            "strategy_name": self.strategy_name,
            "days": [d.isoformat() for d in self.days],
            "diagnostic_day_count": len(self.days),
            "net_pnl": _money(self.net_pnl),
            "gross": _money(self.gross_profit - self.gross_loss),
            "gross_profit": _money(self.gross_profit),
            "gross_loss": _money(self.gross_loss),
            "costs_total": _money(self.costs),
            "slippage": _money(self.slippage),
            "commission": _money(self.commission),
            "trades": self.num_trades,
            "wins": self.wins,
            "losses": self.losses,
            "win_rate_pct": _pct(self.win_rate, 2),
            "profit_factor": _money(self.profit_factor),
            "expectancy": _money(self.expectancy),
            "max_day_drawdown_pct": _pct(self.max_day_dd_pct, 2),
            "mean_mae_pct": _pct(self.mean_mae_pct, 3),
            "mean_mfe_pct": _pct(self.mean_mfe_pct, 3),
            "signals": self.signal_counts(),
            "buckets": self.trade_buckets(),
            "no_trade_reasons": self.no_trade_reason_counts(),
        }


_DAY_BARS_CACHE: dict[date, list[MarketPrice]] = {}


# ---------------------------------------------------------------------------
# candidate providers
# ---------------------------------------------------------------------------


def run_candidate(
    algorithm_id: str,
    bars: list[MarketPrice],
    groups: list[tuple[date, list[MarketPrice]]],
    diag_groups: list[tuple[date, list[MarketPrice]]],
    config: BacktestConfig,
) -> CandidateRun:
    """Replay both candidates over the diagnostic days and aggregate metrics."""
    for day, day_bars in groups:
        _DAY_BARS_CACHE[day] = day_bars

    if algorithm_id == "model_0":
        params = {"fast": 5, "slow": 21}
        signals_per_day: dict[date, list[SignalResult]] = {}
        replays: list[DayReplay] = []
        strategy: Strategy = MovingAverageCrossStrategy(**params)
        for day, day_bars in diag_groups:
            day_signals = moving_average_cross_signals(day_bars, **params)
            signals_per_day[day] = day_signals
            replays.append(replay_day(day_bars, day_signals, strategy, config))
        return CandidateRun(
            algorithm_id="model_0", strategy_name=strategy.name, params=params,
            days=[d for d, _ in diag_groups], replays=replays,
            signals_per_day=signals_per_day, strategy=strategy,
        )

    if algorithm_id == "model_1":
        params = RegimePullbackParams()
        strategy: Strategy = RegimePullbackStrategy(params)
        full_signals = regime_pullback_signals(bars, params)
        slices = _day_signal_slices(full_signals, bars)
        signals_per_day = {day: slices[day] for day, _ in diag_groups}
        replays = [
            replay_day(day_bars, slices[day], strategy, config)
            for day, day_bars in diag_groups
        ]
        return CandidateRun(
            algorithm_id="model_1", strategy_name=strategy.name,
            params=params.as_dict(), days=[d for d, _ in diag_groups],
            replays=replays, signals_per_day=signals_per_day, strategy=strategy,
        )

    if algorithm_id == "model_2":
        params = TrendBreakoutParams()
        strategy: Strategy = TrendBreakoutStrategy(params)
        full_signals = trend_breakout_signals(bars, params)
        slices = _day_signal_slices(full_signals, bars)
        signals_per_day = {day: slices[day] for day, _ in diag_groups}
        replays = [
            replay_day(day_bars, slices[day], strategy, config)
            for day, day_bars in diag_groups
        ]
        return CandidateRun(
            algorithm_id="model_2", strategy_name=strategy.name,
            params=params.as_dict(), days=[d for d, _ in diag_groups],
            replays=replays, signals_per_day=signals_per_day, strategy=strategy,
        )

    if algorithm_id == "model_3":
        params = EndOfDayParams()
        strategy: Strategy = EndOfDayStrategy(params)
        full_signals = end_of_day_signals(bars, params)
        slices = _day_signal_slices(full_signals, bars)
        signals_per_day = {day: slices[day] for day, _ in diag_groups}
        replays = [
            replay_day(day_bars, slices[day], strategy, config)
            for day, day_bars in diag_groups
        ]
        return CandidateRun(
            algorithm_id="model_3", strategy_name=strategy.name,
            params=params.as_dict(), days=[d for d, _ in diag_groups],
            replays=replays, signals_per_day=signals_per_day, strategy=strategy,
        )

    raise ValueError(f"unknown candidate {algorithm_id!r}")


# ---------------------------------------------------------------------------
# robustness grid (model_1) + cost scan
# ---------------------------------------------------------------------------


def _robustness_grid(
    algorithm_id: str,
    bars: list[MarketPrice],
    diag_groups: list[tuple[date, list[MarketPrice]]],
    config: BacktestConfig,
) -> dict[str, object]:
    """Run the cartesian parameter grid for ``algorithm_id`` (diagnostic-only)."""
    spec = GENERIC_GRID_SPECS[algorithm_id]
    domains = spec["domains"]
    base = spec["base_params"]()
    signals_fn = spec["signals_fn"]
    strategy = spec["strategy"](base)
    keys = list(domains)
    total = 1
    for values in domains.values():
        total *= len(values)
    run_cache: dict[str, tuple[CandidateRun, float]] = {}

    def run_with(overrides: dict) -> tuple[CandidateRun, float]:
        token = "|".join(f"{k}={overrides[k]}" for k in sorted(overrides))
        if token in run_cache:
            return run_cache[token]
        params = base.__class__(**{**base.as_dict(), **overrides})
        full_signals = signals_fn(bars, params)
        day_slices = _day_signal_slices(full_signals, bars)
        replays = [
            replay_day(day_bars, day_slices[day], strategy, config)
            for day, day_bars in diag_groups
        ]
        run = CandidateRun(
            algorithm_id="grid", strategy_name=strategy.name,
            params=params.as_dict(), days=[d for d, _ in diag_groups],
            replays=replays, strategy=strategy,
            signals_per_day={d: day_slices[d] for d, _ in diag_groups},
        )
        run_cache[token] = (run, float(run.net_pnl))
        return run_cache[token]

    rows: list[dict] = []
    for combo in product(*domains.values()):
        overrides = dict(zip(keys, combo))
        run, _net = run_with(overrides)
        rows.append({
            **{k: overrides[k] for k in keys},
            "net_pnl": _money(run.net_pnl),
            "trades": run.num_trades,
            "wins": run.wins,
            "losses": run.losses,
            "win_rate_pct": _pct(run.win_rate, 1),
            "profit_factor": _money(run.profit_factor),
            "expectancy": _money(run.expectancy),
            "max_day_dd_pct": _pct(run.max_day_dd_pct, 2),
        })
    positive = sum(1 for r in rows if Decimal(str(r["net_pnl"])) > 0)
    return {
        "algorithm_id": algorithm_id,
        "domain_params": domains,
        "combinations_total": len(rows),
        "combinations_positive_net": positive,
        "base_case": base.as_dict(),
        "rows": rows,
    }


def _cost_scan(
    candidate: CandidateRun,
    replays_by_cfg: dict[str, list[DayReplay]],
) -> dict[str, object]:
    rows: list[dict] = []
    for label, replays in replays_by_cfg.items():
        proxy = CandidateRun(
            algorithm_id=candidate.algorithm_id,
            strategy_name=candidate.strategy_name,
            params=candidate.params,
            days=candidate.days,
            replays=replays,
            signals_per_day=candidate.signals_per_day,
        )
        rows.append({
            "config": label,
            "net_pnl": _money(proxy.net_pnl),
            "costs_total": _money(proxy.costs),
            "trades": proxy.num_trades,
            "profit_factor": _money(proxy.profit_factor),
            "expectancy": _money(proxy.expectancy),
        })
    return {"rows": rows}


def _scan_configs() -> dict[str, BacktestConfig]:
    configs: dict[str, BacktestConfig] = {}
    multipliers = {"1x": Decimal("1"), "3x": Decimal("3"), "5x": Decimal("5")}
    for clabel, cm in multipliers.items():
        for slabel, sm in multipliers.items():
            configs[f"comm{clabel}_slip{slabel}"] = BacktestConfig(
                commission_rate=Decimal("0.0003") * cm,
                slippage_rate=Decimal("0.001") * sm,
            )
    return configs


# ---------------------------------------------------------------------------
# report writers
# ---------------------------------------------------------------------------


def _md_table(headers: list[str], rows: list[list[str]]) -> str:
    head = "| " + " | ".join(headers) + " |"
    sep = "| " + " | ".join("---" for _ in headers) + " |"
    body = ["| " + " | ".join(row) + " |" for row in rows]
    return "\n".join([head, sep] + body)


def _run_header(run_id: str, generated_at: str, algorithm_id: str, dataset_hash: str) -> str:
    return (
        f"- **run_id**: `{run_id}`\n"
        f"- **generated_at**: {generated_at}\n"
        f"- **algorithm_id**: `{algorithm_id}`\n"
        f"- **data_hash**: `{dataset_hash}`\n"
    )


def _candidate_md_section(candidate: CandidateRun, run_id: str, generated_at: str, dataset_hash: str) -> str:
    m = candidate.to_metrics_dict()
    lines = [
        f"## {candidate.algorithm_id} — {candidate.strategy_name}",
        "",
        _run_header(run_id, generated_at, candidate.algorithm_id, dataset_hash),
        "",
        "### Headline (diagnostic window, daily-reset replay)",
        "",
    ]
    kv = [
        ("Net P&L", m["net_pnl"]),
        ("Gross profit", m["gross_profit"]),
        ("Gross loss", m["gross_loss"]),
        ("Costs (slippage + commission)", m["costs_total"]),
        ("Trades (round trips)", str(m["trades"])),
        ("Wins / Losses", f"{m['wins']} / {m['losses']}"),
        ("Win rate", m["win_rate_pct"]),
        ("Profit factor", m["profit_factor"]),
        ("Expectancy (/trade)", m["expectancy"]),
        ("Max daily drawdown", m["max_day_drawdown_pct"]),
        ("Mean MAE / MFE", f"{m['mean_mae_pct']} / {m['mean_mfe_pct']}"),
        ("Signals CALL/PUT/NO_TRADE", f"{m['signals']['CALL']}/{m['signals']['PUT']}/{m['signals']['NO_TRADE']}"),
    ]
    lines.append(_md_table(["Metric", "Value"], [[k, v] for k, v in kv]))
    lines.append("")
    lines.append("### Per trading day")
    day_rows = []
    for replay in candidate.replays:
        day_rows.append([
            replay.day.isoformat(),
            _money(replay.net_pnl),
            str(len(replay.trips)),
            _money(replay.costs),
        ])
    lines.append(_md_table(["Day", "Net P&L", "RT", "Costs"], day_rows))
    lines.append("")
    lines.append("### Trade buckets")
    for bucket_name, buckets in candidate.trade_buckets().items():
        lines.append("")
        lines.append(f"**{bucket_name}**")
        lines.append(_md_table(
            ["Bucket", "Trades", "Wins", "Realized P&L"],
            [[k, str(v["trades"]), str(v["wins"]), v["realized_pnl"]] for k, v in buckets.items()],
        ))
    lines.append("")
    lines.append("### NO_TRADE primary reasons (diagnostic)")
    rows = [[k, str(v)] for k, v in candidate.no_trade_reason_counts().items()]
    lines.append(_md_table(["Reason", "Count"], rows) if rows else "_none_")
    lines.append("")
    return "\n".join(lines)


def _candidate_html_section(candidate: CandidateRun, generated_at: str) -> str:
    m = candidate.to_metrics_dict()
    blocks = [
        "<section><h2>Headline</h2><div class='cards'>"
        + card(m["net_pnl"], "Net P&L", "ok" if candidate.net_pnl >= 0 else "bad")
        + card(str(m["trades"]), "Round trips")
        + card(m["win_rate_pct"], "Win rate")
        + card(m["profit_factor"], "Profit factor")
        + card(m["expectancy"], "Expectancy")
        + card(m["max_day_drawdown_pct"], "Max daily DD")
        + "</div>"
        + kv_rows([
            ("Gross profit", m["gross_profit"]),
            ("Gross loss", m["gross_loss"]),
            ("Costs (slippage + commission)", m["costs_total"]),
            ("Slippage", m["slippage"]),
            ("Commission", m["commission"]),
            ("Wins / Losses", f"{m['wins']} / {m['losses']}"),
            ("Mean MAE / MFE", f"{m['mean_mae_pct']} / {m['mean_mfe_pct']}"),
            ("Signals CALL/PUT/NO_TRADE",
             f"{m['signals']['CALL']}/{m['signals']['PUT']}/{m['signals']['NO_TRADE']}"),
        ])
        + "</section>",
        "<section><h2>Per day</h2>"
        + table(["Day", "Net P&L", "RT", "Costs"], [
            [r.day.isoformat(), _money(r.net_pnl), str(len(r.trips)), _money(r.costs)]
            for r in candidate.replays
        ])
        + "</section>",
    ]
    for bucket_name, buckets in candidate.trade_buckets().items():
        blocks.append(
            f"<section><h2>Buckets · {bucket_name}</h2>"
            + table(["Bucket", "Trades", "Wins", "Realized P&L"], [
                [k, str(v["trades"]), str(v["wins"]), v["realized_pnl"]]
                for k, v in buckets.items()
            ])
            + "</section>"
        )
    no_trade = candidate.no_trade_reason_counts()
    nt_rows = [[k, str(v)] for k, v in no_trade.items()]
    blocks.append(
        "<section><h2>NO_TRADE reasons (diagnostic)</h2>"
        + (table(["Reason", "Count"], nt_rows) if nt_rows else "<p>none</p>")
        + "</section>"
    )
    return "".join(blocks)


def _build_candidate_page(
    candidate: CandidateRun, run_id: str, generated_at: str, dataset: dict,
    *, robustness: dict | None = None, cost_scan: dict | None = None,
) -> str:
    title = f"Algorithm {candidate.algorithm_id} — {candidate.strategy_name}"
    blocks = [
        "<section><h2>Identity</h2>" + kv_rows([
            ("algorithm_id", candidate.algorithm_id),
            ("strategy_name", candidate.strategy_name),
            ("params", json.dumps(candidate.params, sort_keys=True)),
            ("diagnostic days", ", ".join(d.isoformat() for d in candidate.days)),
            ("data_hash", str(dataset.get("data_hash"))),
            ("provider", str(dataset.get("provider"))),
            ("interval", str(dataset.get("interval"))),
            ("run_id", run_id),
        ]) + "</section>",
        _candidate_html_section(candidate, generated_at),
    ]
    if robustness is not None:
        keys = list(robustness["domain_params"])
        rows = [[
            *(str(r[k]) for k in keys),
            r["net_pnl"], str(r["trades"]), r["win_rate_pct"], r["profit_factor"],
            r["expectancy"],
        ] for r in robustness["rows"]]
        blocks.append(
            "<section><h2>Robustness grid "
            f"({robustness['combinations_total']} param combos, "
            f"{robustness['combinations_positive_net']} with net&gt;0; "
            "diagnostic-only, never used to tune)</h2>"
            + table([
                *[_HUMAN.get(k, k) for k in keys],
                "Net P&L", "Trades", "WR%", "PF", "Expectancy",
            ], rows)
            + "</section>"
        )
    if cost_scan is not None:
        rows = [[
            r["config"], r["net_pnl"], r["costs_total"], str(r["trades"]),
            r["profit_factor"], r["expectancy"],
        ] for r in cost_scan["rows"]]
        blocks.append(
            "<section><h2>Cost scan (commission &times; slippage)</h2>"
            + table(["Config", "Net P&L", "Costs", "Trades", "PF", "Expectancy"], rows)
            + "</section>"
        )
    return build_research_html(
        title=title, blocks=blocks, disclaimer=_CAVEAT, generated_at=generated_at,
    )


def _build_history_page(
    rows: list[dict], generated_at: str,
) -> str:
    blocks = [
        "<section><h2>Run registry</h2>"
        + table([
            "Run", "Generated", "Algorithm", "Directional net P&L", "Trades",
            "WR%", "PF", "Status",
        ], [[
            str(r["run_id"]), str(r["generated_at"]), str(r["algorithm_id"]),
            _money(r["net_pnl"]), str(r["trades"]), r["win_rate_pct"],
            _money(r["profit_factor"]), str(r["status"]),
        ] for r in reversed(rows)])
        + "</section>",
    ]
    return build_research_html(
        title="Algorithm Research History — NIFTY 50 F&O Decision Models",
        blocks=blocks,
        disclaimer=_CAVEAT,
        generated_at=generated_at,
    )


def _write_once(path: Path, payload: dict) -> bool:
    """Write an immutable algorithm-definition artifact; no-op if it exists."""
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    return True


def _default_json(value) -> str:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, tuple):
        return list(value)
    raise TypeError(f"cannot serialize {type(value).__name__}")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def _parse() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="NIFTY 50 candidate-agnostic research harness (READ-ONLY).")
    parser.add_argument("--dataset", default=DEFAULT_DATASET)
    parser.add_argument("--diagnostic-days", type=int, default=DEFAULT_DIAGNOSTIC_DAYS)
    parser.add_argument("--candidates", nargs="+", default=["model_0", "model_1", "model_2", "model_3"],
                        choices=["model_0", "model_1", "model_2", "model_3"])
    parser.add_argument("--robustness", action="store_true",
                        help="run the parameter robustness grids (model_1, model_2)")
    parser.add_argument("--cost-scan", action="store_true",
                        help="run the cost sensitivity scans (every candidate)")
    parser.add_argument("--json", action="store_true", help="print a machine-readable summary")
    return parser.parse_args()


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    args = _parse()

    dataset = load_dataset(Path(args.dataset))
    bars = dataset.bars
    validation = validate_bars(bars, interval_minutes=5, allow_empty=False)
    if not validation.ok:
        print("dataset failed validation; refusing to run research.", file=sys.stderr)
        for issue in validation.errors:
            print(f"  {issue}", file=sys.stderr)
        return 2

    groups = group_by_day(bars)
    if args.diagnostic_days <= 0:
        parser = None
        raise ValueError("--diagnostic-days must be >= 1")
    if len(groups) < args.diagnostic_days + 1:
        print(
            f"need at least {args.diagnostic_days + 1} trading days for a warm-up prefix; "
            f"snapshot has {len(groups)}.",
            file=sys.stderr,
        )
        return 3
    warmup_groups = groups[: len(groups) - args.diagnostic_days]
    diag_groups = groups[len(groups) - args.diagnostic_days :]
    del warmup_groups  # model_1 uses the whole continuous series for features

    config = WalkForwardConfig().backtest_config()
    generated_at = _now_stamp().isoformat(timespec="seconds")
    run_id = f"run_{_now_stamp():%Y%m%d_%H%M%S}"

    runs: dict[str, CandidateRun] = {}
    for algorithm_id in args.candidates:
        runs[algorithm_id] = run_candidate(algorithm_id, bars, groups, diag_groups, config)

    dataset_meta = {
        "name": dataset.path.stem,
        "data_hash": dataset.data_hash,
        "provider": dataset.metadata.get("provider"),
        "interval": dataset.metadata.get("interval"),
        "num_bars": dataset.metadata.get("num_bars"),
        "start_date": dataset.metadata.get("start_date"),
        "end_date": dataset.metadata.get("end_date"),
        "timezone": dataset.metadata.get("timezone"),
        "instrument": dataset.metadata.get("instrument"),
    }

    robustness: dict[str, dict | None] = {aid: None for aid in runs}
    cost_scan: dict[str, dict | None] = {aid: None for aid in runs}
    if args.robustness:
        for aid in runs:
            if aid in GENERIC_GRID_SPECS:
                robustness[aid] = _robustness_grid(aid, bars, diag_groups, config)
    if args.cost_scan:
        day_map = dict(diag_groups)
        for aid, candidate in runs.items():
            replays_by_cfg: dict[str, list] = {}
            for label, cfg in _scan_configs().items():
                replays_by_cfg[label] = [
                    replay_day(
                        day_map[candidate.days[i]],
                        list(candidate.signals_per_day[candidate.days[i]]),
                        candidate.strategy or RegimePullbackStrategy(),
                        cfg,
                    )
                    for i in range(len(candidate.days))
                ]
            cost_scan[aid] = _cost_scan(candidate, replays_by_cfg)

    # ----- write per-candidate artifacts -----
    _ALGO_DIR.mkdir(parents=True, exist_ok=True)
    for algorithm_id, candidate in runs.items():
        meta = META_BY_ID[algorithm_id]
        page = _build_candidate_page(
            candidate, run_id, generated_at, dataset_meta,
            robustness=robustness[algorithm_id],
            cost_scan=cost_scan[algorithm_id],
        )
        ( _ALGO_DIR / f"{algorithm_id}.html").write_text(page, encoding="utf-8")
        md = _candidate_md_section(candidate, run_id, generated_at, dataset_meta["data_hash"])
        grid = robustness[algorithm_id]
        if grid is not None:
            keys = list(grid["domain_params"])
            md += "\n## Robustness grid (diagnostic-only, never used to tune)\n\n"
            md += _md_table(
                [_HUMAN[k] for k in keys] + ["Trades", "WR%", "PF", "Exp."],
                [[str(r[k]) for k in keys] + [str(r["trades"]), r["win_rate_pct"],
                  r["profit_factor"], r["expectancy"]] for r in grid["rows"]],
            )
            md += (
                f"\n{grid['combinations_total']} combos tested; "
                f"{grid['combinations_positive_net']} had net>0.\n"
            )
        if cost_scan[algorithm_id] is not None:
            md += "\n## Cost scan\n\n"
            md += _md_table(
                ["Config", "Net P&L", "Costs", "Trades", "PF", "Exp."],
                [[r["config"], r["net_pnl"], r["costs_total"], str(r["trades"]),
                  r["profit_factor"], r["expectancy"]] for r in cost_scan[algorithm_id]["rows"]],
            )
        ( _ALGO_DIR / f"{algorithm_id}.md").write_text(
            md + "\n\n---\n\n> " + _CAVEAT + "\n", encoding="utf-8",
        )
        definition = {
            "algorithm_id": meta["algorithm_id"],
            "family": meta["family"],
            "parent": meta["parent"],
            "description": meta["description"],
            "params": meta["params"],
            "data": {
                "data_hash": dataset_meta["data_hash"],
                "interval": dataset_meta["interval"],
                "instrument": dataset_meta["instrument"],
                "diagnostic_days": [d.isoformat() for d in candidate.days],
            },
            "results": candidate.to_metrics_dict(),
            "run_id": run_id,
            "generated_at": generated_at,
            "status": "DIAGNOSTIC_ONLY",
            "option_profitability": "NOT_AVAILABLE",
        }
        _write_once(_ALGO_DIR / f"{algorithm_id}.json", definition)
        snapshot = dict(definition)
        snapshot["robustness"] = robustness[algorithm_id]
        snapshot["cost_scan"] = cost_scan[algorithm_id]
        ( _ALGO_DIR / f"{algorithm_id}.run_{run_id}.json").write_text(
            json.dumps(snapshot, indent=2, sort_keys=True, default=_default_json) + "\n",
            encoding="utf-8",
        )

    # ----- write the master history registry + pages -----
    history_rows: list[dict] = []
    if _HISTORY_JSON.exists():
        try:
            history_rows = json.loads(_HISTORY_JSON.read_text(encoding="utf-8")).get("runs", [])
        except (json.JSONDecodeError, OSError):
            history_rows = []
    for algorithm_id, candidate in runs.items():
        history_rows.append({
            "run_id": run_id,
            "generated_at": generated_at,
            "algorithm_id": algorithm_id,
            "dataset_hash": dataset_meta["data_hash"],
            "diagnostic_days": [d.isoformat() for d in candidate.days],
            "net_pnl": _money(candidate.net_pnl),
            "trades": candidate.num_trades,
            "win_rate_pct": _pct(candidate.win_rate, 2),
            "profit_factor": _money(candidate.profit_factor),
            "expectancy": _money(candidate.expectancy),
            "status": "DIAGNOSTIC_ONLY",
            "option_profitability": "NOT_AVAILABLE",
        })
    registry = {"runs": history_rows, "generated_at": generated_at}
    _HISTORY_JSON.write_text(
        json.dumps(registry, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8",
    )
    _HISTORY_HTML.write_text(_build_history_page(history_rows, generated_at), encoding="utf-8")

    md_lines = ["# Algorithm Research History — NIFTY 50 F&amp;O Decision Models", ""]
    md_lines.append(f"**generated_at**: {generated_at}  ")
    md_lines.append(f"**data_hash**: `{dataset_meta['data_hash']}`  ")
    md_lines.append(f"**diagnostic window**: {diag_groups[0][0]} .. {diag_groups[-1][0]} "
                    f"({len(diag_groups)} trading days, daily-reset replay)  ")
    md_lines.append(f"> {_CAVEAT}")
    md_lines.append("")
    md_lines.append("## Run registry")
    md_lines.append("")
    md_lines.append(_md_table(
        ["Run", "Generated", "Algorithm", "Net P&L", "Trades", "WR%", "PF", "Status"],
        [[r["run_id"], r["generated_at"], r["algorithm_id"], _money(r["net_pnl"]),
          str(r["trades"]), r["win_rate_pct"], _money(r["profit_factor"]), r["status"]]
         for r in reversed(history_rows)],
    ))
    md_lines.append("")
    for algorithm_id in runs:
        md_lines.append(_candidate_md_section(
            runs[algorithm_id], run_id, generated_at, dataset_meta["data_hash"],
        ))
    _HISTORY_MD.write_text("\n".join(md_lines) + "\n", encoding="utf-8")

    # ----- summary to stdout -----
    summary = {
        "run_id": run_id,
        "generated_at": generated_at,
        "data_hash": dataset_meta["data_hash"],
        "diagnostic_days": [d.isoformat() for d, _ in diag_groups],
        "results": {aid: runs[aid].to_metrics_dict() for aid in runs},
        "reports": {
            "history_md": str(_HISTORY_MD),
            "history_html": str(_HISTORY_HTML),
            "history_json": str(_HISTORY_JSON),
            "candidate_pages": {aid: str(_ALGO_DIR / f"{aid}.html") for aid in runs},
        },
    }
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True, default=_default_json))
    else:
        print(f"run_id         : {run_id}")
        print(f"data           : {dataset_meta['data_hash'][:16]} ({dataset_meta['num_bars']} bars)")
        print(f"diagnostic     : {diag_groups[0][0]} .. {diag_groups[-1][0]} ({len(diag_groups)} days)")
        for aid in runs:
            r = runs[aid]
            print(
                f"{aid:8s} net={str(r.net_pnl):>10} trades={r.num_trades:>4} "
                f"WR={float(r.win_rate):5.2f}% PF={str(r.profit_factor):>8} "
                f"exp={str(r.expectancy):>9}"
            )
        print("REPORT_HTML_UPDATED:", _HISTORY_HTML,
              "|", ", ".join(str(_ALGO_DIR / f"{aid}.html") for aid in runs))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())