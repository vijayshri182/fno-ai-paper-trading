"""Alert records for the pluggable alert engine (WS 7.14).

Alert *records* are plain, serializable data. Nothing here sends a
notification — delivery is pluggable via :class:`~AlertSink` implementations in
:mod:`fno_ai_paper_trading.alerting.engine`. Every trading alert is labelled
with the environment marker **PAPER TRADING — NO LIVE ORDER** by construction.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Mapping

#: Marker every emitted alert carries: the system is paper trading only.
PAPER_TRADING_LABEL = "PAPER TRADING — NO LIVE ORDER"


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _frozen_strings(values: Mapping[str, object] | None) -> Mapping[str, str]:
    copied: dict[str, str] = {}
    for key, value in (values or {}).items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise TypeError("alert metadata keys and values must be strings")
        copied[key] = value
    return _FrozenMapping(copied)


class _FrozenMapping(Mapping[str, str]):
    def __init__(self, data: dict[str, str]) -> None:
        self._data = data

    def __getitem__(self, key: str) -> str:
        return self._data[key]

    def __iter__(self):
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def __repr__(self) -> str:
        return repr(self._data)


class AlertCategory(str, Enum):
    """Alert category (trading / AI-learning / risk / system)."""

    TRADING = "TRADING"
    AI_LEARNING = "AI_LEARNING"
    RISK = "RISK"
    SYSTEM = "SYSTEM"


class AlertLevel(str, Enum):
    """Severity of an alert."""

    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


@dataclass(frozen=True)
class Alert:
    """One structured, serializable alert."""

    category: AlertCategory
    level: AlertLevel
    title: str
    message: str
    source: str = "system"
    timestamp: str = field(default_factory=_now_iso)
    environment: str = PAPER_TRADING_LABEL
    metadata: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.category, AlertCategory):
            raise TypeError("category must be an AlertCategory")
        if not isinstance(self.level, AlertLevel):
            raise TypeError("level must be an AlertLevel")
        if not self.title.strip():
            raise ValueError("alert title is required")
        if not self.message.strip():
            raise ValueError("alert message is required")
        if not self.source.strip():
            raise ValueError("alert source is required")
        if not self.environment.strip():
            raise ValueError("alert environment is required")
        object.__setattr__(self, "metadata", _frozen_strings(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category.value,
            "level": self.level.value,
            "title": self.title,
            "message": self.message,
            "source": self.source,
            "timestamp": self.timestamp,
            "environment": self.environment,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "Alert":
        return cls(
            category=AlertCategory(payload["category"]),
            level=AlertLevel(payload["level"]),
            title=str(payload["title"]),
            message=str(payload["message"]),
            source=str(payload.get("source", "system")),
            timestamp=str(payload.get("timestamp", _now_iso())),
            environment=str(payload.get("environment", PAPER_TRADING_LABEL)),
            metadata=dict(payload.get("metadata", {})),
        )


def trading_alert(
    level: AlertLevel,
    title: str,
    message: str,
    *,
    source: str = "paper-loop",
    metadata: Mapping[str, str] | None = None,
    timestamp: str | None = None,
) -> Alert:
    """Build a TRADING alert; the paper environment label is mandatory."""
    return Alert(
        category=AlertCategory.TRADING,
        level=level,
        title=title,
        message=message,
        source=source,
        metadata=metadata,
        timestamp=timestamp or _now_iso(),
        environment=PAPER_TRADING_LABEL,
    )