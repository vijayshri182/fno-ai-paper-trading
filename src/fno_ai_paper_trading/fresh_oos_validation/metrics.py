"""Trade formation and net-of-cost metric computation for the fresh window.

The cost model is CENTRAL here and identical for the algorithm and the control:
adverse slippage plus commission on both the entry and the exit fill notional,
exactly matching the documented research cost schedule
(``EvaluationConfig().backtest()`` defaults: commission 0.0003/side, slippage
0.001 adverse/side).  All values are ``Decimal``; no floating point enters the
ledger.

Trades are formed from the target stream at bar-close fills (decision at bar
``i`` from ``closes[:i+1]``, fill at ``close_i``) -- the same deterministic
convention as the research harness.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Sequence

from fno_ai_paper_trading.models.market import MarketPrice
from fno_ai_paper_trading.fresh_oos_validation.protocol import (
    COMMISSION_PER_SIDE,
    QUANTITY,
    SLIPPAGE_PER_SIDE,
    TradeLeg,
)
from fno_ai_paper_trading.fresh_oos_validation.signals import FLAT, LONG, SHORT


@dataclass(frozen=True)
class TradeMetrics:
    net_pnl: Decimal
    gross_pnl: Decimal
    costs: Decimal
    trades: int
    win_rate: Decimal
    expectancy: Decimal
    profit_factor: Decimal
    max_drawdown: Decimal
    tail_loss: Decimal
    half1_net: Decimal
    half2_net: Decimal
    regime_breakdown: dict[str, int] = field(default_factory=dict)


def form_trades(bars: Sequence[MarketPrice], targets: Sequence[str]) -> list[TradeLeg]:
    """Convert a target stream into closed single-lot round-trips (bar-close fills)."""
    legs: list[TradeLeg] = []
    position: str = FLAT
    entry_index: int | None = None
    for index, bar in enumerate(bars):
        target = targets[index]
        if target == position:
            continue
        if target == FLAT and position != FLAT and entry_index is not None:
            legs.append(_close_leg(bars, entry_index, index, position, _priority_reason(index)))
            position = FLAT
            entry_index = None
            continue
        if position == FLAT and target in (LONG, SHORT):
            position = target
            entry_index = index
            continue
        if target in (LONG, SHORT) and position in (LONG, SHORT) and entry_index is not None:
            legs.append(_close_leg(bars, entry_index, index, position, "reversal"))
            position = target
            entry_index = index
    return legs


def _close_leg(bars: Sequence[MarketPrice], entry_index: int, exit_index: int, direction: str, reason: str) -> TradeLeg:
    entry_bar = bars[entry_index]
    exit_bar = bars[exit_index]
    return TradeLeg(
        entry_date=entry_bar.timestamp.date(),
        exit_date=exit_bar.timestamp.date(),
        direction=direction,
        entry_index=entry_index,
        exit_index=exit_index,
        entry_fill=entry_bar.close,
        exit_fill=exit_bar.close,
        hold_sessions=len({b.timestamp.date() for b in bars[entry_index : exit_index + 1]}),
        reason=reason,
    )


def _priority_reason(index: int) -> str:
    return "signal"


def trade_net(leg: TradeLeg, *, commission: Decimal = COMMISSION_PER_SIDE, slippage: Decimal = SLIPPAGE_PER_SIDE) -> Decimal:
    """Net P&L of one leg under the documented cost model."""
    quantity = Decimal(QUANTITY)
    entry_notional = leg.entry_fill * quantity
    exit_notional = leg.exit_fill * quantity
    if leg.direction == LONG:
        gross = (leg.exit_fill - leg.entry_fill) * quantity - slippage * (entry_notional + exit_notional)
    else:
        gross = (leg.entry_fill - leg.exit_fill) * quantity - slippage * (entry_notional + exit_notional)
    costs = commission * (entry_notional + exit_notional)
    return gross - costs


def compute_metrics(legs: Sequence[TradeLeg], *, label: str, algorithm: str = "") -> TradeMetrics:
    """Aggregate net-of-cost metrics over a deterministic trade list."""
    nets = [trade_net(leg) for leg in legs]
    grosses = [_gross(leg) for leg in legs]
    wins = [n for n in nets if n > 0]
    losses = [n for n in nets if n < 0]
    trades = len(nets)
    gross_win = sum((n for n in nets if n > 0), Decimal("0"))
    gross_loss = sum((abs(n) for n in nets if n < 0), Decimal("0"))
    gross_pnl = sum(grosses, Decimal("0"))
    costs = sum((_gross(leg) - trade_net(leg)) for leg in legs)
    net = sum(nets, Decimal("0"))
    equity: list[Decimal] = []
    run = Decimal("0")
    peak = Decimal("0")
    max_dd = Decimal("0")
    for n in nets:
        run += n
        equity.append(run)
        if run > peak:
            peak = run
        if peak > run and (peak - run) > max_dd:
            max_dd = peak - run
    tail_loss = _tail(nets)
    split = len(nets) // 2
    half1_net = sum(nets[:split], Decimal("0"))
    half2_net = sum(nets[split:], Decimal("0"))
    win_rate = (Decimal(len(wins)) / Decimal(trades)) if trades else Decimal("0")
    expectancy = (net / Decimal(trades)) if trades else Decimal("0")
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else (Decimal("0") if gross_win == 0 else Decimal("999"))
    return TradeMetrics(
        net_pnl=net,
        gross_pnl=gross_pnl,
        costs=costs,
        trades=trades,
        win_rate=win_rate,
        expectancy=expectancy,
        profit_factor=profit_factor,
        max_drawdown=max_dd,
        tail_loss=tail_loss,
        half1_net=half1_net,
        half2_net=half2_net,
    )


def _gross(leg: TradeLeg) -> Decimal:
    quantity = Decimal(QUANTITY)
    if leg.direction == LONG:
        return (leg.exit_fill - leg.entry_fill) * quantity
    return (leg.entry_fill - leg.exit_fill) * quantity


def _tail(nets: list[Decimal]) -> Decimal:
    if not nets:
        return Decimal("0")
    ordered = sorted(nets)
    cutoff = max(1, (len(ordered) + 4) // 5)
    return sum(ordered[:cutoff], Decimal("0")) / Decimal(cutoff)


def regime_label(day_bars: Sequence[MarketPrice]) -> str:
    """Deterministic entry-day regime label: RISING / SIDEWAYS / FALLING.

    Uses the same MA-gap trend convention as the walk detector (20/60 MA gap
    threshold +-0.05%): a day whose session closes are above the slow MA by more
    than 0.05% is RISING, below the slow MA by more than 0.05% is FALLING, else
    SIDEWAYS.  Decision-time only.
    """
    if not day_bars:
        return "UNCLASSIFIED"
    closes = [bar.close for bar in day_bars]
    slow_ma = sum(closes[-21:], Decimal("0")) / Decimal(min(21, len(closes)))
    if slow_ma == 0:
        return "UNCLASSIFIED"
    gap = (closes[-1] - slow_ma) / slow_ma
    threshold = Decimal("0.0005")
    if gap > threshold:
        return "RISING"
    if gap < -threshold:
        return "FALLING"
    return "SIDEWAYS"


def compare(ours: TradeMetrics, control: TradeMetrics) -> dict[str, object]:
    return {
        "trades_delta": ours.trades - control.trades,
        "net_delta": str(ours.net_pnl - control.net_pnl),
        "expectancy_delta": str(ours.expectancy - control.expectancy),
        "net_pnl_outperforms_control": ours.net_pnl > control.net_pnl,
        "profit_factor_gte_1_0": ours.profit_factor >= Decimal("1.0"),
    }