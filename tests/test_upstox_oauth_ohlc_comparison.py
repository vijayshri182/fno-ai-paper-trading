"""Focused offline tests for the OHLC URL-shape comparison diagnostic.

Never touches the network or a broker: OAuth collaborators and the OHLC quote
transport are all faked, and the exact two-request wiring is asserted (query
form first, path form second). No live quote is ever made.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Callable

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))

import upstox_oauth_ohlc_comparison as comp  # noqa: E402

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

QUERY_ENDPOINT = "/v2/market-quote/ohlc?instrument_key=NSE_FO%7C48704&interval=1d"
PATH_ENDPOINT = "/v2/market-quote/ohlc/NSE_FO%7C48704"
FULL_QUERY_URL = f"{CONFIG.base_url}{QUERY_ENDPOINT}"
FULL_PATH_URL = f"{CONFIG.base_url}{PATH_ENDPOINT}"


class _FakeServer:
    def __init__(self, outcome: CallbackOutcome) -> None:
        self._outcome = outcome

    def serve_once(self, timeout: float) -> CallbackOutcome:
        return self._outcome


def _ok_outcome() -> CallbackOutcome:
    return CallbackOutcome(state_ok=True, code=CODE)


def _patch_oauth(monkeypatch) -> list[str]:
    """Fake the browser/OAuth pieces; return the opened-URL list."""
    opened: list[str] = []

    monkeypatch.setattr(comp, "new_oauth_state", lambda: STATE)
    monkeypatch.setattr(
        comp, "LocalCallbackServer",
        lambda expected_state, port: _FakeServer(_ok_outcome()),
    )
    monkeypatch.setattr(
        comp, "exchange_code_for_token",
        lambda config, code, request=None: TokenResponse(access_token=TOKEN),
    )
    monkeypatch.setattr(comp.webbrowser, "open", lambda url: opened.append(url) or True)
    return opened


def _quote_body(node_key: str, close) -> bytes:
    return json.dumps({"data": {node_key: {"ohlc": {"close": close}}}}).encode("utf-8")


def _make_transport(*responses) -> tuple[Callable, list[str]]:
    """Fake transport that plays the given responses in order; returns (fn, calls)."""
    calls: list[str] = []

    def transport(method, url, *, headers=None, timeout=10.0, data=None, **kwargs):
        calls.append(url)
        next_response = responses[len(calls) - 1]
        if isinstance(next_response, Exception):
            raise next_response
        if isinstance(next_response, HttpError):
            raise next_response
        return next_response

    return transport, calls


class _CurlLikeTransport:
    """Emulates curl.exe URL validation: rejects any URL with no host (exit 3).

    Any path-only argument would fail here exactly as the real diagnostic did
    (``curl: (3) URL rejected: No host part in the URL``), so this transport
    cannot return a quote unless every call receives a fully-qualified URL.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, method, url, *, headers=None, timeout=10.0, data=None, **kwargs):
        self.calls.append(url)
        scheme, _, rest = url.partition("://")
        if not scheme or "/" not in rest:
            raise OSError(
                f"curl failed (exit 3) for {url}: curl: (3) URL rejected: No host part in the URL"
            )
        return HttpResponse(200, _quote_body("NSE_FO:48704", 24550.0), url, {})


class TestRequestEndpoints:
    def test_endpoint_snapshots(self) -> None:
        assert comp.request_a_endpoint() == QUERY_ENDPOINT
        assert comp.request_b_endpoint() == PATH_ENDPOINT

    def test_ohlc_url_expands_to_scheme_and_host(self) -> None:
        assert comp.ohlc_url(CONFIG.base_url, QUERY_ENDPOINT) == FULL_QUERY_URL
        assert comp.ohlc_url(CONFIG.base_url, PATH_ENDPOINT) == FULL_PATH_URL
        assert comp.ohlc_url(CONFIG.base_url, QUERY_ENDPOINT).startswith("https://")

    def test_ohlc_url_handles_trailing_slash_in_base_url(self) -> None:
        assert comp.ohlc_url(f"{CONFIG.base_url}/", QUERY_ENDPOINT) == FULL_QUERY_URL
        assert comp.ohlc_url(f"{CONFIG.base_url}/", PATH_ENDPOINT) == FULL_PATH_URL


class TestFullyQualifiedTransport:
    """The transport must always receive a fully-qualified URL (scheme + host).

    Regression for the confirmed diagnostic defect where bare API paths were
    handed to curl.exe, which exited 3 with "URL rejected: No host part in the
    URL" before any request reached Upstox.
    """

    def test_both_requests_pass_curl_style_host_validation(self, monkeypatch) -> None:
        _patch_oauth(monkeypatch)
        transport = _CurlLikeTransport()

        report = comp.run_ohlc_comparison(config=CONFIG, request=transport)

        # Exactly two GETs, both with scheme+host: a bare path would have
        # raised OSError (curl exit 3) inside _CurlLikeTransport.
        assert transport.calls == [FULL_QUERY_URL, FULL_PATH_URL]
        assert report["classification"] == "QUERY_AND_PATH_AVAILABLE"
        for record in report["requests"]:
            assert record["http_status"] == 200
            assert record["quote_available"] is True
            assert record["reference_price"] == "24550.0"

    def test_no_call_url_is_ever_a_bare_path(self, monkeypatch) -> None:
        _patch_oauth(monkeypatch)
        transport, calls = _make_transport(
            HttpResponse(200, _quote_body("NSE_FO:48704", 24550.0), FULL_QUERY_URL, {}),
            HttpResponse(200, _quote_body("NSE_FO:48704", 24560.0), FULL_PATH_URL, {}),
        )

        comp.run_ohlc_comparison(config=CONFIG, request=transport)

        for url in calls:
            assert "://" in url
            assert url.startswith("https://api.upstox.com/")
            assert url != QUERY_ENDPOINT
            assert url != PATH_ENDPOINT


class TestBothShapesAvailable:
    def test_report_records_both_and_exactly_two_calls(self, monkeypatch) -> None:
        opened = _patch_oauth(monkeypatch)
        transport, calls = _make_transport(
            HttpResponse(200, _quote_body("NSE_FO:48704", 24550.0), QUERY_ENDPOINT, {}),
            HttpResponse(200, _quote_body("NSE_FO:48704", 24560.0), PATH_ENDPOINT, {}),
        )

        report = comp.run_ohlc_comparison(config=CONFIG, request=transport)

        assert report["oauth_started"] is True
        assert report["oauth_completed"] is True
        assert report["access_token_received"] is True
        assert report["token_persisted"] is False
        assert report["token_value_exposed"] is False
        assert report["orders_placed"] is False
        assert report["live_trading_enabled"] is False
        assert report["scheduler_enabled"] is False
        assert report["instrument_key"] == "NSE_FO|48704"
        assert report["classification"] == "QUERY_AND_PATH_AVAILABLE"

        assert len(calls) == 2  # exactly the two OHLC GETs; no OAuth via this transport
        assert calls == [FULL_QUERY_URL, FULL_PATH_URL]
        assert all(url.startswith("https://api.upstox.com/") for url in calls)

        query_record, path_record = report["requests"]
        assert query_record["endpoint"] == QUERY_ENDPOINT
        assert query_record["http_status"] == 200
        assert query_record["quote_available"] is True
        assert query_record["reference_price"] == "24550.0"
        assert path_record["endpoint"] == PATH_ENDPOINT
        assert path_record["http_status"] == 200
        assert path_record["quote_available"] is True
        assert path_record["reference_price"] == "24560.0"

        assert opened == [comp.authorize_url(CONFIG, STATE)]
        assert TOKEN not in json.dumps(report)
        assert CODE not in json.dumps(report)


class TestQueryFormOnly:
    def test_query_form_succeeds_while_path_form_404s(self, monkeypatch) -> None:
        _patch_oauth(monkeypatch)
        error_body = json.dumps({
            "status": "error",
            "errors": [{"errorCode": "UDAPI100060", "message": "Resource not Found."}],
        }).encode("utf-8")
        transport, calls = _make_transport(
            HttpResponse(200, _quote_body("NSE_FO:48704", 24550.0), QUERY_ENDPOINT, {}),
            HttpError(404, PATH_ENDPOINT, error_body),
        )

        report = comp.run_ohlc_comparison(config=CONFIG, request=transport)

        assert len(calls) == 2
        assert report["classification"] == "QUERY_FORM_AVAILABLE_ONLY"

        query_record, path_record = report["requests"]
        assert query_record["quote_available"] is True
        assert query_record["reference_price"] == "24550.0"
        assert path_record["http_status"] == 404
        assert path_record["quote_available"] is False
        assert path_record["reference_price"] is None
        assert path_record["upstox_error_code"] == "UDAPI100060"
        assert path_record["upstox_error_message"] == "Resource not Found."


class TestBothUnavailable:
    def test_path_form_404_but_query_form_also_fails(self, monkeypatch) -> None:
        _patch_oauth(monkeypatch)
        error_body = json.dumps({
            "status": "error",
            "errors": [{"errorCode": "UDAPI100060", "message": "Resource not Found."}],
        }).encode("utf-8")
        transport, calls = _make_transport(
            HttpError(404, QUERY_ENDPOINT, error_body),
            HttpError(404, PATH_ENDPOINT, error_body),
        )

        report = comp.run_ohlc_comparison(config=CONFIG, request=transport)

        assert len(calls) == 2
        assert report["classification"] == "BOTH_404"
        for record in report["requests"]:
            assert record["http_status"] == 404
            assert record["quote_available"] is False
            assert record["upstox_error_code"] == "UDAPI100060"

    def test_query_form_ok_with_missing_close_is_not_available(self, monkeypatch) -> None:
        _patch_oauth(monkeypatch)
        transport, _ = _make_transport(
            HttpResponse(200, json.dumps({"data": {"NSE_FO:48704": {"ohlc": {}}}}).encode("utf-8"), QUERY_ENDPOINT, {}),
            HttpResponse(200, json.dumps({"data": {"NSE_FO:48704": {"ohlc": {}}}}).encode("utf-8"), PATH_ENDPOINT, {}),
        )

        report = comp.run_ohlc_comparison(config=CONFIG, request=transport)

        assert report["classification"] == "QUOTE_UNAVAILABLE"
        for record in report["requests"]:
            assert record["quote_available"] is False
            assert record["reference_price"] is None


class TestOAuthFailure:
    def test_oauth_not_completed_skips_quotes(self, monkeypatch) -> None:
        monkeypatch.setattr(comp, "new_oauth_state", lambda: STATE)
        monkeypatch.setattr(
            comp, "LocalCallbackServer",
            lambda expected_state, port: _FakeServer(CallbackOutcome()),
        )
        transport, calls = _make_transport()

        report = comp.run_ohlc_comparison(config=CONFIG, request=transport)

        assert report["oauth_completed"] is False
        assert report["classification"] == "OAUTH_FAILED"
        assert report["requests"] == []
        assert report["orders_placed"] is False
        assert report["token_persisted"] is False
        assert report["token_value_exposed"] is False
        assert report["live_trading_enabled"] is False
        assert report["scheduler_enabled"] is False
        assert calls == []  # no HTTP calls at all

    def test_token_exchange_error_records_oauth_failure(self, monkeypatch) -> None:
        _patch_oauth(monkeypatch)
        monkeypatch.setattr(
            comp, "exchange_code_for_token",
            lambda config, code, request=None: (_ for _ in ()).throw(
                HttpError(400, "token", b"{}")
            ),
        )
        transport, calls = _make_transport()

        report = comp.run_ohlc_comparison(config=CONFIG, request=transport)

        assert report["oauth_completed"] is True
        assert report["access_token_received"] is False
        assert report["classification"] == "OAUTH_FAILED"
        assert report["requests"] == []
        assert calls == []


class TestReportWriting:
    def test_write_report_roundtrip(self, tmp_path: Path) -> None:
        report = {
            "instrument_key": "NSE_FO|48704",
            "oauth_completed": True,
            "token_persisted": False,
            "token_value_exposed": False,
            "orders_placed": False,
            "live_trading_enabled": False,
            "scheduler_enabled": False,
            "requests": [
                {
                    "endpoint": QUERY_ENDPOINT,
                    "http_status": 200,
                    "upstox_error_code": None,
                    "upstox_error_message": None,
                    "quote_available": True,
                    "reference_price": "24550.0",
                },
                {
                    "endpoint": PATH_ENDPOINT,
                    "http_status": 404,
                    "upstox_error_code": "UDAPI100060",
                    "upstox_error_message": "Resource not Found.",
                    "quote_available": False,
                    "reference_price": None,
                },
            ],
            "classification": "QUERY_FORM_AVAILABLE_ONLY",
            "timestamp_utc": "2026-09-22T08:00:00.000000+00:00",
        }
        path = comp.write_report(report, tmp_path)
        assert path.exists()
        assert path.name.startswith("upstox_oauth_ohlc_comparison_")
        loaded = json.loads(path.read_text(encoding="utf-8"))
        assert loaded["requests"][0]["endpoint"] == QUERY_ENDPOINT
        assert loaded["requests"][1]["upstox_error_code"] == "UDAPI100060"
        assert loaded["token_persisted"] is False


class TestClassification:
    def test_both_available(self) -> None:
        both = [_ok_record(True), _ok_record(True)]
        assert comp._classify(both) == "QUERY_AND_PATH_AVAILABLE"

    def test_query_only(self) -> None:
        records = [_ok_record(True), _ok_record(False)]
        assert comp._classify(records) == "QUERY_FORM_AVAILABLE_ONLY"

    def test_path_only(self) -> None:
        records = [_ok_record(False), _ok_record(True)]
        assert comp._classify(records) == "PATH_FORM_AVAILABLE_ONLY"

    def test_both_404(self) -> None:
        a = _ok_record(False)
        a["http_status"] = 404
        b = _ok_record(False)
        b["http_status"] = 404
        assert comp._classify([a, b]) == "BOTH_404"

    def test_unavailable(self) -> None:
        a = _ok_record(False)
        a["http_status"] = 200
        b = _ok_record(False)
        b["http_status"] = 200
        assert comp._classify([a, b]) == "QUOTE_UNAVAILABLE"


def _ok_record(quote_available: bool) -> dict[str, Any]:
    return {
        "endpoint": QUERY_ENDPOINT,
        "http_status": 200,
        "upstox_error_code": None,
        "upstox_error_message": None,
        "quote_available": quote_available,
        "reference_price": "24550.0" if quote_available else None,
    }