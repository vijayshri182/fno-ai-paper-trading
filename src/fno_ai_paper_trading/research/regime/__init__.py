"""Phase 7 — deterministic, provider-independent market-regime engine.

Consumes normalized ``MarketPrice`` 5m bars only (no broker/execution/Upstox
dependency) and produces decision-time :class:`MarketRegimeReport` snapshots:
direction + volatility + data-quality + evidence. The report is the input
contract for later options-strategy layers; this package never selects a
contract, never emits a recommendation and never places an order.
"""

from fno_ai_paper_trading.research.regime.engine import (
    DIRECTION_MAP,
    ENGINE_VERSION,
    INSUFFICIENT_DATA,
    SCHEMA_VERSION,
    Direction,
    MarketDataState,
    MarketRegimeEngine,
    MarketRegimeReport,
    RegimeEvidence,
    WarmupStatus,
)

__all__ = [
    "DIRECTION_MAP",
    "ENGINE_VERSION",
    "INSUFFICIENT_DATA",
    "SCHEMA_VERSION",
    "Direction",
    "MarketDataState",
    "MarketRegimeEngine",
    "MarketRegimeReport",
    "RegimeEvidence",
    "WarmupStatus",
]