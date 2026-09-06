"""Backtest configuration: execution assumptions and cost model.

All assumptions live here so that research experiments can swap them without
touching the engine.  The cost model mirrors :class:`PaperBrokerConfig` so
backtest fills are priced identically to live paper fills.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from fno_ai_paper_trading.utils.functions import non_negative_decimal, positive_decimal, positive_int


@dataclass(frozen=True)
class BacktestConfig:
    """Immutable configuration for a single backtest run."""

    initial_capital: Decimal = Decimal("100000")
    quantity: int = 1  # position size per signal (absolute)

    # --- cost model (mirrors PaperBrokerConfig) ---
    commission_rate: Decimal = Decimal("0.0003")  # fraction of notional
    commission_fixed: Decimal = Decimal("0")  # flat fee per fill
    slippage_rate: Decimal = Decimal("0.001")  # fraction of price per fill

    # --- risk limits ---
    enable_risk_manager: bool = True
    max_position_quantity: int = 75
    max_order_notional: Decimal = Decimal("250000")
    max_daily_loss: Decimal = Decimal("10000")

    def __post_init__(self) -> None:
        object.__setattr__(self, "initial_capital", positive_decimal(self.initial_capital, "initial_capital"))
        object.__setattr__(self, "quantity", positive_int(self.quantity, "quantity"))
        object.__setattr__(self, "commission_rate", non_negative_decimal(self.commission_rate, "commission_rate"))
        object.__setattr__(self, "commission_fixed", non_negative_decimal(self.commission_fixed, "commission_fixed"))
        object.__setattr__(self, "slippage_rate", non_negative_decimal(self.slippage_rate, "slippage_rate"))
        object.__setattr__(
            self, "max_position_quantity", positive_int(self.max_position_quantity, "max_position_quantity")
        )
        object.__setattr__(
            self, "max_order_notional", positive_decimal(self.max_order_notional, "max_order_notional")
        )
        object.__setattr__(self, "max_daily_loss", positive_decimal(self.max_daily_loss, "max_daily_loss"))
