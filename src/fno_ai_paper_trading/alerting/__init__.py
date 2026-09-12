"""Pluggable alert engine + watchdog / health / fail-safe (WS 7.14).

Paper-only by design: alerts and watchdog decisions are *bookkeeping and
delivery*. Emitting an alert never places an order, never bypasses a risk
control, and never enables live trading; a ``STOP`` fail-safe decision is data
for operators/orchestrators, not an execution. No third-party notification
integration exists — all shipped sinks are local (file/collector/chained).
"""
from __future__ import annotations

from fno_ai_paper_trading.alerting.alerts import (
    PAPER_TRADING_LABEL,
    Alert,
    AlertCategory,
    AlertLevel,
    trading_alert,
)
from fno_ai_paper_trading.alerting.engine import (
    AlertEngine,
    AlertSink,
    ChainedAlertSink,
    CollectingAlertSink,
    FileAlertSink,
)
from fno_ai_paper_trading.alerting.health import (
    ComponentStatus,
    HealthFinding,
    HealthReport,
    SafetyDecision,
    TradingSafety,
    Watchdog,
    WatchdogRun,
    bar_sequence_is_valid,
    is_stale,
)

__all__ = [
    "Alert",
    "AlertCategory",
    "AlertEngine",
    "AlertLevel",
    "AlertSink",
    "ChainedAlertSink",
    "CollectingAlertSink",
    "ComponentStatus",
    "FileAlertSink",
    "HealthFinding",
    "HealthReport",
    "PAPER_TRADING_LABEL",
    "SafetyDecision",
    "TradingSafety",
    "Watchdog",
    "WatchdogRun",
    "bar_sequence_is_valid",
    "is_stale",
    "trading_alert",
]