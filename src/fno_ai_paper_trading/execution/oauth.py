"""Minimum safe local Upstox OAuth callback for the controlled execution test (WS 7.9).

Scope
-----
This module acquires a fresh Upstox **access token** through the browser-based
authorization-code flow. It is deliberately minimal, local-only, and
order-free:

* The authorization starts in the operator's browser and ends on a **loopback
  only** HTTP server bound to ``127.0.0.1`` (never a public interface).
* The registered Redirect URL is ``http://127.0.0.1:8000/callback`` by default.
* The handler receives ``?code=...&state=...``, verifies ``state``, and hands
  the authorization code to the token exchange **in memory**.
* The exchange POSTs the form body (``code``, ``client_id``,
  ``client_secret``, ``redirect_uri``, ``grant_type=authorization_code``) to
  the Upstox token endpoint and returns :class:`TokenResponse`.

Safety guarantees
-----------------
* **No order code exists here.** Nothing in this module can place, modify or
  cancel an order; it only ever talks to the OAuth endpoints and returns a
  token to the caller.
* **Never persisted or exposed.** The authorization code, access token and
  client secret are never written to Git, reports, logs, the dashboard, source
  files, or printed by this module. The CLI helper writes the access token
  only to the git-ignored ``.env`` secret store and never echoes it.
* **Loopback only.** The callback server refuses to bind any non-loopback
  host, so the flow can never be reached by the public network.
* **No secret logging.** The HTTP handler suppresses its access log (the
  request path carries the authorization code) and the OAuth config ``repr``
  redacts the client secret.
* **A token never enables live execution.** ``UPSTOX_ACCESS_TOKEN`` is only
  the credential half of the gate; a real send still requires
  ``FNO_LIVE_EXECUTION_TEST_ENABLED=1``, an operator consent file whose sha256
  fingerprint matches the token, and ``--confirm-live-enablement`` in the same
  run (``execution/gate.py``).
"""
from __future__ import annotations

import hmac
import os
import secrets
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable
from urllib.parse import parse_qs, urlencode, urlsplit

from dotenv import load_dotenv

from fno_ai_paper_trading.execution.errors import UpstoxExecutionError
from fno_ai_paper_trading.utils.http import HttpError, http_request

UPSTOX_AUTHORIZE_PATH = "/v2/login/authorization/dialog"
UPSTOX_TOKEN_PATH = "/v2/login/authorization/token"
UPSTOX_BASE_URL = "https://api.upstox.com"
DEFAULT_CALLBACK_PORT = 8000
DEFAULT_REDIRECT_URI = "http://127.0.0.1:8000/callback"
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

_BLANK_HTML = """
<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>F&O AI — Upstox authorization</title>
<style>body{font-family:'Segoe UI',sans-serif;max-width:520px;margin:64px auto;padding:0 16px;
color:#24292f;line-height:1.5}code{background:#f0f2f4;padding:1px 5px;border-radius:4px}</style>
</head>
<body>
<h2>__TITLE__</h2>
<p>__MESSAGE__</p>
</body>
</html>
"""


@dataclass(frozen=True)
class UpstoxOAuthConfig:
    """OAuth client identity used by the authorization + token exchange.

    ``client_id`` is the Upstox application id (public, ``UPSTOX_API_KEY``);
    ``client_secret`` is the application secret (``UPSTOX_API_SECRET``) that is
    sent **only** in the token-exchange form body — never as an API header,
    never logged, redacted in ``repr``.
    """

    client_id: str
    client_secret: str
    redirect_uri: str = DEFAULT_REDIRECT_URI
    base_url: str = UPSTOX_BASE_URL
    timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "client_id", (self.client_id or "").strip())
        object.__setattr__(self, "client_secret", (self.client_secret or "").strip())
        object.__setattr__(self, "redirect_uri", (self.redirect_uri or DEFAULT_REDIRECT_URI).strip())
        object.__setattr__(self, "base_url", (self.base_url or UPSTOX_BASE_URL).rstrip("/"))
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be > 0")

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    @classmethod
    def from_env(cls) -> "UpstoxOAuthConfig":
        """Build the OAuth identity from the execution-side env variables.

        Uses ``UPSTOX_API_KEY`` (client id) and ``UPSTOX_API_SECRET`` (client
        secret). Never touches the analytics/data credentials (``FNO_UPSTOX_*``).
        """
        load_dotenv()
        return cls(
            client_id=os.getenv("UPSTOX_API_KEY", "").strip(),
            client_secret=os.getenv("UPSTOX_API_SECRET", "").strip(),
            base_url=(os.getenv("UPSTOX_BASE_URL", "") or UPSTOX_BASE_URL),
            timeout_seconds=float(os.getenv("UPSTOX_TIMEOUT_SECONDS", "") or 30.0),
        )

    def __repr__(self) -> str:
        return (
            f"UpstoxOAuthConfig(client_id={self.client_id!r}, "
            f"redirect_uri={self.redirect_uri!r}, base_url={self.base_url!r}, "
            "client_secret=<redacted>)"
        )


def new_oauth_state() -> str:
    """Return a fresh, unpredictable CSRF token for a callback session."""
    return secrets.token_urlsafe(32)


def authorize_url(config: UpstoxOAuthConfig, state: str) -> str:
    """Build the Upstox authorization URL the operator opens in the browser.

    The URL carries the public client id, the registered redirect URI, the
    ``code`` response type and the CSRF ``state``. It never carries any secret.
    """
    query = urlencode(
        {
            "client_id": config.client_id,
            "redirect_uri": config.redirect_uri,
            "response_type": "code",
            "state": state,
        }
    )
    return f"{config.base_url}{UPSTOX_AUTHORIZE_PATH}?{query}"


@dataclass(frozen=True)
class CallbackOutcome:
    """Result of parsing one redirect to the local callback endpoint."""

    state_ok: bool = False
    code: str = ""
    error: str = ""
    waiting: bool = False
    not_found: bool = False

    @property
    def completed(self) -> bool:
        return self.state_ok and bool(self.code)

    @property
    def http_status(self) -> int:
        if self.not_found:
            return 404
        if self.completed:
            return 200
        if self.waiting:
            return 200
        return 400


def resolve_callback_path(
    path: str,
    expected_state: str,
    *,
    callback_paths: tuple[str, ...] = ("/callback", "/callback/"),
) -> CallbackOutcome:
    """Parse a callback URL path into a :class:`CallbackOutcome`.

    Anything on ``/callback`` without a valid ``code`` + matching ``state`` is
    refused. ``/`` is a harmless "waiting" page; every other path is 404. The
    ``state`` comparison is constant-time (``hmac.compare_digest``).
    """
    parsed = urlsplit(path)
    if parsed.path.rstrip("/") in {p.rstrip("/") for p in callback_paths}:
        query = parse_qs(parsed.query, keep_blank_values=True)
        code = (query.get("code") or [None])[0]
        state = (query.get("state") or [""])[0]
        if not code:
            return CallbackOutcome(error="callback carried no authorization code")
        if not hmac.compare_digest(str(state or ""), str(expected_state or "")):
            return CallbackOutcome(error="callback state mismatch (possible CSRF)")
        return CallbackOutcome(state_ok=True, code=str(code))
    if parsed.path in ("/", ""):
        return CallbackOutcome(waiting=True)
    return CallbackOutcome(not_found=True)


def _render_page(title: str, message: str) -> bytes:
    page = _BLANK_HTML.replace("__TITLE__", title).replace("__MESSAGE__", message)
    return page.encode("utf-8")


def _page_for(outcome: CallbackOutcome) -> bytes:
    if outcome.not_found:
        return _render_page("404", "Not found — this is a local-only callback endpoint.")
    if outcome.completed:
        return _render_page(
            "Authorization received",
            "The authorization code was received and handed to the token exchange. "
            "You can close this window and return to the terminal.",
        )
    if outcome.waiting:
        return _render_page(
            "Waiting for authorization",
            "Waiting for Upstox to redirect back to this local callback. "
            "You can close this window if nothing is in progress.",
        )
    return _render_page(
        "Authorization failed",
        f"The local authorization step failed: <code>{outcome.error}</code>. "
        "Return to the terminal and retry.",
    )


class LocalCallbackServer:
    """A loopback-only HTTP server that waits for exactly one OAuth redirect.

    Binds strictly to a loopback address (default ``127.0.0.1:8000``). The
    handler resolves the callback path, never logs the request (the path carries
    the authorization code), and stores the result in memory only.
    """

    def __init__(self, expected_state: str, *, host: str = "127.0.0.1", port: int = DEFAULT_CALLBACK_PORT) -> None:
        if host not in _LOOPBACK_HOSTS:
            raise ValueError(
                f"callback server must bind to a loopback host, got {host!r}; "
                "the callback is local-only by design"
            )
        self.expected_state = expected_state
        self.host = host
        self._result = CallbackOutcome()
        self._event = threading.Event()
        self._server = ThreadingHTTPServer(
            (host, port), _handler_factory(self)
        )
        self._server.timeout = 0.25
        self.port: int = int(self._server.server_address[1])

    @property
    def result(self) -> CallbackOutcome:
        return self._result

    def serve_once(self, timeout: float) -> CallbackOutcome:
        """Serve loopback requests until a callback resolves or ``timeout`` passes."""
        self._event.clear()
        worker = threading.Thread(target=self._server.serve_forever, daemon=True)
        worker.start()
        try:
            self._event.wait(max(0.0, float(timeout)))
        finally:
            self._server.shutdown()
            self._server.server_close()
            worker.join(timeout=2.0)
        return self._result


def _handler_factory(owner: LocalCallbackServer):
    class CallbackHandler(BaseHTTPRequestHandler):
        #: The default BaseHTTPRequestHandler access log line would include the
        #: query string, which carries the authorization code — suppress it.
        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return

        def do_GET(self) -> None:
            outcome = resolve_callback_path(self.path, owner.expected_state)
            body = _page_for(outcome)
            self.send_response(outcome.http_status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            if outcome.completed or outcome.error:
                owner._result = outcome
                owner._event.set()

    return CallbackHandler


@dataclass(frozen=True)
class TokenResponse:
    """Result of the Upstox token exchange (never logged or persisted here)."""

    access_token: str
    refresh_token: str = ""


def exchange_code_for_token(
    config: UpstoxOAuthConfig,
    code: str,
    *,
    request: Callable = http_request,
) -> TokenResponse:
    """Exchange an authorization ``code`` for an access token (form POST).

    ``client_secret`` is sent only inside the form body to the token endpoint,
    as the OAuth spec requires. The returned token stays in memory; this
    function never prints, logs, or persists anything.
    """
    code = (code or "").strip()
    if not code:
        raise UpstoxExecutionError("no authorization code to exchange")
    url = f"{config.base_url}{UPSTOX_TOKEN_PATH}"
    body = urlencode(
        {
            "code": code,
            "client_id": config.client_id,
            "client_secret": config.client_secret,
            "redirect_uri": config.redirect_uri,
            "grant_type": "authorization_code",
        }
    ).encode("utf-8")
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
    }
    try:
        response = request(
            "POST",
            url,
            headers=headers,
            timeout=config.timeout_seconds,
            data=body,
        )
    except HttpError as exc:
        if exc.status == 401:
            raise UpstoxExecutionError(
                "Upstox token exchange rejected the client credentials (HTTP 401)"
            ) from exc
        if exc.status == 400:
            detail = exc.text[:200].strip()
            raise UpstoxExecutionError(
                f"Upstox token exchange rejected the request (HTTP 400): {detail}"
            ) from exc
        raise UpstoxExecutionError(
            f"Upstox token exchange failed (HTTP {exc.status})"
        ) from exc
    payload = response.json
    data = payload.get("data") or {}
    access = str(data.get("access_token") or payload.get("access_token") or "").strip()
    refresh = str(data.get("refresh_token") or payload.get("refresh_token") or "").strip()
    if not access:
        raise UpstoxExecutionError("Upstox token exchange returned no access_token")
    return TokenResponse(access_token=access, refresh_token=refresh)


def exchange_from_env(code: str, *, request: Callable = http_request) -> TokenResponse:
    """Convenience wrapper that builds the OAuth config from the environment."""
    return exchange_code_for_token(UpstoxOAuthConfig.from_env(), code, request=request)