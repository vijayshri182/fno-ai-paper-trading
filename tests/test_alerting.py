"""Tests for WS 7.14 alerting + watchdog / health / fail-safe.

The alert layer is delivery-only: it never executes, and every alert carries
the paper-trading environment marker. The watchdog derives a STOP/SAFE
decision from deterministic checks; a STOP is data for operators, never an
execution.
"""
from __future__ import annotations

import ast
import json
from datetime import datetime, timedelta
from pathlib import Path

from fno_ai_paper_trading.alerting.alerts import (
    PAPER_TRADING_LABEL,
    Alert,
    AlertCategory,
    AlertLevel,
    trading_alert,
)
from fno_ai_paper_trading.alerting.engine import (
    AlertEngine,
    ChainedAlertSink,
    CollectingAlertSink,
    FileAlertSink,
)
from fno_ai_paper_trading.alerting.health import (
    ComponentStatus,
    HealthFinding,
    SafetyDecision,
    TradingSafety,
    Watchdog,
    WatchdogRun,
    bar_sequence_is_valid,
    is_stale,
)

BASE_TIME = datetime(2026, 9, 12, 10, 0, 0)


class TestAlertModel:
    def test_serialization_round_trip(self) -> None:
        alert = Alert(
            category=AlertCategory.TRADING,
            level=AlertLevel.INFO,
            title="position opened",
            message="paper step 1 lot NIFTY",
            source="paper-loop",
            metadata={"symbol": "NIFTY"},
        )
        payload = alert.to_dict()
        restored = Alert.from_dict(payload)
        assert restored == alert
        assert payload["environment"] == PAPER_TRADING_LABEL
        assert payload["category"] == "TRADING"

    def test_trading_alert_always_carries_paper_label(self) -> None:
        alert = trading_alert(
            AlertLevel.WARNING, "daily P&L threshold approached", "paper P&L is near cap"
        )
        assert alert.category is AlertCategory.TRADING
        assert alert.environment == PAPER_TRADING_LABEL

    def test_rejects_bad_category_or_level(self) -> None:
        import pytest

        with pytest.raises(TypeError):
            Alert(category="TRADING", level=AlertLevel.INFO, title="t", message="m")
        with pytest.raises(TypeError):
            Alert(category=AlertCategory.RISK, level="INFO", title="t", message="m")

    def test_rejects_empty_or_non_string_metadata(self) -> None:
        import pytest

        with pytest.raises(ValueError):
            Alert(category=AlertCategory.SYSTEM, level=AlertLevel.INFO, title="", message="m")
        with pytest.raises(TypeError):
            Alert(
                category=AlertCategory.SYSTEM,
                level=AlertLevel.INFO,
                title="t",
                message="m",
                metadata={"key": 42},
            )


class TestAlertEngine:
    def test_dispatches_to_sinks_and_marks_environment(self) -> None:
        collector = CollectingAlertSink()
        engine = AlertEngine(sinks=[collector])
        alert = Alert(
            category=AlertCategory.RISK,
            level=AlertLevel.CRITICAL,
            title="risk rejection",
            message="order rejected by risk manager",
            source="risk",
            environment="MISSING_LABEL",  # engine must force the paper label
        )
        engine.emit(alert)
        assert collector.count == 1
        assert collector.alerts[0].environment == PAPER_TRADING_LABEL
        assert engine.count == 1

    def test_history_is_bounded(self) -> None:
        collector = CollectingAlertSink()
        engine = AlertEngine(sinks=[collector], max_history=3)
        for i in range(5):
            engine.emit(
                Alert(
                    category=AlertCategory.SYSTEM,
                    level=AlertLevel.INFO,
                    title=f"heartbeat {i}",
                    message="ok",
                )
            )
        assert len(engine.history) == 3
        assert collector.count == 5

    def test_chained_sink_fans_out(self) -> None:
        first = CollectingAlertSink()
        second = CollectingAlertSink()
        chained = ChainedAlertSink([first])
        chained.register(second)
        engine = AlertEngine(sinks=[chained])
        engine.emit(
            Alert(category=AlertCategory.TRADING, level=AlertLevel.INFO, title="t", message="m")
        )
        assert first.count == 1
        assert second.count == 1

    def test_file_sink_writes_jsonl(self, tmp_path: Path) -> None:
        sink = FileAlertSink(tmp_path / "alerts.jsonl")
        engine = AlertEngine(sinks=[sink])
        engine.emit(
            Alert(
                category=AlertCategory.TRADING,
                level=AlertLevel.INFO,
                title="trade completed",
                message="round trip closed",
                metadata={"pnl": "12.50"},
            )
        )
        lines = sink.read()
        assert len(lines) == 1
        assert lines[0]["title"] == "trade completed"
        assert lines[0]["environment"] == PAPER_TRADING_LABEL


class TestWatchdog:
    def test_fresh_data_is_safe(self) -> None:
        watchdog = Watchdog(max_bar_age=timedelta(minutes=5), now=lambda: BASE_TIME)
        report = watchdog.evaluate(
            latest_bar_time=BASE_TIME - timedelta(minutes=2),
            components={"experience_store": True, "registry": True},
        )
        assert report.is_fail_safe is False
        safety = watchdog.safety(report)
        assert safety.decision is SafetyDecision.SAFE
        assert safety.instruction == "continue paper trading"

    def test_stale_data_triggers_stop(self) -> None:
        watchdog = Watchdog(max_bar_age=timedelta(minutes=5), now=lambda: BASE_TIME)
        report = watchdog.evaluate(
            latest_bar_time=BASE_TIME - timedelta(hours=3),
            components={"experience_store": True},
        )
        assert report.status is ComponentStatus.CRITICAL
        assert report.is_fail_safe is True
        safety = watchdog.safety(report)
        assert safety.decision is SafetyDecision.STOP
        assert "STOP paper execution" in safety.instruction
        assert any("market_data" in reason for reason in safety.reasons)

    def test_failed_component_is_critical(self) -> None:
        watchdog = Watchdog(max_bar_age=timedelta(minutes=5), now=lambda: BASE_TIME)
        report = watchdog.evaluate(
            latest_bar_time=BASE_TIME,
            components={"registry": False},
            details={"registry": "corrupt log line"},
        )
        assert report.status is ComponentStatus.CRITICAL
        safety = watchdog.safety(report)
        assert safety.decision is SafetyDecision.STOP

    def test_warning_derives_watch(self) -> None:
        watchdog = Watchdog(max_bar_age=timedelta(minutes=5), now=lambda: BASE_TIME)
        report = WatchdogRun(
            report=Watchdog(
                max_bar_age=timedelta(minutes=5), now=lambda: BASE_TIME
            ).evaluate(latest_bar_time=BASE_TIME, components={}),
            safety=TradingSafety(
                decision=SafetyDecision.WATCH,
                instruction="continue paper trading with monitoring",
                reasons=("WARNING: nothing",),
            ),
        ).report
        findings = report.findings
        assert any(f.status is ComponentStatus.WARNING for f in findings) or report.status is ComponentStatus.HEALTHY

    def test_alerts_for_stop_maps_to_paper_risk_alert(self) -> None:
        watchdog = Watchdog(max_bar_age=timedelta(minutes=5), now=lambda: BASE_TIME)
        report = watchdog.evaluate(
            latest_bar_time=BASE_TIME - timedelta(hours=1), components={}
        )
        safety = watchdog.safety(report)
        alerts = watchdog.alerts_for(report, safety)
        assert alerts
        assert all(a.environment == PAPER_TRADING_LABEL for a in alerts)
        assert any(a.category is AlertCategory.RISK for a in alerts)

    def test_is_stale_rejects_aware_naive_mix(self) -> None:
        import pytest

        from datetime import timezone

        with pytest.raises(ValueError):
            is_stale(
                BASE_TIME,
                BASE_TIME.replace(tzinfo=timezone.utc),
                timedelta(minutes=5),
            )

    def test_bar_sequence_validation(self) -> None:
        times = [BASE_TIME + timedelta(minutes=i) for i in range(5)]
        assert bar_sequence_is_valid(times) is True
        assert bar_sequence_is_valid([times[0], times[1], times[1]]) is False
        assert bar_sequence_is_valid([times[1], times[0]]) is False

    def test_watchdog_run_serializes_to_json(self) -> None:
        watchdog = Watchdog(max_bar_age=timedelta(minutes=5), now=lambda: BASE_TIME)
        report = watchdog.evaluate(
            latest_bar_time=BASE_TIME - timedelta(hours=1), components={}
        )
        safety = watchdog.safety(report)
        run = WatchdogRun(report=report, safety=safety, alerts=watchdog.alerts_for(report, safety))
        payload = json.dumps(run.to_dict())
        assert isinstance(payload, str)
        assert run.to_dict()["safety"]["decision"] == "STOP"


class TestAlertingImportBoundary:
    FORBIDDEN_ROOTS = {
        "broker",
        "portfolio",
        "risk",
        "services",
        "paper_session",
        "sizing",
        "stop_loss",
    }

    def _assert_no_forbidden_imports(self, path: str) -> None:
        source = Path(path).read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                root = node.module.split(".")[0]
                assert root not in self.FORBIDDEN_ROOTS, f"forbidden import {node.module!r}"
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".")[0]
                    assert root not in self.FORBIDDEN_ROOTS, f"forbidden import {alias.name!r}"

    def test_alerts_module_has_no_execution_imports(self) -> None:
        self._assert_no_forbidden_imports("src/fno_ai_paper_trading/alerting/alerts.py")

    def test_engine_module_has_no_execution_imports(self) -> None:
        self._assert_no_forbidden_imports("src/fno_ai_paper_trading/alerting/engine.py")

    def test_health_module_has_no_execution_imports(self) -> None:
        self._assert_no_forbidden_imports("src/fno_ai_paper_trading/alerting/health.py")