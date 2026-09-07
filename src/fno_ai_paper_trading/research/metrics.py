"""Robust performance metrics computed from a :class:`BacktestResult`.

All metrics are deterministic ``Decimal`` values. Metrics that cannot be
meaningfully computed for a given dataset return ``None`` instead of an
invented number (e.g. Sharpe ratio when there is no return variance, CAGR when
the period is too short, expectancy when there are no trades).

Documented assumptions
----------------------
* Bars are treated as *daily* for annualization purposes by default
  (``bars_per_year=252``); supply a different value for other bar cadences.
* The risk-free rate is an annualized rate, converted to a per-bar rate with
  simple division (``risk_free_rate / bars_per_year``).
* Volatility, Sharpe and Sortino use *population* standard deviation of daily
  equity returns, annualized by ``sqrt(bars_per_year)``.
* Sharpe/Sortino/volatility require at least ``min_returns`` daily returns
  (default 10) — smaller samples yield ``None``.
* CAGR requires an elapsed period of at least ``min_years_for_cagr``
  (default 0.1 years); otherwise it is ``None``.
* Exposure/time-in-market is the fraction of bars during which any position is
  open, expressed as a percentage.
* ``slippage_cost`` and ``total_commission`` are reported separately so friction
  can be attributed; ``transaction_costs`` is their sum. ``net_pnl`` already
  nets out both (it equals final equity minus initial capital).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation

from fno_ai_paper_trading.backtest.result import BacktestResult, EquityPoint
from fno_ai_paper_trading.models.enums import OrderSide
from fno_ai_paper_trading.utils.functions import non_negative_int, positive_int, to_decimal

_DAYS_PER_YEAR = Decimal("365.25")
_SECONDS_PER_DAY = Decimal(24 * 60 * 60)
_MIN_YEARS_FOR_CAGR = Decimal("0.1")


@dataclass(frozen=True)
class PerformanceMetrics:
    """Metrics derived from a completed backtest run."""

    initial_capital: Decimal
    final_equity: Decimal
    net_pnl: Decimal
    net_return_pct: Decimal
    cagr_pct: Decimal | None
    max_drawdown: Decimal
    max_drawdown_pct: Decimal
    drawdown_duration_bars: int
    num_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: Decimal
    profit_factor: Decimal
    avg_winning_trade: Decimal | None
    avg_losing_trade: Decimal | None
    expectancy: Decimal | None
    total_commission: Decimal
    slippage_cost: Decimal
    gross_profit: Decimal
    gross_loss: Decimal
    annualized_volatility: Decimal | None
    sharpe_ratio: Decimal | None
    sortino_ratio: Decimal | None
    exposure_pct: Decimal
    bars_in_market: int
    total_bars: int

    @property
    def transaction_costs(self) -> Decimal:
        """Total broker price of trading: slippage + commission."""
        return self.total_commission + self.slippage_cost


def compute_metrics(
    result: BacktestResult,
    *,
    bars_per_year: int = 252,
    risk_free_rate: int | float | str | Decimal = 0,
    min_returns: int = 10,
    min_years_for_cagr: int | float | str | Decimal = _MIN_YEARS_FOR_CAGR,
) -> PerformanceMetrics:
    """Compute :class:`PerformanceMetrics` from a backtest result.

    Raises only for absurd inputs (e.g. non-positive ``bars_per_year``). It
    never raises for degenerate results — unavailable metrics become ``None``.
    """
    bars_per_year = positive_int(bars_per_year, "bars_per_year")
    min_returns = positive_int(min_returns, "min_returns")
    min_years = Decimal(min_years_for_cagr)
    rf_daily = to_decimal(risk_free_rate) / Decimal(bars_per_year)

    initial = result.initial_capital
    final_equity = result.final_equity
    net_pnl = result.total_pnl
    net_return_pct = result.total_return_pct
    curve = result.equity_curve

    max_drawdown = result.max_drawdown
    max_drawdown_pct = result.max_drawdown_pct
    duration = _max_drawdown_duration(curve)

    avg_win, avg_loss, expectancy = _trade_expectancy(result)

    rets = _daily_returns(curve)
    vol, sharpe, sortino = _annualized_stats(rets, rf_daily, bars_per_year, min_returns)
    cagr = _cagr_pct(result, min_years)

    bars_in_market, exposure_pct = _exposure(result)

    return PerformanceMetrics(
        initial_capital=initial,
        final_equity=final_equity,
        net_pnl=net_pnl,
        net_return_pct=net_return_pct,
        cagr_pct=cagr,
        max_drawdown=max_drawdown,
        max_drawdown_pct=max_drawdown_pct,
        drawdown_duration_bars=duration,
        num_trades=result.num_trades,
        winning_trades=result.winning_trades,
        losing_trades=result.losing_trades,
        win_rate=result.win_rate,
        profit_factor=result.profit_factor,
        avg_winning_trade=avg_win,
        avg_losing_trade=avg_loss,
        expectancy=expectancy,
        total_commission=result.total_commission,
        slippage_cost=result.slippage_cost,
        gross_profit=result.gross_profit,
        gross_loss=result.gross_loss,
        annualized_volatility=vol,
        sharpe_ratio=sharpe,
        sortino_ratio=sortino,
        exposure_pct=exposure_pct,
        bars_in_market=bars_in_market,
        total_bars=len(curve),
    )


def _max_drawdown_duration(curve: list[EquityPoint]) -> int:
    """Longest stretch of consecutive bars below the running peak."""
    if not curve:
        return 0
    peak = curve[0].equity
    duration = 0
    longest = 0
    for point in curve:
        if point.equity >= peak:
            peak = point.equity
            duration = 0
        else:
            duration += 1
            longest = max(longest, duration)
    return longest


def _trade_expectancy(result: BacktestResult) -> tuple[Decimal | None, Decimal | None, Decimal | None]:
    """Average winning trade, average losing trade, and per-trade expectancy."""
    avg_win: Decimal | None = None
    avg_loss: Decimal | None = None
    expectancy: Decimal | None = None
    if result.winning_trades > 0:
        avg_win = result.gross_profit / Decimal(result.winning_trades)
    if result.losing_trades > 0:
        avg_loss = result.gross_loss / Decimal(result.losing_trades)
    if result.num_trades > 0:
        if avg_win is not None:
            win_part = result.win_rate / Decimal("100") * avg_win
        else:
            win_part = Decimal("0")
        if avg_loss is not None:
            loss_part = (Decimal("100") - result.win_rate) / Decimal("100") * avg_loss
        else:
            loss_part = Decimal("0")
        expectancy = win_part + loss_part
    return avg_win, avg_loss, expectancy


def _daily_returns(curve: list[EquityPoint]) -> list[Decimal]:
    returns: list[Decimal] = []
    for previous, current in zip(curve, curve[1:]):
        if previous.equity > 0:
            returns.append(current.equity / previous.equity - 1)
    return returns


def _annualized_stats(
    returns: list[Decimal],
    rf_daily: Decimal,
    bars_per_year: int,
    min_returns: int,
) -> tuple[Decimal | None, Decimal | None, Decimal | None]:
    if len(returns) < min_returns:
        return None, None, None
    mean = sum(returns, Decimal("0")) / Decimal(len(returns))
    n = Decimal(len(returns))
    variance = sum(((r - mean) ** 2 for r in returns), Decimal("0")) / n
    if variance.is_zero():
        return None, None, None
    std = variance.sqrt()
    if not std.is_finite() or std <= 0:
        return None, None, None

    annual_vol = std * Decimal(bars_per_year).sqrt()
    excess = mean - rf_daily
    sharpe = (excess / std) * Decimal(bars_per_year).sqrt()

    downside = [min(r - rf_daily, Decimal("0")) for r in returns]
    downside_var = sum((d ** 2 for d in downside), Decimal("0")) / n
    if downside_var <= 0:
        downside_std: Decimal | None = None
    else:
        downside_std = downside_var.sqrt()
    sortino: Decimal | None = None
    if downside_std is not None and not downside_std.is_zero():
        sortino = (excess / downside_std) * Decimal(bars_per_year).sqrt()

    if not annual_vol.is_finite():
        annual_vol = None
    if sharpe is not None and not sharpe.is_finite():
        sharpe = None
    if sortino is not None and not sortino.is_finite():
        sortino = None
    return annual_vol, sharpe, sortino


def _cagr_pct(result: BacktestResult, min_years: Decimal) -> Decimal | None:
    curve = result.equity_curve
    if len(curve) < 2:
        return None
    years = _elapsed_years(curve[0].timestamp, curve[-1].timestamp)
    if years <= 0 or years < min_years:
        return None
    initial = result.initial_capital
    final_equity = result.final_equity
    if initial <= 0 or final_equity <= 0:
        return None
    ratio = final_equity / initial
    try:
        cagr = (ratio.ln() / years).exp() - 1
    except (InvalidOperation, ValueError, ArithmeticError):
        return None
    if cagr.is_finite():
        return cagr * Decimal("100")
    return None


def _elapsed_years(start: datetime, end: datetime) -> Decimal:
    seconds = (end - start).total_seconds()
    return Decimal(str(seconds)) / (_SECONDS_PER_DAY * _DAYS_PER_YEAR)


def _exposure(result: BacktestResult) -> tuple[int, Decimal]:
    """Bars with an open position, and that count as a percentage of all bars."""
    curve = result.equity_curve
    if not curve:
        return 0, Decimal("0")

    deltas: dict[datetime, int] = {}
    for trade in result.trades:
        qty = trade.quantity if trade.side == OrderSide.BUY else -trade.quantity
        ts = trade.executed_at
        deltas[ts] = deltas.get(ts, 0) + qty

    quantity = 0
    bars_in_market = 0
    for point in curve:
        quantity += deltas.get(point.timestamp, 0)
        if quantity != 0:
            bars_in_market += 1
    return bars_in_market, Decimal(bars_in_market) / Decimal(len(curve)) * Decimal("100")