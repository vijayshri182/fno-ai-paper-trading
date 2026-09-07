"""Execution assumptions for backtest research.

A deterministic, single-fraction model that combines the components of the
effective fill price:

* ``slippage_rate`` — the base execution slippage applied to the bar close
* ``half_spread_rate`` — half the quoted bid/ask spread (as a fraction)
* ``impact_rate`` — a simple market-impact proxy (as a fraction)

The total adverse rate is applied exactly like the backtest engine's existing
slippage: buys fill at ``price * (1 + total_adverse_rate)`` and sells at
``price * (1 - total_adverse_rate)``. Keeping the combined fraction explicit
and deterministic prevents zero-friction results without pretending to model
order books.

The default values are illustrative inputs for research, not claims about any
market's real spread or impact.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from fno_ai_paper_trading.utils.functions import non_negative_decimal


@dataclass(frozen=True)
class ExecutionAssumptions:
    """Deterministic execution-price assumptions for one backtest run."""

    slippage_rate: Decimal = Decimal("0.0005")  # base execution slippage
    half_spread_rate: Decimal = Decimal("0")  # half the quoted spread
    impact_rate: Decimal = Decimal("0")  # simple market-impact proxy

    def __post_init__(self) -> None:
        object.__setattr__(self, "slippage_rate", non_negative_decimal(self.slippage_rate, "slippage_rate"))
        object.__setattr__(self, "half_spread_rate", non_negative_decimal(self.half_spread_rate, "half_spread_rate"))
        object.__setattr__(self, "impact_rate", non_negative_decimal(self.impact_rate, "impact_rate"))

    @property
    def total_adverse_rate(self) -> Decimal:
        """Combined adverse-price fraction used by the backtest engine."""
        return self.slippage_rate + self.half_spread_rate + self.impact_rate