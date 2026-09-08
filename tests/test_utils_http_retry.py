"""Tests for the curl.exe HTTP transport and retry helper."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from fno_ai_paper_trading.utils import http as http_module
from fno_ai_paper_trading.utils.http import HttpError, http_get, is_retryable_status
from fno_ai_paper_trading.utils.retry import RetryExhausted, describe_last_error, retry_call


# ---------------------------------------------------------------------------
# HTTP transport
# ---------------------------------------------------------------------------

def _fake_curl(status: int, body: bytes, headers: dict[str, str] | None = None):
    """Build a fake ``subprocess.run`` that emulates a curl.exe invocation.

    Emulates ``curl -D <file> -o -`` by writing an ``HTTP/1.1`` block into the
    ``-D`` target file and returning ``body`` on stdout.
    """

    def runner(cmd, **kwargs):
        dump_file = None
        for index, arg in enumerate(cmd):
            if arg == "-D":
                dump_file = cmd[index + 1]
        assert dump_file is not None, "expected -D <headers-dump> in the curl command"
        reason = "OK"
        if status == 429:
            reason = "Too Many Requests"
        elif status < 200 or status >= 300:
            reason = "Error"
        lines = [f"HTTP/1.1 {status} {reason}".rstrip()]
        for name, value in (headers or {"content-type": "application/json"}).items():
            lines.append(f"{name}: {value}")
        Path(dump_file).write_bytes(("\r\n".join(lines) + "\r\n").encode("utf-8"))
        return subprocess.CompletedProcess(cmd, 0, stdout=body, stderr=b"")

    return runner


class TestHttpGet:
    def _use_curl(self, monkeypatch) -> None:
        monkeypatch.setattr(http_module, "_use_curl", lambda: True)

    def test_get_returns_typed_response(self, monkeypatch) -> None:
        payload = {"last_price": 24200.5}
        body = json.dumps(payload).encode()
        monkeypatch.setattr(http_module, "_run_process", _fake_curl(200, body))
        self._use_curl(monkeypatch)
        response = http_get("http://example.invalid/quote")
        assert response.status == 200
        assert response.json == payload
        assert "last_price" in response.text

    def test_query_params_are_encoded(self, monkeypatch) -> None:
        captured: dict[str, list[str]] = {}

        def runner(cmd, **kwargs):
            captured["cmd"] = cmd
            return _fake_curl(200, b"{}")(cmd, **kwargs)

        monkeypatch.setattr(http_module, "_run_process", runner)
        self._use_curl(monkeypatch)
        http_get("http://example.invalid/quote", params={"i": "NSE:26000", "x": "a b"})
        assert "?i=NSE%3A26000&x=a+b" in captured["cmd"][-1]

    def test_headers_are_forwarded(self, monkeypatch) -> None:
        captured: dict[str, str] = {}

        def runner(cmd, **kwargs):
            header_arg = next(arg for arg in cmd if arg.startswith("@"))
            captured["header_file"] = Path(header_arg[1:]).read_text(encoding="utf-8")
            captured["cmd"] = cmd
            return _fake_curl(200, b"{}")(cmd, **kwargs)

        monkeypatch.setattr(http_module, "_run_process", runner)
        self._use_curl(monkeypatch)
        http_get("http://example.invalid/quote", headers={"X-Kite-Version": "3"})
        assert "X-Kite-Version: 3" in captured["header_file"]
        assert "GET" in captured["cmd"]

    def test_error_status_raises_http_error(self, monkeypatch) -> None:
        body = b'{"error": "too many requests"}'
        monkeypatch.setattr(http_module, "_run_process", _fake_curl(429, body))
        self._use_curl(monkeypatch)
        with pytest.raises(HttpError) as excinfo:
            http_get("http://example.invalid/quote")
        assert excinfo.value.status == 429
        assert "too many requests" in excinfo.value.text


class TestIsRetryableStatus:
    def test_rate_limit_and_fivexx_are_retryable(self) -> None:
        assert is_retryable_status(429) is True
        assert is_retryable_status(500) is True
        assert is_retryable_status(503) is True
        assert is_retryable_status(504) is True

    def test_other_statuses_are_not_retryable(self) -> None:
        assert is_retryable_status(400) is False
        assert is_retryable_status(401) is False
        assert is_retryable_status(404) is False
        assert is_retryable_status(200) is False


# ---------------------------------------------------------------------------
# Retry
# ---------------------------------------------------------------------------

class _NoSleep:
    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


class TestRetryCall:
    def test_success_on_first_attempt(self) -> None:
        calls = []

        def fn():
            calls.append(1)
            return "ok"

        assert retry_call(fn, sleep=_NoSleep()) == "ok"
        assert len(calls) == 1

    def test_retries_transient_then_succeeds(self) -> None:
        sleep = _NoSleep()
        calls = []

        def fn():
            calls.append(1)
            if len(calls) == 1:
                raise ValueError("boom")
            return "recovered"

        assert retry_call(fn, attempts=3, delay=0.5, sleep=sleep) == "recovered"
        assert len(calls) == 2
        assert sleep.calls == [0.5]

    def test_exhaustion_raises_with_cause(self) -> None:
        sleep = _NoSleep()
        calls = []

        def fn():
            calls.append(1)
            raise ValueError("boom")

        with pytest.raises(RetryExhausted) as excinfo:
            retry_call(fn, attempts=4, delay=0.1, backoff=2.0, max_delay=5.0, sleep=sleep)
        assert len(calls) == 4
        assert isinstance(describe_last_error(excinfo.value), ValueError)
        assert sleep.calls == [0.1, 0.2, 0.4]

    def test_only_retries_opted_in_exceptions(self) -> None:
        calls = []

        def fn():
            calls.append(1)
            raise ValueError("not retried")

        with pytest.raises(ValueError):
            retry_call(fn, attempts=3, exceptions=(KeyError,), sleep=_NoSleep())
        assert len(calls) == 1

    def test_attempts_must_be_positive(self) -> None:
        with pytest.raises(ValueError):
            retry_call(lambda: None, attempts=0)