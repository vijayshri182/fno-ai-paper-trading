"""Discovery backtest engine: per-day fresh-portfolio replays + full metrics.

Reuses the production :class:`BacktestEngine` / :class:`WalkForwardConfig`
economies (slippage 0.10%, commission 0.03%/side, quantity 1, initial capital
100k, 2% protective stop, risk manager on) for every daily replay. Each day
starts flat with a fresh portfolio; the provider's force-exit signals square the
day so end-of-day square-off is real, and an unmatched position is still
reported as ``open_at_close`` instead of being silently hidden.

Metrics are computed over completed round trips *and* the day-level equity path:
net/gross, WR, PF, expectancy, avg win/loss, max drawdown, Sharpe (where the
daily-return series is valid), yearly / monthly, regime / volatility-bucket /
time-of-day / CALL-PUT attribution, longest losing streak, trade frequency and
segment stability. The window is sliced into train/validation by the cycle
runner; the protected OOS segment is never loaded here.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Callable, Iterable, Sequence

from fno_ai_paper_trading.backtest.config import BacktestConfig
from fno_ai_paper_trading.backtest.engine import BacktestEngine
from fno_ai_paper_trading.backtest.result import BacktestResult
from fno_ai_paper_trading.learning.capture import pair_round_trips
from fno_ai_paper_trading.models.enums import OrderSide
from fno_ai_paper_trading.models.market import MarketPrice
from fno_ai_paper_trading.models.position import Trade
from fno_ai_paper_trading.strategies.base import SignalResult, Strategy
from fno_ai_paper_trading.walkforward.config import WalkForwardConfig

from fno_ai_paper_trading.discovery.catalog import (
    CandidateDefinition,
    build_params,
    resolve_signal_fn,
)


class _ReplayStrategy(Strategy):
    """Analyze is never called: the engine receives precomputed per-day signals."""

    name = "discovery.replay"

    def analyze(self, bars: list[MarketPrice]) -> SignalResult:
        raise RuntimeError("analyze() disabled; engine must receive signals=")


@dataclass(frozen=True)
class TradeRecord:
    """One completed round trip with full attribution for mining."""

    day: date
    side: str  # CALL (long entry) or PUT (short entry)
    entry_time: str
    entry_meta: dict[str, Any]
    exit_reason: str
    entry_price: Decimal
    exit_price: Decimal
    net_pnl: Decimal
    costs: Decimal
    gross: Decimal

    def to_dict(self) -> dict[str, Any]:
        return {
            "day": self.day.isoformat(),
            "side": self.side,
            "entry_time": self.entry_time,
            "entry_meta": self.entry_meta,
            "exit_reason": self.exit_reason,
            "entry_price": format(self.entry_price, "f"),
            "exit_price": format(self.exit_price, "f"),
            "net_pnl": format(self.net_pnl, "f"),
            "costs": format(self.costs, "f"),
            "gross": format(self.gross, "f"),
        }


@dataclass
class DayReplay:
    """Result of one daily fresh-portfolio replay."""

    day: date
    net_pnl: Decimal
    gross_pnl: Decimal
    costs: Decimal
    trades: int
    wins: int
    losses: int
    open_at_close: int
    max_drawdown_pct: float
    signals: int = 0
    records: list[TradeRecord] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "day": self.day.isoformat(),
            "net_pnl": format(self.net_pnl, "f"),
            "gross_pnl": format(self.gross_pnl, "f"),
            "costs": format(self.costs, "f"),
            "trades": self.trades,
            "wins": self.wins,
            "losses": self.losses,
            "open_at_close": self.open_at_close,
            "max_drawdown_pct": round(self.max_drawdown_pct, 6),
        }


@dataclass
class DiscoveryRun:
    """Full-metric view of one candidate over one (train/validation) window."""

    candidate_id: str
    version: str
    window_start: date
    window_end: date
    days: int
    days_with_trades: int
    trades: int
    wins: int
    losses: int
    win_rate: float
    gross_pnl: Decimal
    net_pnl: Decimal
    costs: Decimal
    profit_factor: float
    expectancy: Decimal
    avg_win: Decimal
    avg_loss: Decimal
    max_drawdown_pct: float
    sharpe: float | None
    avg_pnl_per_day: Decimal
    trade_frequency: float
    open_at_close_total: int
    consecutive_losses: int
    yearly: dict[str, Decimal]
    monthly: dict[str, Decimal]
    regime_breakdown: dict[str, dict[str, Any]]
    vol_bucket_breakdown: dict[str, dict[str, Any]]
    tod_breakdown: dict[str, dict[str, Any]]
    side_breakdown: dict[str, dict[str, Any]]
    segment_nets: list[Decimal]
    positive_segments: int
    segments: int
    quality_issues: list[str]
    records: list[TradeRecord]
    day_replays: list[DayReplay]

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "version": self.version,
            "window_start": self.window_start.isoformat(),
            "window_end": self.window_end.isoformat(),
            "days": self.days,
            "days_with_trades": self.days_with_trades,
            "trades": self.trades,
            "wins": self.wins,
            "losses": self.losses,
            "win_rate": round(self.win_rate, 4),
            "gross_pnl": format(self.gross_pnl, "f"),
            "net_pnl": format(self.net_pnl, "f"),
            "costs": format(self.costs, "f"),
            "profit_factor": (round(self.profit_factor, 4) if math.isfinite(self.profit_factor) else "inf"),
            "expectancy": format(self.expectancy, "f"),
            "avg_win": format(self.avg_win, "f"),
            "avg_loss": format(self.avg_loss, "f"),
            "max_drawdown_pct": round(self.max_drawdown_pct, 6),
            "sharpe": (round(self.sharpe, 4) if self.sharpe is not None else None),
            "avg_pnl_per_day": format(self.avg_pnl_per_day, "f"),
            "trade_frequency": round(self.trade_frequency, 4),
            "open_at_close_total": self.open_at_close_total,
            "consecutive_losses": self.consecutive_losses,
            "yearly": {k: format(v, "f") for k, v in sorted(self.yearly.items())},
            "monthly": {k: format(v, "f") for k, v in sorted(self.monthly.items(), reverse=True)},
            "regime_breakdown": {k: {kk: format(vv, "f") if isinstance(vv, Decimal) else vv for kk, vv in v.items()} for k, v in self.regime_breakdown.items()},
            "vol_bucket_breakdown": {k: {kk: format(vv, "f") if isinstance(vv, Decimal) else vv for kk, vv in v.items()} for k, v in self.vol_bucket_breakdown.items()},
            "tod_breakdown": {k: {kk: format(vv, "f") if isinstance(vv, Decimal) else vv for kk, vv in v.items()} for k, v in self.tod_breakdown.items()},
            "side_breakdown": {k: {kk: format(vv, "f") if isinstance(vv, Decimal) else vv for kk, vv in v.items()} for k, v in self.side_breakdown.items()},
            "segment_nets": [format(v, "f") for v in self.segment_nets],
            "positive_segments": self.positive_segments,
            "segments": self.segments,
            "quality_issues": self.quality_issues,
        }


def _float(value: Decimal | float) -> float:
    return float(value)


def _group_days(bars: Iterable[MarketPrice], start: date | None, end: date | None) -> list[list[MarketPrice]]:
    """Chronological day-bar groups strictly inside [start, end]."""
    by_day: dict[date, list[MarketPrice]] = {}
    for bar in bars:
        day = bar.timestamp.date()
        if start is not None and day < start:
            continue
        if end is not None and day > end:
            continue
        by_day.setdefault(day, []).append(bar)
    return [by_day[day] for day in sorted(by_day)]


def _trade_records(day: date, signals: Sequence[SignalResult], trades: Sequence[Trade]) -> tuple[list[TradeRecord], int]:
    """Completed round trips with attribution from the day's decision-time meta."""
    meta_by_ts = {
        s.timestamp.isoformat(): ((dict(s.meta) if isinstance(s.meta, dict) else {}) if s.meta else {})
        for s in signals if s.timestamp is not None
    }
    sig_by_ts = {
        s.timestamp.isoformat(): s for s in signals if s.timestamp is not None
    }
    round_trips, open_count = pair_round_trips(trades)
    records: list[TradeRecord] = []
    for entry, exit_ in round_trips:
        side = "CALL" if entry.side == OrderSide.BUY else "PUT"
        entry_meta = meta_by_ts.get(entry.executed_at.isoformat(), {})
        exit_sig = sig_by_ts.get(exit_.executed_at.isoformat())
        exit_reason = exit_sig.reason if exit_sig is not None else "exit (no signal meta)"
        records.append(
            TradeRecord(
                day=day,
                side=side,
                entry_time=entry.executed_at.strftime("%H:%M"),
                entry_meta=entry_meta,
                exit_reason=exit_reason,
                entry_price=entry.price,
                exit_price=exit_.price,
                net_pnl=exit_.realized_pnl - entry.commission - exit_.commission,
                costs=entry.commission + exit_.commission,
                gross=exit_.realized_pnl,
            )
        )
    return records, open_count


def replay_candidate(
    defn: CandidateDefinition,
    bars: Sequence[MarketPrice],
    config: WalkForwardConfig,
    *,
    start: date | None = None,
    end: date | None = None,
) -> list[DayReplay]:
    """Replay every day in [start, end] with a fresh portfolio; return per-day results.

    Signals are computed once over the *full* series (decision-time: each value
    uses only bars up to and including itself, so indicators and cross-day
    range/volatility statistics respect warm-up and never look ahead). Each day
    is then replayed with a fresh portfolio using that day's signal slice — the
    same contract the production engine enforces (``signals[i] ==
    strategy.analyze(bars[:i+1])``).
    """
    signal_fn = resolve_signal_fn(defn)
    params = build_params(defn)
    engine = BacktestEngine()
    backtest_config = config.backtest_config()
    strategy = _ReplayStrategy()
    full_bars = list(bars)
    full_signals = signal_fn(full_bars, params)
    if len(full_signals) != len(full_bars):
        raise ValueError(
            f"{defn.candidate_id}: provider returned {len(full_signals)} signals "
            f"for {len(full_bars)} bars"
        )
    day_offset: dict[date, int] = {}
    prev_day: date | None = None
    for idx, bar in enumerate(full_bars):
        day = bar.timestamp.date()
        if day != prev_day:
            day_offset[day] = idx
            prev_day = day
    day_groups = _group_days(full_bars, start, end)
    replays: list[DayReplay] = []
    for day_bars in day_groups:
        day = day_bars[0].timestamp.date()
        offset = day_offset[day]
        day_signals = full_signals[offset:offset + len(day_bars)]
        result = engine.run(
            day_bars,
            strategy,
            config=backtest_config,
            signals=day_signals,
        )
        records, open_count = _trade_records(day, day_signals, result.trades)
        gross = result.gross_profit + result.gross_loss
        replays.append(
            DayReplay(
                day=day,
                net_pnl=result.total_pnl,
                gross_pnl=gross,
                costs=result.total_commission + result.slippage_cost,
                trades=result.num_trades,
                wins=result.winning_trades,
                losses=result.losing_trades,
                open_at_close=open_count,
                max_drawdown_pct=float(result.max_drawdown_pct),
                signals=len(day_signals),
                records=records,
            )
        )
    return replays


def _bucketize(records: list[TradeRecord], key_fn) -> dict[str, list[TradeRecord]]:
    buckets: dict[str, list[TradeRecord]] = {}
    for record in records:
        key = key_fn(record)
        if key is None:
            key = "UNKNOWN"
        buckets.setdefault(key, []).append(record)
    return buckets


def _bucket_stats(records: list[TradeRecord]) -> dict[str, Any]:
    trades = len(records)
    net = sum((r.net_pnl for r in records), Decimal("0"))
    wins = [r for r in records if r.net_pnl > 0]
    losses = [r for r in records if r.net_pnl < 0]
    gross_profit = sum((r.net_pnl for r in wins), Decimal("0"))
    gross_loss = sum((r.net_pnl for r in losses), Decimal("0"))
    wr = (len(wins) / trades * 100.0) if trades else 0.0
    pf = (float(gross_profit / -gross_loss) if gross_loss < 0 and gross_profit > 0 else 0.0)
    return {
        "trades": trades,
        "net": net,
        "win_rate": round(wr, 2),
        "profit_factor": round(pf, 2) if math.isfinite(pf) else "inf",
    }


def analyze(
    day_replays: list[DayReplay],
    candidate_id: str,
    version: str,
    *,
    window_start: date,
    window_end: date,
    initial_capital: Decimal = Decimal("100000"),
    segments: int = 4,
) -> DiscoveryRun:
    if not day_replays:
        return DiscoveryRun(
            candidate_id=candidate_id, version=version,
            window_start=window_start, window_end=window_end,
            days=0, days_with_trades=0, trades=0, wins=0, losses=0,
            win_rate=0.0, gross_pnl=Decimal("0"), net_pnl=Decimal("0"),
            costs=Decimal("0"), profit_factor=0.0, expectancy=Decimal("0"),
            avg_win=Decimal("0"), avg_loss=Decimal("0"),
            max_drawdown_pct=0.0, sharpe=None, avg_pnl_per_day=Decimal("0"),
            trade_frequency=0.0, open_at_close_total=0, consecutive_losses=0,
            yearly={}, monthly={}, regime_breakdown={}, vol_bucket_breakdown={},
            tod_breakdown={}, side_breakdown={}, segment_nets=[], positive_segments=0,
            segments=0, quality_issues=["no days replayed in window"],
            records=[], day_replays=[],
        )

    records = [r for replay in day_replays for r in replay.records]
    days = len(day_replays)
    trades = sum(r.trades for r in day_replays)
    wins = sum(r.wins for r in day_replays)
    losses = sum(r.losses for r in day_replays)
    net = sum((r.net_pnl for r in day_replays), Decimal("0"))
    gross_pnl = sum((r.gross_pnl for r in day_replays), Decimal("0"))
    costs = sum((r.costs for r in day_replays), Decimal("0"))
    open_total = sum(r.open_at_close for r in day_replays)
    days_with_trades = sum(1 for r in day_replays if r.trades > 0)

    # profit factor / averages are per completed round trip, net of costs
    if records:
        gross_profit = sum((r.net_pnl for r in records if r.net_pnl > 0), Decimal("0"))
        gross_loss = sum((r.net_pnl for r in records if r.net_pnl < 0), Decimal("0"))
    else:
        gross_profit = gross_loss = Decimal("0")
    profit_factor = float(Decimal(gross_profit) / abs(gross_loss)) if gross_loss < 0 and gross_profit > 0 else (float("inf") if gross_profit > 0 and gross_loss == 0 else 0.0)

    win_rate = (wins / trades * 100.0) if trades else 0.0
    expectancy = (net / trades) if trades else Decimal("0")
    avg_win = (sum((r.net_pnl for r in day_replays if r.net_pnl > 0), Decimal("0")) / wins) if wins else Decimal("0")
    avg_loss = (sum((r.net_pnl for r in day_replays if r.net_pnl < 0), Decimal("0")) / losses) if losses else Decimal("0")

    # daily-equity path → max drawdown + sharpe + streaks + stability
    equity = initial_capital
    peak = initial_capital
    equity_curve: list[Decimal] = []
    for replay in day_replays:
        equity += replay.net_pnl
        equity_curve.append(equity)
        peak = max(peak, equity)
    max_drawdown_pct = float((peak - min(equity_curve)) / peak) * 100.0 if peak > 0 else 0.0

    returns = [float(replay.net_pnl / initial_capital) for replay in day_replays] if initial_capital > 0 else [0.0] * days
    mean_r = sum(returns) / days if days else 0.0
    variance = sum((r - mean_r) ** 2 for r in returns) / days if days > 1 else 0.0
    std = math.sqrt(variance)
    sharpe = (mean_r / std * math.sqrt(252.0)) if std > 1e-12 else None

    streak = 0
    longest = 0
    for replay in day_replays:
        streak = streak + 1 if replay.net_pnl < 0 else 0
        longest = max(longest, streak)
    consecutive_losses = longest

    avg_pnl_per_day = net / days if days else Decimal("0")
    trade_frequency = trades / days if days else 0.0

    # yearly / monthly
    yearly: dict[str, Decimal] = {}
    monthly: dict[str, Decimal] = {}
    for replay in day_replays:
        key_y = str(replay.day.year)
        key_m = f"{replay.day.year}-{replay.day.month:02d}"
        yearly[key_y] = yearly.get(key_y, Decimal("0")) + replay.net_pnl
        monthly[key_m] = monthly.get(key_m, Decimal("0")) + replay.net_pnl

    # record-level breakdowns
    records = [r for replay in day_replays for r in replay.records]
    regime_buckets = _bucketize(records, lambda r: r.entry_meta.get("regime"))
    vol_buckets = _bucketize(records, lambda r: r.entry_meta.get("volatility_bucket"))
    tod_buckets = _bucketize(records, lambda r: r.entry_time[:2])
    side_buckets = _bucketize(records, lambda r: r.side)
    regime_breakdown = {k: _bucket_stats(v) for k, v in sorted(regime_buckets.items())}
    vol_bucket_breakdown = {k: _bucket_stats(v) for k, v in sorted(vol_buckets.items())}
    tod_breakdown = {k: _bucket_stats(v) for k, v in sorted(tod_buckets.items())}
    side_breakdown = {k: _bucket_stats(v) for k, v in sorted(side_buckets.items())}

    # stability across contiguous segments
    seg_size = max(1, days // segments)
    seg_nets: list[Decimal] = []
    for i in range(0, days, seg_size):
        chunk = day_replays[i:i + seg_size]
        seg_nets.append(sum((r.net_pnl for r in chunk), Decimal("0")))
    positive_segments = sum(1 for s in seg_nets if s > 0)
    segment_count = len(seg_nets)

    quality: list[str] = []
    if trades < 10:
        quality.append("insufficient trades (< 10) — evidence too thin to be meaningful")
    if open_total:
        quality.append(f"{open_total} unmatched position(s) left open at day close")
    if days_with_trades and trade_frequency < 0.01:
        quality.append("no opportunities fired in most sessions")

    return DiscoveryRun(
        candidate_id=candidate_id, version=version,
        window_start=window_start, window_end=window_end,
        days=days, days_with_trades=days_with_trades, trades=trades,
        wins=wins, losses=losses, win_rate=round(win_rate, 2),
        gross_pnl=gross_pnl, net_pnl=net, costs=costs,
        profit_factor=profit_factor, expectancy=expectancy,
        avg_win=avg_win, avg_loss=avg_loss,
        max_drawdown_pct=round(max_drawdown_pct, 4), sharpe=sharpe,
        avg_pnl_per_day=avg_pnl_per_day, trade_frequency=round(trade_frequency, 4),
        open_at_close_total=open_total, consecutive_losses=consecutive_losses,
        yearly=yearly, monthly=monthly,
        regime_breakdown=regime_breakdown, vol_bucket_breakdown=vol_bucket_breakdown,
        tod_breakdown=tod_breakdown, side_breakdown=side_breakdown,
        segment_nets=seg_nets, positive_segments=positive_segments,
        segments=segment_count, quality_issues=quality,
        records=records, day_replays=day_replays,
    )