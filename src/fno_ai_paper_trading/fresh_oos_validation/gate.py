"""Pre-validation gate: every condition that must hold before a controlled run.

The gate is a pure, deterministic function of the verified pool snapshot plus
invocation context.  It never reads the network, never imports strategies and
never writes anything; a single failing condition blocks the run with an exact
reason.
"""
from __future__ import annotations

from dataclasses import dataclass

from fno_ai_paper_trading.fresh_oos_validation.pool import PoolReport
from fno_ai_paper_trading.fresh_oos_validation.protocol import (
    ALGO_FINGERPRINT,
    CONTROL_FINGERPRINT,
    FROZEN_ALGO_PARAMS,
    FROZEN_CONTROL_PARAMS,
    MIN_TRADES,
    assert_frozen_params,
)


@dataclass(frozen=True)
class GateReport:
    """Result of the gate evaluation: reasons (empty => run allowed)."""

    reasons: tuple[str, ...]
    can_run: bool

    def __str__(self) -> str:
        return "; ".join(self.reasons) if self.reasons else "ALL CHECKS PASS"


def _frozen_reason(mapping: object, name: str, fingerprint: str) -> str | None:
    try:
        assert_frozen_params(dict(mapping), name, fingerprint)  # type: ignore[arg-type]
        return None
    except (ValueError, TypeError) as exc:
        return str(exc)


def evaluate_gate(
    pool: PoolReport,
    *,
    explicit_invoke: bool,
    min_days: int = 20,
    min_bars: int = 1500,
    min_trades: int = MIN_TRADES,
    consumed: bool = False,
    algo_params: object = FROZEN_ALGO_PARAMS,
    control_params: object = FROZEN_CONTROL_PARAMS,
) -> GateReport:
    """Return the refusals that would block a single-use validation run."""
    reasons: list[str] = []
    if not explicit_invoke:
        reasons.append(
            "controlled command requires explicit human invocation (--explicit); never automatic"
        )
    if consumed:
        reasons.append("single-use validation already consumed; the fresh pool is NOT re-runnable")
    if pool.num_days < min_days:
        reasons.append(f"fresh pool has {pool.num_days} days < required {min_days}")
    if pool.num_bars_count < min_bars:
        reasons.append(f"fresh pool has {pool.num_bars_count} bars < required {min_bars}")
    algo_reason = _frozen_reason(algo_params, "OUR-ALGO-004", ALGO_FINGERPRINT)
    if algo_reason is not None:
        reasons.append(algo_reason)
    control_reason = _frozen_reason(control_params, "MA(5,21)", CONTROL_FINGERPRINT)
    if control_reason is not None:
        reasons.append(control_reason)
    return GateReport(reasons=tuple(reasons), can_run=not reasons)