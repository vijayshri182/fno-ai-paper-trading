"""Fresh out-of-sample (OOS) data collector and scheduler.

The fresh OOS subsystem is a deterministic, immutable, server-safe acquisition
service. It collects NIFTY 50 5-minute candles **strictly after** the lineage's
already-consumed protected window (2025-10-06 .. 2026-09-11), validates each
day's completeness, stores it content-addressed (SHA-256) in an immutable area,
records every run in a manifest, and reports data readiness. It never runs
validation, never tunes parameters, never places orders and never touches the
protected OOS/research datasets.

Dependency direction is strictly: ``Collector -> Historical Data Client ->
Immutable Store -> Manifest``. No strategy, backtest, walk-forward, promotion,
execution or broker module is ever imported.
"""
from __future__ import annotations

from fno_ai_paper_trading.fresh_oos.collector import (
    FreshOosCollector,
    FreshOosCollectorConfig,
)
from fno_ai_paper_trading.fresh_oos.client import (
    HistoricalDataClient,
    UpstoxHistoricalDataClient,
)
from fno_ai_paper_trading.fresh_oos.credential_provider import (
    CREDENTIAL_ENV,
    CredentialState,
    RuntimeCredentialProvider,
)
from fno_ai_paper_trading.fresh_oos.errors import FreshOosError
from fno_ai_paper_trading.fresh_oos.manifest import FreshOosManifest
from fno_ai_paper_trading.fresh_oos.protocol import (
    FRESH_OOS_BOUNDARY,
    MIN_BARS,
    MIN_TRADES,
    MIN_TRADING_DAYS,
    ReadinessReport,
    compute_readiness,
)
from fno_ai_paper_trading.fresh_oos.store import FreshOosStore

__all__ = [
    "FreshOosCollector",
    "FreshOosCollectorConfig",
    "HistoricalDataClient",
    "UpstoxHistoricalDataClient",
    "FreshOosManifest",
    "FreshOosStore",
    "FreshOosError",
    "FRESH_OOS_BOUNDARY",
    "MIN_TRADING_DAYS",
    "MIN_BARS",
    "MIN_TRADES",
    "ReadinessReport",
    "compute_readiness",
    "CREDENTIAL_ENV",
    "CredentialState",
    "RuntimeCredentialProvider",
]