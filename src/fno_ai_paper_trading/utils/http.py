"""Dependency-free HTTP transport on top of the standard library.

Only JSON/plain-text GET (plus CSV bodies) is needed for the market-data
providers. Everything here is synchronous and returns either a typed
:class:`HttpResponse` or raises :class:`HttpError` for non-2xx responses, so
callers never touch raw sockets.
"""
from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping


class HttpError(Exception):
    """Raised when a request completes with a non-2xx status code."""

    def __init__(self, status: int, url: str, body: bytes = b"") -> None:
        self.status = status
        self.url = url
        self.body = body
        super().__init__(f"HTTP {status} for {url}")

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


def is_retryable_status(status: int) -> bool:
    """True when ``status`` is a transient 429 or 5xx response worth retrying."""
    return status == 429 or 500 <= status <= 599


@dataclass(frozen=True)
class HttpResponse:
    """A completed HTTP response."""

    status: int
    body: bytes
    url: str
    headers: Mapping[str, str]

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    @property
    def json(self) -> Any:
        return json.loads(self.text)


def http_request(
    method: str,
    url: str,
    *,
    params: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
    timeout: float = 10.0,
    data: bytes | None = None,
    verify_ssl: bool = True,
) -> HttpResponse:
    """Perform a synchronous HTTP request.

    ``params`` are URL-encoded onto the query string. ``headers`` are sent as-is.
    Non-2xx responses raise :class:`HttpError`.
    """
    if params:
        query = urllib.parse.urlencode(params)
        url = f"{url}?{query}" if "?" not in url else f"{url}&{query}"

    request_headers = {k: v for k, v in (headers or {}).items()}
    request = urllib.request.Request(url, data=data, headers=request_headers, method=method)
    context = ssl.create_default_context() if verify_ssl else ssl._create_unverified_context()

    try:
        with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
            status = int(response.status)
            body = response.read()
            response_headers = {k.lower(): v for k, v in response.headers.items()}
    except urllib.error.HTTPError as exc:
        raise HttpError(int(exc.code), url, exc.read()) from exc

    return HttpResponse(status=status, body=body, url=url, headers=response_headers)


def http_get(
    url: str,
    *,
    params: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
    timeout: float = 10.0,
    verify_ssl: bool = True,
) -> HttpResponse:
    """Perform a GET request returning a :class:`HttpResponse` (bodies always bytes)."""
    return http_request(
        "GET",
        url,
        params=params,
        headers=headers,
        timeout=timeout,
        verify_ssl=verify_ssl,
    )