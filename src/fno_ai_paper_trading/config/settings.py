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


@dataclass(frozen=True)
class KiteSettings:
    """Read-only Kite Connect market-data configuration.

    Credentials are read from the environment only. Empty values are allowed at
    load time so the rest of the system still starts without Kite; the provider
    raises ``ProviderConfigurationError`` only if a live call is attempted while
    credentials are missing.
    """

    api_key: str = ""
    access_token: str = ""
    base_url: str = "https://api.kite.trade"
    timeout_seconds: float = 10.0
    max_retries: int = 3

    def __post_init__(self) -> None:
        base_url = self.base_url.strip().rstrip("/")
        if not base_url.startswith("https://") and not base_url.startswith("http://"):
            raise ValueError("base_url must start with http:// or https://")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be > 0")
        if self.max_retries < 0:
            raise ValueError("max_retries must be >= 0")
        object.__setattr__(self, "api_key", self.api_key.strip())
        object.__setattr__(self, "access_token", self.access_token.strip())
        object.__setattr__(self, "base_url", base_url)

    @property
    def configured(self) -> bool:
        """True when API key and access token are both present."""
        return bool(self.api_key and self.access_token)


@dataclass(frozen=True)
class UpstoxSettings:
    """Read-only Upstox market-data configuration.

    Only ``access_token`` is required to call the historical-data API. The
    client id/secret are placeholders for the token-generation flow (SSO), not
    for order placement — this provider is historical-data read-only and there
    is no live-execution path here.
    """

    client_id: str = ""
    client_secret: str = ""
    access_token: str = ""
    base_url: str = "https://api.upstox.com"
    timeout_seconds: float = 10.0
    max_retries: int = 3

    def __post_init__(self) -> None:
        base_url = self.base_url.strip().rstrip("/")
        if not base_url.startswith("https://") and not base_url.startswith("http://"):
            raise ValueError("base_url must start with http:// or https://")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be > 0")
        if self.max_retries < 0:
            raise ValueError("max_retries must be >= 0")
        object.__setattr__(self, "client_id", self.client_id.strip())
        object.__setattr__(self, "client_secret", self.client_secret.strip())
        object.__setattr__(self, "access_token", self.access_token.strip())
        object.__setattr__(self, "base_url", base_url)

    @property
    def configured(self) -> bool:
        """True when an access token is present."""
        return bool(self.access_token)


def _env_decimal(name: str, default: str) -> Decimal:
    return Decimal(os.getenv(name, default).strip())


def _env_int(name: str, default: str) -> int:
    return int(os.getenv(name, default).strip())


def _env_float(name: str, default: str) -> float:
    return float(os.getenv(name, default).strip())


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


def load_kite_settings(env_file: str | Path | None = None) -> KiteSettings:
    """Load Kite Connect settings from the environment.

    Follows the same ``.env`` precedence rules as :func:`load_settings`.
    Returns an unconfigured ``KiteSettings`` when credentials are absent.
    """
    if env_file is not None:
        load_dotenv(dotenv_path=env_file)
    else:
        load_dotenv()

    return KiteSettings(
        api_key=os.getenv("FNO_KITE_API_KEY", ""),
        access_token=os.getenv("FNO_KITE_ACCESS_TOKEN", ""),
        base_url=os.getenv("FNO_KITE_BASE_URL", "https://api.kite.trade"),
        timeout_seconds=_env_float("FNO_KITE_TIMEOUT_SECONDS", "10"),
        max_retries=_env_int("FNO_KITE_MAX_RETRIES", "3"),
    )


def load_upstox_settings(env_file: str | Path | None = None) -> UpstoxSettings:
    """Load Upstox settings from the environment.

    Follows the same ``.env`` precedence rules as :func:`load_settings`.
    Returns an unconfigured ``UpstoxSettings`` when no access token is present;
    the provider raises ``ProviderConfigurationError`` only if a live call is
    attempted without one.
    """
    if env_file is not None:
        load_dotenv(dotenv_path=env_file)
    else:
        load_dotenv()

    return UpstoxSettings(
        client_id=os.getenv("UPSTOX_CLIENT_ID", ""),
        client_secret=os.getenv("UPSTOX_CLIENT_SECRET", ""),
        access_token=os.getenv("UPSTOX_ACCESS_TOKEN", ""),
        base_url=os.getenv("UPSTOX_BASE_URL", "https://api.upstox.com"),
        timeout_seconds=_env_float("UPSTOX_TIMEOUT_SECONDS", "10"),
        max_retries=_env_int("UPSTOX_MAX_RETRIES", "3"),
    )