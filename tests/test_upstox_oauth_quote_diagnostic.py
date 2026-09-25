"""Focused offline tests for the OAuth -> one-quote diagnostic (WS 7.9+).

Never touches the network or a broker: OAuth collaborators, the quote
transport and the browser opener are all faked, and the real
``UpstoxExecutionAdapter.quote()`` is exercised against an injected transport
to prove the wiring end to end. No live quote is ever made.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))

import upstox_oauth_quote_diagnostic as diag  # noqa: E402

from fno_ai_paper_trading.execution.errors import UpstoxExecutionError  # noqa: E402
from fno_ai_paper_trading.execution.oauth import (  # noqa: E402
    CallbackOutcome,
    TokenResponse,
    UpstoxOAuthConfig,
)
from fno_ai_paper_trading.utils.http import HttpError, HttpResponse  # noqa: E402

CONFIG = UpstoxOAuthConfig(
    client_id="app-id-123",
    client_secret="app-secret-abc",
    redirect_uri="http://127.0.0.1:8000/callback",
    base_url="https://api.upstox.com",
    timeout_seconds=300.0,
)
STATE = "csrf-state-xyz"
CODE = "authorization-code-000111"
TOKEN = "diagnostic-access-token-never-printed"

_QUOTE_BODY = json.dumps(
    {"data": {"NSE_FO:NIFTY 27 OCT 26": {
        "ohlc": {"close": 1234.5},
        "instrument_token": "NSE_FO|56983",
    }}}
).encode("utf-8")


class _FakeServer:
    def __init__(self, outcome: CallbackOutcome) -> None:
        self._outcome = outcome

    def serve_once(self, timeout: float) -> CallbackOutcome:
        return self._outcome


def _ok_outcome() -> CallbackOutcome:
    return CallbackOutcome(state_ok=True, code=CODE)


def _patch_oauth(monkeypatch, transport: Any | None = None) -> list[str]:
    """Fake the browser/OAuth pieces; return the opened-URL list."""
    opened: list[str] = []

    monkeypatch.setattr(diag, "new_oauth_state", lambda: STATE)
    monkeypatch.setattr(
        diag, "LocalCallbackServer",
        lambda expected_state, port: _FakeServer(_ok_outcome()),
    )
    monkeypatch.setattr(
        diag, "exchange_code_for_token",
        lambda config, code, request=None: TokenResponse(access_token=TOKEN),
    )
    monkeypatch.setattr(diag.webbrowser, "open", lambda url: opened.append(url) or True)
    return opened


class TestSuccessfulQuote:
    def test_report_fields_and_exactly_one_quote(self, monkeypatch) -> None:
        calls: list[str] = []
        opened = _patch_oauth(monkeypatch)

        def quote_transport(method, url, *, headers=None, timeout=10.0, data=None, **kwargs):
            calls.append(url)
            return HttpResponse(200, _QUOTE_BODY, url, headers or {})

        report = diag.run_oauth_quote_diagnostic(config=CONFIG, request=quote_transport)

        assert report["oauth_started"] is True
        assert report["oauth_completed"] is True
        assert report["access_token_received"] is True
        assert report["token_persisted"] is False
        assert report["token_value_exposed"] is False
        assert report["instrument_key"] == "NSE_FO|56983"
        assert report["quote_endpoint"] == "/v2/market-quote/ohlc?instrument_key=NSE_FO%7C56983&interval=1d"
        assert report["http_status"] == 200
        assert report["upstox_error_code"] is None
        assert report["upstox_error_message"] is None
        assert report["quote_available"] is True
        assert report["reference_price"] == "1234.5"
        assert report["classification"] == "QUOTE_AVAILABLE"
        assert report["confidence"] == "1.0"
        assert report["orders_placed"] is False
        assert report["live_trading_enabled"] is False
        assert report["scheduler_enabled"] is False
        assert "timestamp_utc" in report
        assert len(calls) == 1  # exactly one quote request; no retries
        assert "/v2/market-quote/ohlc?instrument_key=NSE_FO%7C56983&interval=1d" in calls[0]
        assert opened == [diag.authorize_url(CONFIG, STATE)]
        assert TOKEN not in json.dumps(report)
        assert CODE not in json.dumps(report)

    def test_closed_browser_still_proceeds(self, monkeypatch) -> None:
        monkeypatch.setattr(diag, "new_oauth_state", lambda: STATE)
        monkeypatch.setattr(
            diag, "LocalCallbackServer",
            lambda expected_state, port: _FakeServer(_ok_outcome()),
        )
        monkeypatch.setattr(
            diag, "exchange_code_for_token",
            lambda config, code, request=None: TokenResponse(access_token=TOKEN),
        )
        monkeypatch.setattr(diag.webbrowser, "open", lambda url: False)

        def quote_transport(method, url, *, headers=None, timeout=10.0, data=None, **kwargs):
            return HttpResponse(200, _QUOTE_BODY, url, headers or {})

        report = diag.run_oauth_quote_diagnostic(
            config=CONFIG, request=quote_transport
        )
        assert report["oauth_completed"] is True
        assert report["classification"] == "QUOTE_AVAILABLE"


class TestQuoteFailure:
    def test_http_error_captures_upstox_error_details(self, monkeypatch) -> None:
        _patch_oauth(monkeypatch)

        def error_transport(method, url, *, headers=None, timeout=10.0, data=None, **kwargs):
            body = json.dumps(
                {"errors": [{"error_code": "BAD_INDICATOR", "message": "invalid instrument"}]}
            ).encode("utf-8")
            raise HttpError(400, url, body)

        report = diag.run_oauth_quote_diagnostic(config=CONFIG, request=error_transport)

        assert report["oauth_completed"] is True
        assert report["access_token_received"] is True
        assert report["http_status"] == 400
        assert report["upstox_error_code"] == "BAD_INDICATOR"
        assert report["upstox_error_message"] == "invalid instrument"
        assert report["quote_available"] is False
        assert report["reference_price"] is None
        assert report["classification"] == "QUOTE_UNAVAILABLE"
        assert report["confidence"] == "0.0"
        assert TOKEN not in json.dumps(report)

    def test_close_price_missing_is_unavailable(self, monkeypatch) -> None:
        _patch_oauth(monkeypatch)

        def bare_transport(method, url, *, headers=None, timeout=10.0, data=None, **kwargs):
            return HttpResponse(200, b'{"data": {}}', url, headers or {})

        report = diag.run_oauth_quote_diagnostic(config=CONFIG, request=bare_transport)

        assert report["http_status"] == 200
        assert report["quote_available"] is False
        assert report["classification"] == "QUOTE_UNAVAILABLE"
        assert report["confidence"] == "0.0"


class TestOAuthFailure:
    def test_no_callback_never_reaches_quote(self, monkeypatch) -> None:
        monkeypatch.setattr(diag, "new_oauth_state", lambda: STATE)
        monkeypatch.setattr(
            diag, "LocalCallbackServer",
            lambda expected_state, port: _FakeServer(
                CallbackOutcome(state_ok=False, error="callback state mismatch (possible CSRF)")
            ),
        )
        monkeypatch.setattr(diag.webbrowser, "open", lambda url: True)
        touched: list[str] = []

        def must_not_run(config, code, *, request=None):
            touched.append("exchange")
            return TokenResponse(access_token="")

        monkeypatch.setattr(diag, "exchange_code_for_token", must_not_run)

        report = diag.run_oauth_quote_diagnostic(config=CONFIG, request=lambda *a, **k: (_ for _ in ()).throw(AssertionError("no quote allowed")))

        assert report["oauth_started"] is True
        assert report["oauth_completed"] is False
        assert report["access_token_received"] is False
        assert report["classification"] == "OAUTH_FAILED"
        assert report["confidence"] == "0.0"
        assert report["quote_available"] is False
        assert report["http_status"] is None
        assert report["reference_price"] is None
        assert touched == []
        assert TOKEN not in json.dumps(report)
        assert CODE not in json.dumps(report)

    def test_token_exchange_failure_is_oauth_failure(self, monkeypatch) -> None:
        _patch_oauth(monkeypatch)

        def boom(config, code, *, request=None):
            raise UpstoxExecutionError("Upstox token exchange rejected the request (HTTP 400): nope")

        monkeypatch.setattr(diag, "exchange_code_for_token", boom)

        report = diag.run_oauth_quote_diagnostic(config=CONFIG, request=None)

        assert report["oauth_completed"] is True
        assert report["access_token_received"] is False
        assert report["classification"] == "OAUTH_FAILED"
        assert report["confidence"] == "0.0"
        assert report["quote_available"] is False


class TestReportWriter:
    def test_write_report_is_timestamped_windows_safe_json(self, tmp_path: Path) -> None:
        report: dict[str, Any] = {
            "oauth_started": True,
            "oauth_completed": True,
            "access_token_received": True,
            "token_persisted": False,
            "token_value_exposed": False,
            "instrument_key": "NSE_FO|56983",
            "quote_endpoint": "/v2/market-quote/ohlc?instrument_key=NSE_FO%7C56983&interval=1d",
            "http_status": 200,
            "upstox_error_code": None,
            "upstox_error_message": None,
            "quote_available": True,
            "reference_price": "1234.5",
            "classification": "QUOTE_AVAILABLE",
            "confidence": "1.0",
            "orders_placed": False,
            "live_trading_enabled": False,
            "scheduler_enabled": False,
            "timestamp_utc": "2026-09-22T10:15:30.123456+00:00",
        }
        out_dir = tmp_path / "reports" / "execution"
        path = diag.write_report(report, out_dir)
        assert path.exists()
        assert path.parent == out_dir
        assert ":" not in path.name
        assert path.name.startswith("upstox_oauth_quote_diagnostic_")
        assert json.loads(path.read_text(encoding="utf-8")) == report


class TestCli:
    def test_cli_never_echoes_token_code_or_secret_and_writes_report(
        self, monkeypatch, capsys, tmp_path: Path
    ) -> None:
        calls: list[str] = []
        opened = _patch_oauth(monkeypatch)

        def quote_transport(method, url, *, headers=None, timeout=10.0, data=None, **kwargs):
            calls.append(url)
            return HttpResponse(200, _QUOTE_BODY, url, headers or {})

        monkeypatch.setattr(diag, "http_request", quote_transport)
        monkeypatch.setenv("UPSTOX_API_KEY", "clitest-app-id")
        monkeypatch.setenv("UPSTOX_API_SECRET", "clitest-app-secret-zzz")
        monkeypatch.setenv("UPSTOX_ACCESS_TOKEN", "")
        monkeypatch.setenv("UPSTOX_BASE_URL", CONFIG.base_url)

        rc = diag.main([
            "--redirect-uri", CONFIG.redirect_uri,
            "--base-url", CONFIG.base_url,
            "--timeout", "5",
            "--report-dir", str(tmp_path),
            "--state", STATE,
        ])
        out = capsys.readouterr().out

        assert rc == 0
        assert TOKEN not in out
        assert CODE not in out
        assert "clitest-app-secret-zzz" not in out
        assert "clitest-app-id" not in out
        assert len(calls) == 1  # exactly one quote request
        assert len(opened) == 1

        files = list(tmp_path.glob("upstox_oauth_quote_diagnostic_*.json"))
        assert len(files) == 1
        loaded = json.loads(files[0].read_text(encoding="utf-8"))
        assert loaded["classification"] == "QUOTE_AVAILABLE"
        assert loaded["instrument_key"] == "NSE_FO|56983"
        assert loaded["token_persisted"] is False
        assert loaded["token_value_exposed"] is False
        assert loaded["orders_placed"] is False
        assert loaded["live_trading_enabled"] is False
        assert loaded["scheduler_enabled"] is False

    def test_cli_returns_2_when_credentials_missing(self, monkeypatch, capsys) -> None:
        monkeypatch.setenv("UPSTOX_API_KEY", "")
        monkeypatch.setenv("UPSTOX_API_SECRET", "")
        rc = diag.main(["--report-dir", "reports/execution"])
        captured = capsys.readouterr()
        assert rc == 2
        assert "UPSTOX_API_KEY" in captured.err