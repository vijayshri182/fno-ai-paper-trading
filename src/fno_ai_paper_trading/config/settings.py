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
    # Live paper-session settings (v1: 5-minute cadence, long-only index).
    paper_interval: str = "5m"  # canonical bar interval token for the session loop
    paper_lookback_days: int = 3  # how many calendar days of bars each fetch pulls
    paper_risk_per_trade_pct: Decimal = Decimal("0.01")  # 1% of current equity risked per trade
    paper_stop_loss_pct: Decimal = Decimal("0.02")  # fixed 2% stop distance from entry price
    paper_state_dir: str = "paper_state"  # git-ignored ledger/snapshot directory

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
    """Read-only Upstox market-data configuration (**analytics/data credential**).

    This object belongs exclusively to the analytics / market-data layer: it is
    loaded from the ``FNO_UPSTOX_*`` environment variables and is never passed
    to the WS 7.9 execution adapter. The execution path consumes its own
    ``UPSTOX_ACCESS_TOKEN`` directly and must **never** fall back to this value;
    equally, this layer never reads ``UPSTOX_ACCESS_TOKEN`` as a fallback.

    Only ``access_token`` is required to call the historical-data API. The
    client id/secret are placeholders for the token-generation flow (SSO), not
    for order placement — this provider is historical-data read-only and there
    is no live-execution path here.
    """

    client_id: str = ""
    client_secret: str = ""
    access_token: str = ""
    api_key: str = ""
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
        object.__setattr__(self, "api_key", self.api_key.strip())
        object.__setattr__(self, "base_url", base_url)

    @property
    def configured(self) -> bool:
        """True when an access token is present."""
        return bool(self.access_token)


@dataclass(frozen=True)
class LiveExecutionTestSettings:
    """Configuration for the controlled Upstox live F&O execution integration
    test (WS 7.9 — execution layer).

    This capability is **off by default**. Nothing here authorizes a real
    order: the manager additionally requires an operator-authored consent file
    and a matching in-memory access token before any order write (see
    :mod:`fno_ai_paper_trading.execution.gate`). Secrets are never stored in
    this dataclass — tokens stay in the process environment only.

    Fields:

    * ``enabled``               -- master enable flag (env, default ``0``).
    * ``consent_file``          -- operator-authored consent file path.
    * ``hold_seconds``          -- mandatory hold between entry fill and exit.
    * ``expiry_hours``          -- how long a signed consent file is valid.
    * ``dry_run``               -- default True: the adapter never POSTs orders.
    * ``max_margin_notional``   -- client-side margin sanity cap (rupees).
    * ``order_timeout_seconds`` -- how long to poll an order for a status.
    * ``poll_seconds``          -- pause between order-status polls.
    """

    enabled: bool = False
    consent_file: str = "reports/execution/operator_consent.json"
    hold_seconds: int = 300
    expiry_hours: float = 24.0
    dry_run: bool = True
    max_margin_notional: Decimal = Decimal("100000")
    order_timeout_seconds: float = 30.0
    poll_seconds: float = 2.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "consent_file", (self.consent_file or "").strip())
        if not self.consent_file:
            raise ValueError("consent_file must not be empty")
        object.__setattr__(self, "hold_seconds", positive_int(self.hold_seconds, "hold_seconds"))
        object.__setattr__(self, "expiry_hours", positive_decimal(str(self.expiry_hours), "expiry_hours"))
        object.__setattr__(self, "max_margin_notional", positive_decimal(self.max_margin_notional, "max_margin_notional"))
        if self.order_timeout_seconds <= 0:
            raise ValueError("order_timeout_seconds must be > 0")
        if self.poll_seconds < 0:
            raise ValueError("poll_seconds must be >= 0")


def _env_decimal(name: str, default: str) -> Decimal:
    return Decimal(os.getenv(name, default).strip())


def _env_int(name: str, default: str) -> int:
    return int(os.getenv(name, default).strip())


def _env_float(name: str, default: str) -> float:
    return float(os.getenv(name, default).strip())


def _env_bool(name: str, default: str) -> bool:
    value = os.getenv(name, default).strip().lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"expected a boolean for {name}, got {value!r}")


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
    """Load Upstox analytics/data settings from the environment.

    Follows the same ``.env`` precedence rules as :func:`load_settings`.

    Reads the **analytics/data-scoped** ``FNO_UPSTOX_*`` variables only. The
    WS 7.9 execution token (``UPSTOX_ACCESS_TOKEN``) is deliberately not read
    here: keeping the two credential streams separate means paper trading and
    research never require the execution token, and the execution adapter never
    reuses this object.

    Returns an unconfigured ``UpstoxSettings`` when no access token is present;
    the provider raises ``ProviderConfigurationError`` only if a live call is
    attempted without one.
    """
    if env_file is not None:
        load_dotenv(dotenv_path=env_file)
    else:
        load_dotenv()

    return UpstoxSettings(
        client_id=os.getenv("FNO_UPSTOX_CLIENT_ID", ""),
        client_secret=os.getenv("FNO_UPSTOX_CLIENT_SECRET", ""),
        access_token=os.getenv("FNO_UPSTOX_ACCESS_TOKEN", ""),
        api_key=os.getenv("FNO_UPSTOX_API_KEY", ""),
        base_url=os.getenv("FNO_UPSTOX_BASE_URL", "https://api.upstox.com"),
        timeout_seconds=_env_float("FNO_UPSTOX_TIMEOUT_SECONDS", "10"),
        max_retries=_env_int("FNO_UPSTOX_MAX_RETRIES", "3"),
    )


def load_live_test_settings(env_file: str | Path | None = None) -> LiveExecutionTestSettings:
    """Load the live-execution-test settings from the environment.

    Follows the same ``.env`` precedence rules as :func:`load_settings`. The
    access token is deliberately **not** read here — it is fetched from the
    environment at the point of use by the gate/adapter so it is never stored.
    All live-execution-test knobs default to safe/off values.
    """
    if env_file is not None:
        load_dotenv(dotenv_path=env_file)
    else:
        load_dotenv()

    return LiveExecutionTestSettings(
        enabled=_env_bool("FNO_LIVE_EXECUTION_TEST_ENABLED", "0"),
        consent_file=os.getenv("FNO_LIVE_TEST_CONSENT_FILE", "reports/execution/operator_consent.json"),
        hold_seconds=_env_int("FNO_LIVE_TEST_HOLD_SECONDS", "300"),
        expiry_hours=_env_float("FNO_LIVE_TEST_EXPIRY_HOURS", "24"),
        dry_run=_env_bool("FNO_LIVE_TEST_DRY_RUN", "1"),
        max_margin_notional=_env_decimal("FNO_LIVE_TEST_MAX_MARGIN", "100000"),
        order_timeout_seconds=_env_float("FNO_LIVE_TEST_ORDER_TIMEOUT_SECONDS", "30"),
        poll_seconds=_env_float("FNO_LIVE_TEST_POLL_SECONDS", "2"),
    )