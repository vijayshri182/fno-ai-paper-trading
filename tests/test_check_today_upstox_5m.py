"""Hermetic tests for the read-only today's-5m Upstox readiness monitor.

No network, no real Upstox, no orders: both the Upstox fetch and the HTTP
status probe are faked. Covers the state machine (READY / NOT_READY / ERROR),
exit codes, Watchdog freshness reuse, credential redaction, status-JSON
integrity, log content and atomic file updates.

Candle-source routing is verified against both seams: the current trading day
must go through the Intraday V3 seam (``fetch_intraday_day``) with an
``/historical-candle/intraday/...`` request and never fall back to the dated
Historical seam; completed days keep using ``fetch_5m_day``.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))

import check_today_upstox_5m as monitor  # noqa: E402

from fno_ai_paper_trading.data.instrument_registry import get_research_instrument  # noqa: E402
from fno_ai_paper_trading.models.market import MarketPrice  # noqa: E402

INSTRUMENT = get_research_instrument("NIFTY 50")
DAY = date(2026, 9, 24)
NOW = datetime(2026, 9, 24, 12, 0, 0)


def _bar(ts: datetime, close: str = "23100") -> MarketPrice:
    value = Decimal(close)
    return MarketPrice(
        instrument=INSTRUMENT,
        timestamp=ts,
        open=value,
        high=value + Decimal("10"),
        low=value - Decimal("10"),
        close=value,
        volume=1,
    )


def _today_fresh_bars(day: date = DAY) -> list[MarketPrice]:
    return [_bar(datetime(day.year, day.month, day.day, 11, 59))]


def _today_stale_bars(day: date = DAY) -> list[MarketPrice]:
    return [_bar(datetime(day.year, day.month, day.day, 9, 15))]


def _yesterday_bars() -> list[MarketPrice]:
    return [_bar(datetime(2026, 9, 23, 15, 25))]


class FakeClient:
    """Fake HistoricalDataClient with an injectable failure + redaction hook."""

    def __init__(self, bars=None, error=None, redactor=None):
        self.bars = list(bars or [])
        self.error = error
        self._redactor = redactor or (lambda text: text)
        self.fetch_calls = 0
        self.seam = None

    def _serve(self):
        self.fetch_calls += 1
        if self.error is not None:
            raise self.error
        return list(self.bars)

    def fetch_5m_day(self, instrument, day):
        self.seam = "fetch_5m_day"
        return self._serve()

    def fetch_intraday_day(self, instrument):
        self.seam = "fetch_intraday_day"
        return self._serve()

    def redact_error_text(self, text):
        return self._redactor(text)


def _run_check(client, probe=None, watchdog=None, now=NOW, day=DAY):
    return monitor.run_check(
        client,
        INSTRUMENT,
        day,
        now,
        probe=probe or (lambda d: (200, "success")),
        watchdog=watchdog,
    )


class TestReady:
    def test_today_fresh_candles_ready(self) -> None:
        check = _run_check(FakeClient(_today_fresh_bars()))
        assert check.state == "READY"
        assert check.exit_code() == 0
        assert check.candle_count == 1
        assert check.watchdog_fresh is True
        assert check.hard_error is False
        assert not check.blocker
        assert check.source == "intraday"  # current day is served by Intraday V3

    def test_multiple_fresh_candles_ready(self) -> None:
        bars = [_bar(datetime(2026, 9, 24, 11, 50)), _bar(datetime(2026, 9, 24, 11, 55), "23120")]
        check = _run_check(FakeClient(bars))
        assert check.state == "READY"
        assert check.first_candle == "2026-09-24T11:50:00"
        assert check.latest_candle == "2026-09-24T11:55:00"
        assert check.http_status == 200
        assert check.api_status == "success"


class TestRouting:
    def test_current_day_uses_intraday_v3_seam(self) -> None:
        client = FakeClient(_today_fresh_bars())
        check = _run_check(client, day=DAY, now=NOW)  # day == now.date() -> intraday
        assert check.source == "intraday"
        assert client.seam == "fetch_intraday_day"
        assert client.fetch_calls == 1
        assert "/v3/historical-candle/intraday/" in check.request
        assert check.state == "READY"

    def test_current_day_never_uses_dated_historical_request(self) -> None:
        client = FakeClient(_today_fresh_bars())
        check = _run_check(client, day=DAY, now=NOW)
        assert client.seam == "fetch_intraday_day"  # today must only hit Intraday V3
        assert monitor.UPSTOX_INTRADAY_CANDLE_PATH in check.request
        # The intraday path carries unit/interval but never a dated YYYY-MM-DD window.
        tail = check.request.split(monitor.UPSTOX_INTRADAY_CANDLE_PATH)[-1]
        assert "minutes/5" in tail
        assert "2026-" not in tail

    def test_completed_day_uses_historical_v3_seam(self) -> None:
        completed = date(2026, 9, 23)
        client = FakeClient([_bar(datetime(2026, 9, 23, 9, 15))])
        check = _run_check(client, day=completed, now=NOW)  # day != now.date() -> historical
        assert check.source == "historical"
        assert client.seam == "fetch_5m_day"
        assert client.fetch_calls == 1
        assert "/v3/historical-candle/intraday/" not in check.request
        assert f"/minutes/5/{completed.isoformat()}/{completed.isoformat()}" in check.request


class TestNotReady:
    def test_zero_candles_not_ready_exit_1(self) -> None:
        check = _run_check(FakeClient([]))
        assert check.state == "NOT_READY"
        assert check.exit_code() == 1
        assert check.candle_count == 0
        assert check.watchdog_fresh is None
        assert "zero candles" in check.blocker

    def test_yesterday_data_not_ready(self) -> None:
        check = _run_check(FakeClient(_yesterday_bars()))
        assert check.state == "NOT_READY"
        assert check.exit_code() == 1
        assert "not dated" in check.blocker
        assert check.latest_candle == "2026-09-23T15:25:00"

    def test_stale_latest_candle_not_ready(self) -> None:
        check = _run_check(FakeClient(_today_stale_bars()))
        assert check.state == "NOT_READY"
        assert check.exit_code() == 1
        assert check.watchdog_fresh is False
        assert "max_bar_age" in check.blocker


class TestError:
    def test_http_api_probe_failure_exit_2(self) -> None:
        def failing_probe(day):
            raise RuntimeError("connection reset by peer")

        check = _run_check(FakeClient(_today_fresh_bars()), probe=failing_probe)
        assert check.state == "ERROR"
        assert check.exit_code() == 2
        assert check.hard_error is True
        assert "HTTP/API probe failed" in check.blocker
        assert check.candle_count == 0

    def test_fetch_failure_exit_2(self) -> None:
        check = _run_check(FakeClient(error=RuntimeError("Upstox 503 after retries")))
        assert check.state == "ERROR"
        assert check.exit_code() == 2
        assert "fetch_intraday_day failed" in check.blocker

    def test_http_200_empty_is_not_error(self) -> None:
        # HTTP 200 + candles [] must be NOT_READY (exit 1), never ERROR.
        check = _run_check(FakeClient([]), probe=lambda d: (200, "success"))
        assert check.state == "NOT_READY"
        assert check.exit_code() == 1


class TestCredentials:
    TOKEN = "supersecret-analytics-token-abc123"

    def test_fetch_error_is_redacted_from_blocker(self) -> None:
        client = FakeClient(
            error=RuntimeError(f"thing broke {self.TOKEN} !!"),
            redactor=lambda text: text.replace(self.TOKEN, "<token-redacted>"),
        )
        check = _run_check(client)
        assert self.TOKEN not in check.blocker
        assert "<token-redacted>" in check.blocker

    def test_probe_error_is_redacted_from_blocker(self) -> None:
        def bad_probe(day):
            raise RuntimeError(f"auth failed token={self.TOKEN}")

        client = FakeClient(
            _today_fresh_bars(),
            redactor=lambda text: text.replace(self.TOKEN, "<token-redacted>"),
        )
        check = _run_check(client, probe=bad_probe)
        assert self.TOKEN not in check.blocker
        assert "<token-redacted>" in check.blocker

    def test_no_credentials_in_status_json_or_log(self, tmp_path) -> None:
        client = FakeClient(
            error=RuntimeError(f"boom {self.TOKEN}"),
            redactor=lambda text: text.replace(self.TOKEN, "<token-redacted>"),
        )
        check = _run_check(client)
        status = tmp_path / monitor.STATUS_FILENAME
        log = tmp_path / monitor.LOG_FILENAME
        monitor._atomic_write_json(status, check.to_status_payload())
        monitor._append_log(log, check)

        text = status.read_text(encoding="utf-8") + log.read_text(encoding="utf-8")
        assert self.TOKEN not in text
        payload = json.loads(status.read_text(encoding="utf-8"))
        assert payload["credential_present"] is not None  # never the value itself


class TestNoExecutionReachability:
    def test_script_imports_no_execution_or_order_modules(self) -> None:
        source = (REPO_ROOT / "scripts" / "check_today_upstox_5m.py").read_text(encoding="utf-8")
        imports = re.findall(r"^\s*(?:from|import)\s+([a-zA-Z0-9_\.]+)", source, re.MULTILINE)
        forbidden_prefixes = ("fno_ai_paper_trading.execution", "fno_ai_paper_trading.live_trading")
        forbidden_names = (
            "place_order", "PlaceOrder", "modify_order", "cancel_order",
            "OrderApi", "try_convert_order", "get_order_history",
        )
        for module in imports:
            assert not module.startswith(forbidden_prefixes), (
                f"execution/live_trading module imported: {module}"
            )
        assert not any(name in source for name in forbidden_names)
        assert "fetch_5m_day" in source  # completed-day data seam
        assert "fetch_intraday_day" in source  # current-day (Intraday V3) data seam

    def test_fetch_only_via_the_routed_seam(self) -> None:
        client = FakeClient(_today_fresh_bars())
        check = _run_check(client)
        assert client.fetch_calls == 1
        assert client.seam == "fetch_intraday_day"
        assert check.state == "READY"


class TestStatusFile:
    def test_status_json_required_fields(self, tmp_path) -> None:
        check = _run_check(FakeClient(_today_fresh_bars()))
        status = tmp_path / monitor.STATUS_FILENAME
        monitor._atomic_write_json(status, check.to_status_payload())
        payload = json.loads(status.read_text(encoding="utf-8"))
        for field in (
            "checked_at", "nse_date", "instrument", "interval",
            "session_start", "session_end", "http_status",
            "api_status", "candle_count", "first_candle",
            "latest_candle", "latest_candle_age_seconds",
            "watchdog_fresh", "ready", "blocker",
        ):
            assert field in payload, f"missing field {field}"
        assert payload["nse_date"] == "2026-09-24"
        assert payload["interval"] == "5m"
        assert payload["ready"] is True
        assert payload["state"] == "READY"
        assert payload["blocker"] == ""

    def test_atomic_status_update_no_residue(self, tmp_path) -> None:
        status = tmp_path / monitor.STATUS_FILENAME
        first = _run_check(FakeClient([])).to_status_payload()
        second = _run_check(FakeClient(_today_fresh_bars())).to_status_payload()

        monitor._atomic_write_json(status, first)
        assert json.loads(status.read_text(encoding="utf-8"))["state"] == "NOT_READY"

        monitor._atomic_write_json(status, second)
        assert json.loads(status.read_text(encoding="utf-8"))["state"] == "READY"
        assert not list(tmp_path.glob(f".tmp-{monitor.STATUS_FILENAME}"))

    def test_log_line_records_readiness_fields(self, tmp_path) -> None:
        check = _run_check(FakeClient([]))
        log = tmp_path / monitor.LOG_FILENAME
        monitor._append_log(log, check)
        line = log.read_text(encoding="utf-8")
        assert "status=(NOT_READY)" in line
        assert "request=" in line and "/minutes/5" in line
        assert "candles=0" in line


class TestMainExitCodes:
    def test_main_ready_exit_0(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setattr(monitor, "_now_nse", lambda: NOW)
        monkeypatch.setattr(
            monitor, "run_check",
            lambda client, instrument, day, now=None, **kw: monitor.TodayCheck(
                checked_at="2026-09-24T12:00:00",
                nse_date=DAY.isoformat(),
                instrument="NSE_INDEX|Nifty 50",
                interval="5m",
                session_start="2026-09-24T09:15:00",
                session_end="2026-09-24T15:30:00",
                request="/v3/historical-candle/intraday/NSE_INDEX%7CNifty%2050/minutes/5",
                source="intraday",
                http_status=200,
                api_status="success",
                candle_count=1,
                first_candle="2026-09-24T11:59:00",
                latest_candle="2026-09-24T11:59:00",
                latest_candle_age_seconds=60,
                watchdog_fresh=True,
                ready=True,
                credential="PRESENT",
            ),
        )
        assert monitor.main(["--root", str(tmp_path)]) == 0
        assert (tmp_path / monitor.STATUS_FILENAME).exists()
        assert (tmp_path / monitor.LOG_FILENAME).exists()

    def test_main_not_ready_exit_1(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setattr(monitor, "_now_nse", lambda: NOW)
        monkeypatch.setattr(
            monitor, "run_check",
            lambda client, instrument, day, now=None, **kw: monitor.TodayCheck(
                checked_at="2026-09-24T12:00:00",
                nse_date=DAY.isoformat(),
                instrument="NSE_INDEX|Nifty 50",
                interval="5m",
                session_start="2026-09-24T09:15:00",
                session_end="2026-09-24T15:30:00",
                request="/v3/historical-candle/intraday/NSE_INDEX%7CNifty%2050/minutes/5",
                source="intraday",
                http_status=200,
                api_status="success",
                candle_count=0,
                first_candle=None,
                latest_candle=None,
                latest_candle_age_seconds=None,
                watchdog_fresh=None,
                ready=False,
                credential="PRESENT",
                blocker="Upstox returned zero candles for 2026-09-24",
            ),
        )
        assert monitor.main(["--root", str(tmp_path)]) == 1

    def test_main_error_exit_2(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setattr(monitor, "_now_nse", lambda: NOW)
        monkeypatch.setattr(
            monitor, "run_check",
            lambda client, instrument, day, now=None, **kw: monitor.TodayCheck(
                checked_at="2026-09-24T12:00:00",
                nse_date=DAY.isoformat(),
                instrument="NSE_INDEX|Nifty 50",
                interval="5m",
                session_start="2026-09-24T09:15:00",
                session_end="2026-09-24T15:30:00",
                request="/v3/historical-candle/intraday/NSE_INDEX%7CNifty%2050/minutes/5",
                source="intraday",
                http_status=None,
                api_status=None,
                candle_count=0,
                first_candle=None,
                latest_candle=None,
                latest_candle_age_seconds=None,
                watchdog_fresh=None,
                ready=False,
                credential="PRESENT",
                hard_error=True,
                blocker="Upstox service unavailable after retries",
            ),
        )
        assert monitor.main(["--root", str(tmp_path)]) == 2