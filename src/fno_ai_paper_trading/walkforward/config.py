"""Walk-forward adaptive research & learning engine configuration (WS 7.18).

The engine replays trading days chronologically. At day D the algorithm used is
frozen *before* D is evaluated; learning from D only affects D + 1 or a later
day, and only after the explicit challenger/promotion gate permits it. Every
knob that can change the historical walk lives in this one immutable object so a
run is fully reproducible -- ``config_hash`` fingerprints the whole
configuration and is recorded in every artifact and in the resume checkpoint.

The engine is a paper/historical research machine: it imports no execution code,
never enables live trading, and can never weaken the RiskManager / sizing /
stop-loss / PaperBroker / Portfolio safety layers.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal

from fno_ai_paper_trading.backtest.config import BacktestConfig
from fno_ai_paper_trading.utils.functions import non_negative_decimal, positive_decimal, positive_int

FRAMEWORK_VERSION = "walkforward-1.0"


def _json_default(value: object) -> str:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, date):
        return value.isoformat()
    raise TypeError(f"cannot serialize {type(value).__name__}")


@dataclass(frozen=True)
class WalkForwardConfig:
    """All reproducible knobs of a walk-forward research run."""

    # ---- domain bounds ----------------------------------------------------
    first_date: date | None = None
    last_date: date | None = None
    # Hard top bound: days >= protected_oos_start are never loaded or processed.
    protected_oos_start: date | None = None
    # Optional day budget (a runtime bound for smoke runs; NOT part of the
    # fingerprint semantics of the research window itself).
    max_research_days: int | None = None

    # ---- shared cost / execution assumptions (mirror BacktestConfig) ------
    initial_capital: Decimal = Decimal("100000")
    quantity: int = 1
    commission_rate: Decimal = Decimal("0.0003")
    commission_fixed: Decimal = Decimal("0")
    slippage_rate: Decimal = Decimal("0.001")
    enable_risk_manager: bool = True
    max_position_quantity: int = 75
    max_order_notional: Decimal = Decimal("250000")
    max_daily_loss: Decimal = Decimal("10000")
    enable_stop_loss: bool = True
    stop_loss_pct: Decimal = Decimal("0.02")

    # ---- evidence thresholds & research cadence ---------------------------
    # Minimum champion evidence (in trading days) before the first research round.
    min_evidence_days: int = 20
    # How often (in trading days) a new research round may generate challengers.
    research_cadence_days: int = 20
    # Trailing evidence window (in trading days) used by a research round.
    research_window_days: int = 60
    # Trailing window for the nightly problem review / evidence snapshot.
    nightly_review_window_days: int = 20
    # Minimum completed trades in a regime bucket before a hypothesis may fire.
    min_hypothesis_regime_trades: int = 8
    # Anti-overfitting budget caps.
    max_challengers_per_round: int = 1
    max_challengers_total: int = 5

    # ---- challenger validation --------------------------------------------
    # Future-only validation window assigned to every generated challenger.
    validation_window_days: int = 20
    # How often (in trading days) the promotion gate may run.
    promotion_cadence_days: int = 10

    # ---- promotion gate ----------------------------------------------------
    min_validation_trades: int = 10
    min_profit_factor: Decimal = Decimal("1.0")
    max_drawdown_pct: Decimal = Decimal("5")
    max_consecutive_losing_days: int = 8
    max_tail_loss_pct: Decimal = Decimal("10")
    min_net_pnl_per_cost: Decimal = Decimal("1.0")
    max_trades_per_day: Decimal = Decimal("10")
    min_regime_trades: int = 5
    regime_degradation_tolerance: Decimal = Decimal("0.75")

    def __post_init__(self) -> None:
        if self.first_date is not None and self.last_date is not None:
            if self.last_date < self.first_date:
                raise ValueError("last_date must be >= first_date")
        if (
            self.protected_oos_start is not None
            and self.last_date is not None
            and self.last_date >= self.protected_oos_start
        ):
            raise ValueError("last_date must be strictly before protected_oos_start")
        if self.max_research_days is not None:
            positive_int(self.max_research_days, "max_research_days")
        for name in (
            "min_evidence_days",
            "research_cadence_days",
            "research_window_days",
            "nightly_review_window_days",
            "min_hypothesis_regime_trades",
            "max_challengers_per_round",
            "max_challengers_total",
            "validation_window_days",
            "promotion_cadence_days",
            "min_validation_trades",
            "min_regime_trades",
        ):
            positive_int(getattr(self, name), name)
        if self.max_challengers_per_round > self.max_challengers_total:
            raise ValueError("max_challengers_per_round must be <= max_challengers_total")
        object.__setattr__(
            self, "initial_capital", positive_decimal(self.initial_capital, "initial_capital")
        )
        object.__setattr__(self, "quantity", positive_int(self.quantity, "quantity"))
        object.__setattr__(
            self, "commission_rate", non_negative_decimal(self.commission_rate, "commission_rate")
        )
        object.__setattr__(
            self, "commission_fixed", non_negative_decimal(self.commission_fixed, "commission_fixed")
        )
        object.__setattr__(
            self, "slippage_rate", non_negative_decimal(self.slippage_rate, "slippage_rate")
        )
        object.__setattr__(
            self, "max_position_quantity", positive_int(self.max_position_quantity, "max_position_quantity")
        )
        object.__setattr__(
            self, "max_order_notional", positive_decimal(self.max_order_notional, "max_order_notional")
        )
        object.__setattr__(self, "max_daily_loss", positive_decimal(self.max_daily_loss, "max_daily_loss"))
        if self.enable_stop_loss:
            stop = positive_decimal(self.stop_loss_pct, "stop_loss_pct")
            if stop >= 1:
                raise ValueError("stop_loss_pct must be < 1")
            object.__setattr__(self, "stop_loss_pct", stop)
        for name in (
            "min_profit_factor",
            "max_drawdown_pct",
            "max_tail_loss_pct",
            "min_net_pnl_per_cost",
            "max_trades_per_day",
            "regime_degradation_tolerance",
        ):
            object.__setattr__(self, name, non_negative_decimal(getattr(self, name), name))

    @property
    def config_hash(self) -> str:
        """Deterministic fingerprint of every knob that changes the walk."""
        canonical = json.dumps(
            asdict(self), sort_keys=True, separators=(",", ":"), default=_json_default
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def backtest_config(self) -> BacktestConfig:
        """Shared, deterministic execution assumptions for every daily replay."""
        return BacktestConfig(
            initial_capital=self.initial_capital,
            quantity=self.quantity,
            commission_rate=self.commission_rate,
            commission_fixed=self.commission_fixed,
            slippage_rate=self.slippage_rate,
            enable_risk_manager=self.enable_risk_manager,
            max_position_quantity=self.max_position_quantity,
            max_order_notional=self.max_order_notional,
            max_daily_loss=self.max_daily_loss,
            enable_stop_loss=self.enable_stop_loss,
            stop_loss_pct=self.stop_loss_pct,
        )

    def to_dict(self) -> dict[str, object]:
        return json.loads(json.dumps(asdict(self), sort_keys=True, default=_json_default))