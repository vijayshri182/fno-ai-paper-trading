"""Buy-and-hold benchmark for strategy comparison.

The benchmark buys ``quantity`` units at the first bar's close and holds them
until the last bar's close. It uses the raw close prices with **no** costs and
**no** slippage so the strategy (net of its own assumptions) can be contrasted
without conflating the two. Benchmark returns are gross price returns and are
labelled as such.

The benchmark is deliberately simple: it is a reference return series, not a
trading model.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from fno_ai_paper_trading.models.market import MarketPrice
from fno_ai_paper_trading.research.metrics import _daily_returns
from fno_ai_paper_trading.utils.functions import positive_decimal, positive_int


@dataclass(frozen=True)
class BenchmarkResult:
    """Reference buy-and-hold performance (gross of costs)."""

    name: str
    quantity: int
    entry_price: Decimal
    final_price: Decimal
    final_equity: Decimal
    total_return_pct: Decimal
    max_drawdown: Decimal
    max_drawdown_pct: Decimal
    volatility_pct: Decimal | None
    exposure_pct: Decimal = Decimal("100")

    @property
    def is_benchmark(self) -> bool:
        return True


def buy_and_hold(
    bars: list[MarketPrice],
    initial_capital: Decimal,
    quantity: int,
    name: str = "buy_and_hold",
    *,
    bars_per_year: int = 252,
    min_returns: int = 10,
) -> BenchmarkResult:
    """Compute a gross buy-and-hold benchmark over ``bars``.

    Raises :class:`ValueError` if the position cannot be fully funded from
    ``initial_capital`` at the initial close price (including the instrument
    multiplier), since a partial position would complicate comparisons.
    """
    if not bars:
        raise ValueError("benchmark requires at least one bar")
    initial_capital = positive_decimal(initial_capital, "initial_capital")
    quantity = positive_int(quantity, "quantity")

    entry = bars[0].close
    final_price = bars[-1].close
    multiplier = bars[0].instrument.multiplier

    cost = entry * quantity * multiplier
    if cost > initial_capital:
        raise ValueError(
            f"buy-and-hold position notional {cost} exceeds initial capital {initial_capital}"
        )

    cash_after = initial_capital - cost
    curve_equities = [cash_after + bar.close * quantity * multiplier for bar in bars]

    final_equity = curve_equities[-1]
    total_return_pct = (final_equity - initial_capital) / initial_capital * Decimal("100")

    peak = Decimal("0")
    max_drawdown = Decimal("0")
    for equity in curve_equities:
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
    max_drawdown_pct = (max_drawdown / peak * Decimal("100")) if peak > 0 else Decimal("0")

    returns = _daily_returns_wrap(curve_equities)
    vol = _volatility(returns, bars_per_year, min_returns)

    return BenchmarkResult(
        name=name,
        quantity=quantity,
        entry_price=entry,
        final_price=final_price,
        final_equity=final_equity,
        total_return_pct=total_return_pct,
        max_drawdown=max_drawdown,
        max_drawdown_pct=max_drawdown_pct,
        volatility_pct=vol,
    )


def _daily_returns_wrap(equities: list[Decimal]) -> list[Decimal]:
    """Reuse ``_daily_returns`` (which expects objects with an ``equity`` attr)."""

    @dataclass
    class _Eq:
        equity: Decimal

    return _daily_returns([_Eq(e) for e in equities])


def _volatility(returns: list[Decimal], bars_per_year: int, min_returns: int) -> Decimal | None:
    if len(returns) < min_returns:
        return None
    mean = Decimal("0")
    if returns:
        mean = sum(returns, Decimal("0")) / Decimal(len(returns))
    variance = sum(((r - mean) ** 2 for r in returns), Decimal("0")) / Decimal(len(returns))
    if variance.is_zero() or variance < 0:
        return None
    std = variance.sqrt()
    if not std.is_finite():
        return None
    vol = std * Decimal(bars_per_year).sqrt() * Decimal("100")
    return vol if vol.is_finite() else None