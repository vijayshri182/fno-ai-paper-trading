"""Controlled single-use validation of the fresh-OOS pool.

This package is the separate, human-invoked validation job for the fresh
out-of-sample pool collected strictly after the consumed protected boundary
(2026-09-11).  The fresh-OOS *collector* (``fno_ai_paper_trading.fresh_oos``)
never imports this package and never triggers it; running validation requires an
explicit command plus a pool that has genuinely reached the coverage threshold
(>=20 trading days, >=1500 bars).  Consumption is recorded once and refuses any
second run, so the pool remains single-use.

Every check is decision-time, deterministic and network-free.  The two engines
(OUR-ALGO-004 with its frozen coherent parameters, and the frozen MA(5,21)
control) are replay instruments for the fresh window only; they never touch
the protected OOS and never change any research/promotion artifact.
"""
from fno_ai_paper_trading.fresh_oos_validation.protocol import (
    ALGO_NAME,
    CONTROL_NAME,
    FROZEN_ALGO_PARAMS,
    FROZEN_CONTROL_PARAMS,
    MIN_TRADES,
    PROTOCOL_NAME,
    PROTOCOL_VERSION,
)

__all__ = [
    "PROTOCOL_NAME",
    "PROTOCOL_VERSION",
    "ALGO_NAME",
    "CONTROL_NAME",
    "FROZEN_ALGO_PARAMS",
    "FROZEN_CONTROL_PARAMS",
    "MIN_TRADES",
]