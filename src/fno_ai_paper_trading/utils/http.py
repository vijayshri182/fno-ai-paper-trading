"""HTTP transport with a curl.exe backend on Windows.

Only JSON/plain-text GET (plus CSV bodies) is needed for the market-data
providers. On Windows the transport shells out to ``curl.exe`` (schannel TLS)
because the stdlib ``urllib`` TLS fingerprint is blocked by the Cloudflare WAF
in front of ``api.upstox.com`` (HTTP 403 / Error 1010 browser_signature_banned),
which made every Upstox request fail despite a valid access token. ``urllib``
remains the fallback on non-Windows platforms.

Everything returns either a typed :class:`HttpResponse` or raises
:class:`HttpError` for non-2xx responses, so callers never touch raw sockets.
The public interface (``http_request`` / ``http_get`` / ``HttpResponse`` /
``HttpError``) is unchanged from the urllib-only transport.
"""
from __future__ import annotations

import json
import os
import ssl
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping

# Module-level hook so tests can substitute a fake process runner.
_run_process = subprocess.run


def _use_curl() -> bool:
    """True when the Windows curl.exe backend should be used."""
    return sys.platform == "win32"


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

    return _transport_request(
        method,
        url,
        headers=headers,
        timeout=timeout,
        data=data,
        verify_ssl=verify_ssl,
    )


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


def _transport_request(
    method: str,
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    timeout: float = 10.0,
    data: bytes | None = None,
    verify_ssl: bool = True,
) -> HttpResponse:
    """Route a request to the active backend."""
    if _use_curl():
        return _curl_request(
            method,
            url,
            headers=headers,
            timeout=timeout,
            data=data,
            verify_ssl=verify_ssl,
        )
    return _urllib_request(
        method,
        url,
        headers=headers,
        timeout=timeout,
        data=data,
        verify_ssl=verify_ssl,
    )


def _curl_request(
    method: str,
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    timeout: float = 10.0,
    data: bytes | None = None,
    verify_ssl: bool = True,
) -> HttpResponse:
    """Execute a request via ``curl.exe``.

    Headers are passed through an ``@file`` argument so the access token never
    appears on the command line. Response headers are dumped to a scratch file
    and the body is captured on stdout, so both are parsed without any external
    encoding quirks.
    """
    with tempfile.TemporaryDirectory(prefix="fno_http_") as tmp:
        header_file = os.path.join(tmp, "request_headers.txt")
        dump_file = os.path.join(tmp, "response_headers.txt")
        with open(header_file, "w", encoding="utf-8", newline="\n") as fh:
            for name, value in (headers or {}).items():
                fh.write(f"{name}: {value}\n")

        cmd: list[str] = [
            "curl.exe",
            "--silent",
            "--show-error",
            "--location",
            "--max-time",
            str(timeout),
            "-X",
            method,
            "-H",
            f"@{header_file}",
            "-D",
            dump_file,
            "-o",
            "-",
        ]
        if not verify_ssl:
            cmd.append("--insecure")
        if data is not None:
            body_file = os.path.join(tmp, "request_body.bin")
            with open(body_file, "wb") as fh:
                fh.write(data)
            cmd.extend(["--data-binary", f"@{body_file}"])
        cmd.append(url)

        result = _run_process(cmd, capture_output=True)

        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace")
            raise OSError(
                f"curl failed (exit {result.returncode}) for {url}: {stderr}"
            )

        status, response_headers = _parse_curl_headers(dump_file)
        if status is None:
            raise OSError(f"curl returned no HTTP status line for {url}")
        if status >= 400:
            raise HttpError(status, url, result.stdout)
        return HttpResponse(status=status, body=result.stdout, url=url, headers=response_headers)


def _urllib_request(
    method: str,
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    timeout: float = 10.0,
    data: bytes | None = None,
    verify_ssl: bool = True,
) -> HttpResponse:
    """Fallback backend (non-Windows) using the stdlib urllib."""
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


def _parse_curl_headers(path: str) -> tuple[int | None, dict[str, str]]:
    """Parse the ``curl -D`` header dump into ``(status, headers)``.

    When ``--location`` follows redirects the dump contains one ``HTTP/...``
    block per hop; only the last block (the final response) is kept.
    """
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError:
        return None, {}

    status: int | None = None
    headers: dict[str, str] = {}
    for line in raw.split(b"\r\n"):
        text = line.strip(b"\r\n").decode("utf-8", errors="replace")
        if not text:
            continue
        if text.startswith("HTTP/"):
            parts = text.split(" ", 2)
            try:
                status = int(parts[1])
            except (IndexError, ValueError):
                status = None
            continue
        if ":" in text:
            name, _, value = text.partition(":")
            headers[name.strip().lower()] = value.strip()
    return status, headers