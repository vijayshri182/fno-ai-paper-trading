"""Watchdog / health / fail-safe (WS 7.14).

A watchdog evaluates the operational soundness of paper-system inputs and
produces a :class:`HealthReport` plus a :class:`TradingSafety` decision. The
fail-safe contract (§17k) is *HOLD / STOP paper execution rather than guessing*:
the decision is data (``STOP`` + instruction + reasons) and is never itself an
execution — it is consumed by operators and future orchestrators, who remain the
ones that ever touch risk.

Staleness and integrity checks are pure functions over timestamps / bar
sequences, so they are deterministic and fully testable offline.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Callable, Mapping

from fno_ai_paper_trading.alerting.alerts import (
    Alert,
    AlertCategory,
    AlertLevel,
)


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


class ComponentStatus(str, Enum):
    HEALTHY = "HEALTHY"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


class SafetyDecision(str, Enum):
    SAFE = "SAFE"
    WATCH = "WATCH"
    STOP = "STOP"


@dataclass(frozen=True)
class HealthFinding:
    """One checked component's verdict."""

    component: str
    status: ComponentStatus
    message: str
    detail: str = ""


@dataclass(frozen=True)
class HealthReport:
    """Aggregate health state across components."""

    generated_at: str
    findings: tuple[HealthFinding, ...]

    @property
    def status(self) -> ComponentStatus:
        if any(f.status is ComponentStatus.CRITICAL for f in self.findings):
            return ComponentStatus.CRITICAL
        if any(f.status is ComponentStatus.WARNING for f in self.findings):
            return ComponentStatus.WARNING
        return ComponentStatus.HEALTHY

    @property
    def is_fail_safe(self) -> bool:
        return self.status is ComponentStatus.CRITICAL

    def to_dict(self) -> dict[str, object]:
        return {
            "generated_at": self.generated_at,
            "status": self.status.value,
            "is_fail_safe": self.is_fail_safe,
            "findings": [
                {
                    "component": finding.component,
                    "status": finding.status.value,
                    "message": finding.message,
                    "detail": finding.detail,
                }
                for finding in self.findings
            ],
        }


@dataclass(frozen=True)
class TradingSafety:
    """Fail-safe decision: SAFE / WATCH / STOP (with instructions)."""

    decision: SafetyDecision
    instruction: str
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "decision": self.decision.value,
            "instruction": self.instruction,
            "reasons": list(self.reasons),
        }


def is_stale(latest_bar_time: datetime, now: datetime, max_age: timedelta) -> bool:
    """True when the latest bar is older than ``max_age`` at ``now``.

    Timestamps must be mutually aware or mutually naive; mixing is rejected.
    A future-dated bar is treated as stale (invalid/out-of-order data).
    """
    if (latest_bar_time.tzinfo is None) != (now.tzinfo is None):
        raise ValueError("latest_bar_time and now must both be aware or both be naive")
    age = now - latest_bar_time
    return age < timedelta(0) or age > max_age


def bar_sequence_is_valid(timestamps: list[datetime]) -> bool:
    """Bars must be chronological with strictly increasing, unique timestamps."""
    previous: datetime | None = None
    for value in timestamps:
        if previous is not None and value <= previous:
            return False
        previous = value
    return True


class Watchdog:
    """Runs configured checks and derives the fail-safe decision."""

    def __init__(
        self,
        *,
        max_bar_age: timedelta = timedelta(minutes=5),
        now: Callable[[], datetime] = datetime.now,
    ) -> None:
        if max_bar_age <= timedelta(0):
            raise ValueError("max_bar_age must be positive")
        self.max_bar_age = max_bar_age
        self._now = now

    def evaluate(
        self,
        *,
        latest_bar_time: datetime,
        components: Mapping[str, bool],
        details: Mapping[str, str] | None = None,
    ) -> HealthReport:
        """Evaluate named component health plus data freshness.

        ``components`` maps name -> healthy? (False == CRITICAL). Freshness is
        checked against ``latest_bar_time``: a stale feed is CRITICAL.
        """
        findings: list[HealthFinding] = []
        details = details or {}

        if is_stale(latest_bar_time, self._now(), self.max_bar_age):
            findings.append(
                HealthFinding(
                    component="market_data",
                    status=ComponentStatus.CRITICAL,
                    message="market data is stale",
                    detail=(
                        f"latest bar {latest_bar_time.isoformat()} older than "
                        f"{self.max_bar_age} at {self._now().isoformat()}"
                    ),
                )
            )
        else:
            findings.append(
                HealthFinding(
                    component="market_data",
                    status=ComponentStatus.HEALTHY,
                    message="market data is fresh",
                    detail=f"latest bar {latest_bar_time.isoformat()}",
                )
            )

        for name, healthy in components.items():
            status = (
                ComponentStatus.HEALTHY if healthy else ComponentStatus.CRITICAL
            )
            findings.append(
                HealthFinding(
                    component=name,
                    status=status,
                    message="component ok" if healthy else "component failed",
                    detail=details.get(name, ""),
                )
            )
        return HealthReport(generated_at=_now_iso(), findings=tuple(findings))

    def safety(self, report: HealthReport) -> TradingSafety:
        """Derive the fail-safe decision from a health report.

        STOP means hold/stop paper execution rather than guessing (§17k).
        """
        critical = [f.component for f in report.findings if f.status is ComponentStatus.CRITICAL]
        warnings = [f.component for f in report.findings if f.status is ComponentStatus.WARNING]
        if critical:
            return TradingSafety(
                decision=SafetyDecision.STOP,
                instruction=(
                    "STOP paper execution — HOLD new entries, close nothing, "
                    "do not guess; investigate before continuing"
                ),
                reasons=tuple(f"CRITICAL: {name}" for name in critical),
            )
        if warnings:
            return TradingSafety(
                decision=SafetyDecision.WATCH,
                instruction="continue paper trading with monitoring",
                reasons=tuple(f"WARNING: {name}" for name in warnings),
            )
        return TradingSafety(
            decision=SafetyDecision.SAFE,
            instruction="continue paper trading",
            reasons=(),
        )

    def alerts_for(self, report: HealthReport, safety: TradingSafety) -> tuple[Alert, ...]:
        """Map a health report into SYSTEM/RISK alerts (paper-labelled)."""
        alerts: list[Alert] = []
        for finding in report.findings:
            if finding.status is ComponentStatus.HEALTHY:
                continue
            level = {
                ComponentStatus.CRITICAL: AlertLevel.CRITICAL,
                ComponentStatus.WARNING: AlertLevel.WARNING,
            }[finding.status]
            alerts.append(
                Alert(
                    category=(
                        AlertCategory.RISK
                        if finding.component in ("risk", "risk_manager", "stop_loss", "sizing")
                        else AlertCategory.SYSTEM
                    ),
                    level=level,
                    title=f"{finding.component}: {finding.message}",
                    message=finding.detail or finding.message,
                    source="watchdog",
                    metadata={"component": finding.component},
                )
            )
        if safety.decision is SafetyDecision.STOP:
            alerts.append(
                Alert(
                    category=AlertCategory.RISK,
                    level=AlertLevel.CRITICAL,
                    title="fail-safe: STOP paper execution",
                    message=safety.instruction,
                    source="watchdog",
                    metadata={"reasons": "; ".join(safety.reasons)},
                )
            )
        return tuple(alerts)


@dataclass(frozen=True)
class WatchdogRun:
    """One watchdog evaluation plus the alerts generated for it (a report unit)."""

    report: HealthReport
    safety: TradingSafety
    alerts: tuple[Alert, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, object]:
        return {
            "report": self.report.to_dict(),
            "safety": self.safety.to_dict(),
            "alerts": [alert.to_dict() for alert in self.alerts],
        }