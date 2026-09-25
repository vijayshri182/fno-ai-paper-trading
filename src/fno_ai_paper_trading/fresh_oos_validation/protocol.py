"""Canonical protocol constants, frozen parameter pins and result types.

The single-use validation job is the *only* consumer of the fresh-OOS pool
after 20 trading days / 1500 bars are collected.  This module pins:

* the frozen OUR-ALGO-004 parameters (Ledger Entry 004; byte-exact),
* the frozen MA(5,21) control parameters,
* the documented research cost model,
* the result/outcome types every report is serialised with.

Nothing here touches the network or imports any strategy/execution module.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, asdict
from datetime import date, datetime
from decimal import Decimal
from typing import Mapping

from fno_ai_paper_trading.fresh_oos.protocol import FRESH_OOS_BOUNDARY, MIN_TRADES

PROTOCOL_NAME = "fresh_oos_single_use_validation"
PROTOCOL_VERSION = 1

# Frozen OUR-ALGO-004 parameters (Ledger Entry 004 -- do not change, ever).
ALGO_NAME = "OUR-ALGO-004"
FROZEN_ALGO_PARAMS: Mapping[str, object] = {
    "fast": 9,
    "slow": 26,
    "slope_window": 5,
    "lookback": 20,
    "stop_atr_mult": 4.0,
    "max_hold_days": 25,
    "warmup": 54,
}

CONTROL_NAME = "MA(5,21)"
FROZEN_CONTROL_PARAMS: Mapping[str, object] = {"fast": 5, "slow": 21}

# Documented cost model -- mirrors the research stack's canonical schedule
# (``evaluation.records.EvaluationConfig`` defaults; its ``.backtest()`` builds
# the identical ``BacktestConfig``): initial_capital 100000, quantity 1,
# commission 0.0003/side (commission_fixed 0), slippage 0.001 adverse/side,
# fills at the bar close. The validation job applies the schedule centrally so
# the algorithm and the control share ONE cost model.
#
# Research-parity nuance (documented in EVIDENCE_LIMITATIONS): the backtest
# engine prices every fill at the slip-adjusted price and therefore charges
# commission on slip-adjusted notional -- a second-order r*s*(E+X) larger than
# this central schedule at qty 1. The delta is exactly quantifiable and is
# pinned by a guard test.
CAPITAL = 100_000
QUANTITY = 1
COMMISSION_PER_SIDE = Decimal("0.0003")
SLIPPAGE_PER_SIDE = Decimal("0.001")

# Fresh-window acceptance statuses that count toward the consumable pool.
ACCEPTED_POOL_STATUSES = frozenset({"ACQUIRED", "NOOP", "NOOP_ESTABLISHED"})

# Verdict statuses for a single-use validation outcome.
STATUS_REFUSED = "REFUSED"
STATUS_CONSUMED_ALREADY = "CONSUMED_ALREADY"
STATUS_NOT_READY = "NOT_READY"
STATUS_SUCCESS = "SUCCESS"
STATUS_FAILED = "FAILED"


def _stable_json_dumps(data: object) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)


def params_fingerprint(params: Mapping[str, object]) -> str:
    """Stable SHA-256 over the frozen parameter mapping (sorted keys)."""
    return hashlib.sha256(_stable_json_dumps(dict(params)).encode("utf-8")).hexdigest()


ALGO_FINGERPRINT = params_fingerprint(FROZEN_ALGO_PARAMS)
CONTROL_FINGERPRINT = params_fingerprint(FROZEN_CONTROL_PARAMS)


def assert_frozen_params(params: Mapping[str, object], name: str, fingerprint: str) -> None:
    """Refuse to proceed unless the supplied parameters are the frozen literals."""
    if params_fingerprint(dict(params)) != fingerprint:
        raise ValueError(
            f"{name} parameters are not the frozen literals: expected fingerprint "
            f"{fingerprint}, got {params_fingerprint(dict(params))}"
        )


@dataclass(frozen=True)
class TradeLeg:
    """One closed round-trip from the replay engines (bar-timestamp fills, qty 1)."""

    entry_date: date
    exit_date: date
    direction: str          # "LONG" | "SHORT"
    entry_index: int
    exit_index: int
    entry_fill: Decimal
    exit_fill: Decimal
    hold_sessions: int
    reason: str             # why the position closed

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class MetricsReport:
    """Net-of-cost metrics for one run over the fresh pool."""

    algorithm: str
    params_fingerprint: str
    trades: int
    win_rate: Decimal
    net_pnl: Decimal
    gross_pnl: Decimal
    costs: Decimal
    expectancy: Decimal
    profit_factor: Decimal
    max_drawdown: Decimal
    tail_loss: Decimal
    half1_net: Decimal
    half2_net: Decimal
    regime_breakdown: Mapping[str, object] = field(default_factory=dict)
    insufficient_sample: bool = False

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class ValidationOutcome:
    """Machine-readable result of one controlled single-use validation."""

    run_id: str
    status: str
    consumed: bool
    consumed_at: str | None
    dates: tuple[str, ...]
    bars: int
    per_day_hashes: Mapping[str, str]
    algorithm: str
    algorithm_fingerprint: str
    control: str
    control_fingerprint: str
    cost_model: Mapping[str, object]
    algorithm_metrics: MetricsReport | None = None
    control_metrics: MetricsReport | None = None
    comparison: Mapping[str, object] = field(default_factory=dict)
    evidence_limitations: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    message: str = ""

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "ValidationOutcome":
        def _metric(raw: object) -> MetricsReport | None:
            if raw is None:
                return None
            m = MetricsReport(**{k: v for k, v in dict(raw).items() if k in MetricsReport.__dataclass_fields__})
            return m

        return cls(
            run_id=str(data["run_id"]),
            status=str(data["status"]),
            consumed=bool(data["consumed"]),
            consumed_at=data.get("consumed_at"),
            dates=tuple(str(x) for x in data.get("dates", [])),
            bars=int(data.get("bars", 0)),
            per_day_hashes=dict(data.get("per_day_hashes") or {}),
            algorithm=str(data["algorithm"]),
            algorithm_fingerprint=str(data["algorithm_fingerprint"]),
            control=str(data["control"]),
            control_fingerprint=str(data["control_fingerprint"]),
            cost_model=dict(data.get("cost_model") or {}),
            algorithm_metrics=_metric(data.get("algorithm_metrics")),
            control_metrics=_metric(data.get("control_metrics")),
            comparison=dict(data.get("comparison") or {}),
            evidence_limitations=tuple(str(x) for x in data.get("evidence_limitations", [])),
            reasons=tuple(str(x) for x in data.get("reasons", [])),
            message=str(data.get("message", "")),
        )


EVIDENCE_LIMITATIONS = (
    "The fresh-OOS replay engines are research-consistent validation instruments built on the "
    "frozen OUR-ALGO-004 coherent parameters and the frozen MA(5,21) control. They are NOT the "
    "protected research artifacts; this replay does not re-run the consumed protected OOS and "
    "does not change ANY research/promotion result.",
    "VALIDATION research only. A PROMISING/positive result here is evidence for human review, "
    "never automatic promotion. PROMOTION=NO, ALGO READY=NO, algorithm health unchanged until "
    "the governance process explicitly promotes.",
    "NIFTY 50 INDEX 5m candles: options/expiry/IV/OI and a traded instrument profile are not "
    "present; no option-strategy claim is made.",
    "This window is single-use: a consumed window is never re-validated and its result is never "
    "edited after the first controlled run.",
    "Cost parity: the central schedule (commission 0.0003/side on fill notional, adverse "
    "slippage 0.001/side) matches the research EvaluationConfig().backtest() rates at qty 1; the "
    "research engine additionally prices commission on slip-adjusted notional, a second-order "
    "delta of (commission_rate * slippage_rate) * (entry+exit) per round trip.",
)


def cost_model_dict() -> Mapping[str, object]:
    return {
        "capital": CAPITAL,
        "quantity": QUANTITY,
        "commission_per_side": str(COMMISSION_PER_SIDE),
        "commission_fixed": "0",
        "slippage_per_side": str(SLIPPAGE_PER_SIDE),
        "research_schedule_source": "evaluation.records.EvaluationConfig().backtest() defaults",
        "note": "commission + adverse slippage applied per side on fill notional; research engine "
        "charges commission on slip-adjusted notional (second-order delta r*s*(entry+exit))",
    }