"""Tests for the dependency-free HTTP transport and retry helper."""
from __future__ import annotations

import io
import json
from urllib.error import HTTPError

import pytest

from fno_ai_paper_trading.utils.http import HttpError, http_get, is_retryable_status
from fno_ai_paper_trading.utils.retry import RetryExhausted, describe_last_error, retry_call


# ---------------------------------------------------------------------------
# HTTP transport
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, status: int, body: bytes, headers: dict | None = None) -> None:
        self.status = status
        self._body = body
        self.headers = headers or {"content-type": "application/json"}

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *args) -> None:
        return None


class TestHttpGet:
    def _patch(self, monkeypatch, fake):
        captured = {}

        def fake_urlopen(request, **kwargs):
            captured["request"] = request
            captured["headers"] = dict(request.header_items())
            return fake

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        return captured

    def test_get_returns_typed_response(self, monkeypatch) -> None:
        payload = {"last_price": 24200.5}
        body = json.dumps(payload).encode()
        self._patch(monkeypatch, _FakeResponse(200, body))
        response = http_get("http://example.invalid/quote")
        assert response.status == 200
        assert response.json == payload
        assert "last_price" in response.text

    def test_query_params_are_encoded(self, monkeypatch) -> None:
        captured = self._patch(monkeypatch, _FakeResponse(200, b"{}"))
        http_get("http://example.invalid/quote", params={"i": "NSE:26000", "x": "a b"})
        assert "?i=NSE%3A26000&x=a+b" in captured["request"].full_url

    def test_headers_are_forwarded(self, monkeypatch) -> None:
        captured = self._patch(monkeypatch, _FakeResponse(200, b"{}"))
        http_get("http://example.invalid/quote", headers={"X-Kite-Version": "3"})
        forwarded = {k.lower(): v for k, v in captured["headers"].items()}
        assert forwarded["x-kite-version"] == "3"

    def test_error_status_raises_http_error(self, monkeypatch) -> None:
        body = b'{"error": "too many requests"}'

        def raise_http(*args, **kwargs):
            raise HTTPError("http://example.invalid/quote", 429, "Too Many Requests", {}, io.BytesIO(body))

        monkeypatch.setattr("urllib.request.urlopen", raise_http)
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