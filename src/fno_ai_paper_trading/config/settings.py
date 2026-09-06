"""Application configuration.

Configuration is environment-variable based (with sensible paper-trading
defaults). Real secrets belong in ``.env`` (git-ignored) or the environment;
this module never hard-codes credentials. The name prefix ``FNO_`` is used for
all variables.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from pathlib import Path

from dotenv import load_dotenv

from fno_ai_paper_trading.utils.functions import non_negative_decimal, positive_decimal, positive_int


class Environment(str, Enum):
    """Defines the supported run environments.

    This system is paper-trading only; all environments forbid live execution.
    """

    DEVELOPMENT = "development"
    TEST = "test"
    PAPER = "paper"

    @classmethod
    def parse(cls, value: str) -> "Environment":
        try:
            return cls(value.strip().lower())
        except ValueError as exc:
            raise ValueError(
                f"unknown environment '{value}'; expected one of {[e.value for e in cls]}"
            ) from exc


@dataclass(frozen=True)
class PaperSettings:
    """Typed, validated runtime settings for the paper trading system.

    All money/price values are stored as :class:`decimal.Decimal`.
    """

    environment: Environment
    initial_capital: Decimal
    max_position_quantity: int
    max_order_notional: Decimal
    max_daily_loss: Decimal
    commission_rate: Decimal  # fraction of notional, e.g. 0.0003 = 0.03%
    commission_fixed: Decimal  # flat fee per fill
    slippage_rate: Decimal  # fraction of price applied per fill, e.g. 0.001 = 0.1%
    log_level: str = "INFO"

    def __post_init__(self) -> None:
        object.__setattr__(self, "initial_capital", positive_decimal(self.initial_capital, "initial_capital"))
        object.__setattr__(self, "max_position_quantity", positive_int(self.max_position_quantity, "max_position_quantity"))
        object.__setattr__(self, "max_order_notional", positive_decimal(self.max_order_notional, "max_order_notional"))
        object.__setattr__(self, "max_daily_loss", positive_decimal(self.max_daily_loss, "max_daily_loss"))
        object.__setattr__(self, "commission_rate", non_negative_decimal(self.commission_rate, "commission_rate"))
        object.__setattr__(self, "commission_fixed", non_negative_decimal(self.commission_fixed, "commission_fixed"))
        object.__setattr__(self, "slippage_rate", non_negative_decimal(self.slippage_rate, "slippage_rate"))


def _env_decimal(name: str, default: str) -> Decimal:
    return Decimal(os.getenv(name, default).strip())


def _env_int(name: str, default: str) -> int:
    return int(os.getenv(name, default).strip())


def load_settings(env_file: str | Path | None = None) -> PaperSettings:
    """Load settings from ``.env``/``env_file`` and environment variables.

    If ``env_file`` is ``None``, ``.env`` in the current directory is loaded
    (if present). Values are read from the process environment, so exported
    variables take precedence.
    """
    if env_file is not None:
        load_dotenv(dotenv_path=env_file)
    else:
        load_dotenv()

    environment = Environment.parse(os.getenv("FNO_ENVIRONMENT", Environment.DEVELOPMENT.value))
    log_level = os.getenv("FNO_LOG_LEVEL", "INFO").strip().upper()

    return PaperSettings(
        environment=environment,
        initial_capital=_env_decimal("FNO_PAPER_INITIAL_CAPITAL", "100000"),
        max_position_quantity=_env_int("FNO_PAPER_MAX_POSITION_QUANTITY", "75"),
        max_order_notional=_env_decimal("FNO_PAPER_MAX_ORDER_NOTIONAL", "250000"),
        max_daily_loss=_env_decimal("FNO_PAPER_MAX_DAILY_LOSS", "10000"),
        commission_rate=_env_decimal("FNO_PAPER_COMMISSION_RATE", "0.0003"),
        commission_fixed=_env_decimal("FNO_PAPER_COMMISSION_FIXED", "0"),
        slippage_rate=_env_decimal("FNO_PAPER_SLIPPAGE_RATE", "0.001"),
        log_level=log_level,
    )