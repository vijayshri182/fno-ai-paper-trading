"""Pluggable alert engine (WS 7.14).

Delivery is decoupled from alert creation: :class:`AlertSink` implementations
receive fully-formed :class:`Alert` records and may route them anywhere (file,
collector, future email/Telegram/Slack/Teams). The engine stamps every alert
with the paper-trading environment marker and keeps a bounded in-memory
history. No notification integration is implemented (products of any sink are
the integrator's choice); all shipped sinks here log or collect — they never
contact external services.
"""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from fno_ai_paper_trading.alerting.alerts import (
    PAPER_TRADING_LABEL,
    Alert,
    AlertCategory,
    AlertLevel,
)


class AlertSink(ABC):
    """Receives alerts for delivery (file, collector, channel adapter...)."""

    @abstractmethod
    def emit(self, alert: Alert) -> None:
        """Deliver one alert. Must not raise for a single bad alert."""

    def close(self) -> None:
        """Release resources, if any. Default: no-op."""


class CollectingAlertSink(AlertSink):
    """In-memory collector (tests, statistics, in-process consumers)."""

    def __init__(self) -> None:
        self.alerts: list[Alert] = []

    def emit(self, alert: Alert) -> None:
        self.alerts.append(alert)

    @property
    def count(self) -> int:
        return len(self.alerts)


class FileAlertSink(AlertSink):
    """Append-only JSONL alert log (one canonical JSON line per alert)."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    def emit(self, alert: Alert) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(alert.to_dict(), sort_keys=True) + "\n")
        except OSError:
            # A failed sink must never take the paper loop down.
            return

    @property
    def path(self) -> Path:
        return self._path

    def read(self) -> list[dict[str, object]]:
        import json

        entries: list[dict[str, object]] = []
        if not self._path.is_file():
            return entries
        for line in self._path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                entries.append(json.loads(line))
            except (ValueError, TypeError):
                continue
        return entries


class ChainedAlertSink(AlertSink):
    """Fan an alert out to every registered sink."""

    def __init__(self, sinks: Sequence[AlertSink] = ()) -> None:
        self._sinks: list[AlertSink] = []
        for sink in sinks:
            self.register(sink)

    def register(self, sink: AlertSink) -> None:
        if not isinstance(sink, AlertSink):
            raise TypeError("sink must be an AlertSink")
        self._sinks.append(sink)

    def emit(self, alert: Alert) -> None:
        for sink in self._sinks:
            sink.emit(alert)

    def close(self) -> None:
        for sink in self._sinks:
            try:
                sink.close()
            except Exception:
                continue


@dataclass(frozen=True)
class AlertEngine:
    """Dispatches fully-formed alerts to all registered sinks.

    Every dispatched alert is stamped with the paper-trading environment marker
    so no consumer can forget it. ``max_history`` bounds the in-memory history;
    ``emit`` never routes around risk (it is pure bookkeeping/delivery).
    """

    sinks: Sequence[AlertSink] = ()
    max_history: int = 200
    history: tuple[Alert, ...] = field(default_factory=tuple, init=False)

    def register(self, sink: AlertSink) -> "AlertEngine":
        if not isinstance(sink, AlertSink):
            raise TypeError("sink must be an AlertSink")
        object.__setattr__(self, "sinks", tuple(self.sinks) + (sink,))
        return self

    def emit(
        self,
        alert: Alert,
        *,
        category: AlertCategory | None = None,
        level: AlertLevel | None = None,
    ) -> Alert:
        """Dispatch an alert. Overrides permit category/level corrections.

        The environment marker is always forced to the paper label regardless
        of input, defending against any alert losing its label.
        """
        stamped = Alert(
            category=category or alert.category,
            level=level or alert.level,
            title=alert.title,
            message=alert.message,
            source=alert.source,
            timestamp=alert.timestamp,
            environment=PAPER_TRADING_LABEL,
            metadata=dict(alert.metadata),
        )
        new_history = tuple(self.history) + (stamped,)
        if self.max_history > 0 and len(new_history) > self.max_history:
            new_history = new_history[-self.max_history :]
        object.__setattr__(self, "history", new_history)
        for sink in self.sinks:
            sink.emit(stamped)
        return stamped

    def close(self) -> None:
        for sink in self.sinks:
            try:
                sink.close()
            except Exception:
                continue

    @property
    def count(self) -> int:
        return len(self.history)