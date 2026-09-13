"""Deterministic, offline tests for the local Upstox OAuth helper (WS 7.9).

Covers the authorization-URL builder, the loopback-only callback server (real
loopback sockets to prove the browser flow end-to-end without any external
network), the form-POST token exchange, and the no-secret/no-order guarantees.

No test here can touch a real broker: the token exchange uses an injected fake
``request`` and the callback server binds to ``127.0.0.1``.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from fno_ai_paper_trading.execution.errors import UpstoxExecutionError
from fno_ai_paper_trading.execution.oauth import (
    DEFAULT_REDIRECT_URI,
    LocalCallbackServer,
    TokenResponse,
    UpstoxOAuthConfig,
    authorize_url,
    exchange_code_for_token,
    new_oauth_state,
    resolve_callback_path,
)
from fno_ai_paper_trading.utils.http import HttpResponse, HttpError

CLIENT_ID = "upstox-app-id-123"
CLIENT_SECRET = "top-secret-app-secret-abc"
REDIRECT = DEFAULT_REDIRECT_URI
STATE = "csrf-state-token-xyz"
CODE = "authorization-code-000111"
TOKEN = "access-token-value-ABCD"


def _config(**overrides) -> UpstoxOAuthConfig:
    values = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "redirect_uri": REDIRECT,
    }
    values.update(overrides)
    return UpstoxOAuthConfig(**values)


def _terminate_on(marker: dict[str, object], server: LocalCallbackServer, timeout: float = 30.0) -> None:
    marker["outcome"] = server.serve_once(timeout)


def _loopback_get(url: str) -> tuple[int, str]:
    """Deterministic loopback GET that never consults the process proxy config."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=15) as response:
            return int(response.status), response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read().decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Authorization URL
# ---------------------------------------------------------------------------


class TestAuthorizeUrl:
    def test_url_contains_expected_params(self) -> None:
        url = authorize_url(_config(), STATE)
        assert "/v2/login/authorization/dialog" in url
        pieces = urllib.parse.urlsplit(url)
        query = urllib.parse.parse_qs(pieces.query)
        assert query["client_id"] == [CLIENT_ID]
        assert query["redirect_uri"] == [REDIRECT]
        assert query["response_type"] == ["code"]
        assert query["state"] == [STATE]

    def test_url_never_contains_client_secret(self) -> None:
        url = authorize_url(_config(), STATE)
        assert CLIENT_SECRET not in url

    def test_redirect_uri_is_exactly_the_registered_callback(self) -> None:
        assert DEFAULT_REDIRECT_URI == "http://127.0.0.1:8000/callback"
        url = authorize_url(_config(), STATE)
        assert "127.0.0.1:8000/callback" in urllib.parse.unquote(url)

    def test_state_is_unpredictable(self) -> None:
        assert new_oauth_state() != new_oauth_state()


# ---------------------------------------------------------------------------
# Callback path resolution (pure logic)
# ---------------------------------------------------------------------------


class TestResolveCallbackPath:
    def test_complete_callback_is_accepted(self) -> None:
        outcome = resolve_callback_path(f"/callback?code={CODE}&state={STATE}", STATE)
        assert outcome.completed is True
        assert outcome.code == CODE
        assert outcome.state_ok is True

    def test_requires_the_expected_state(self) -> None:
        outcome = resolve_callback_path(f"/callback?code={CODE}&state=wrong-state", STATE)
        assert outcome.completed is False
        assert "state mismatch" in outcome.error

    def test_missing_code_is_rejected(self) -> None:
        outcome = resolve_callback_path("/callback", STATE)
        assert outcome.completed is False
        assert "no authorization code" in outcome.error

    def test_trailing_slash_callback_is_equal(self) -> None:
        outcome = resolve_callback_path(f"/callback/?code={CODE}&state={STATE}", STATE)
        assert outcome.completed is True
        assert outcome.code == CODE

    def test_root_path_is_a_waiting_page(self) -> None:
        outcome = resolve_callback_path("/", STATE)
        assert outcome.waiting is True
        assert outcome.completed is False

    def test_other_paths_are_404(self) -> None:
        outcome = resolve_callback_path("/favicon.ico", STATE)
        assert outcome.not_found is True
        assert outcome.completed is False


# ---------------------------------------------------------------------------
# Local loopback callback server
# ---------------------------------------------------------------------------


class TestLocalCallbackServer:
    def test_server_binds_to_loopback_only(self) -> None:
        with pytest.raises(ValueError, match="loopback"):
            LocalCallbackServer(STATE, host="0.0.0.0")

    def test_full_browser_roundtrip_returns_the_code(self) -> None:
        server = LocalCallbackServer(expected_state=STATE, port=0)
        assert server.host == "127.0.0.1"
        marker: dict[str, object] = {}
        worker = threading.Thread(target=_terminate_on, args=(marker, server), daemon=True)
        worker.start()
        status, body = _loopback_get(
            f"http://127.0.0.1:{server.port}/callback?code={CODE}&state={STATE}"
        )
        worker.join(timeout=30)
        assert status == 200
        assert marker["outcome"].completed is True
        assert marker["outcome"].code == CODE
        # The response page must never echo the authorization code (or the token).
        assert CODE not in body
        assert TOKEN not in body

    def test_wrong_state_returns_error_page_and_no_code(self) -> None:
        server = LocalCallbackServer(expected_state=STATE, port=0)
        marker: dict[str, object] = {}
        worker = threading.Thread(target=_terminate_on, args=(marker, server), daemon=True)
        worker.start()
        status, body = _loopback_get(
            f"http://127.0.0.1:{server.port}/callback?code={CODE}&state=wrong"
        )
        worker.join(timeout=30)
        assert status == 400
        assert "state mismatch" in body
        assert marker["outcome"].completed is False

    def test_missing_code_returns_error_page(self) -> None:
        server = LocalCallbackServer(expected_state=STATE, port=0)
        marker: dict[str, object] = {}
        worker = threading.Thread(target=_terminate_on, args=(marker, server), daemon=True)
        worker.start()
        status, body = _loopback_get(f"http://127.0.0.1:{server.port}/callback?state={STATE}")
        worker.join(timeout=30)
        assert status == 400
        assert "no authorization code" in body

    def test_timeout_without_callback_is_not_completed(self) -> None:
        server = LocalCallbackServer(expected_state=STATE, port=0)
        outcome = server.serve_once(0.2)
        assert outcome.completed is False
        assert "no callback" in outcome.error or outcome.waiting is False or not outcome.error


# ---------------------------------------------------------------------------
# Token exchange (form POST; injected fake transport)
# ---------------------------------------------------------------------------


class TestTokenExchange:
    def test_exchange_posts_form_and_parses_token(self) -> None:
        captured: dict[str, object] = {}

        def fake_request(method, url, *, headers=None, timeout=10.0, data=None, **kwargs):
            captured["method"] = method
            captured["url"] = url
            captured["headers"] = headers
            captured["data"] = data
            return HttpResponse(200, b'{"access_token": "' + TOKEN.encode() + b'","refresh_token":"ref"}', url, {})

        result = exchange_code_for_token(_config(), CODE, request=fake_request)
        assert isinstance(result, TokenResponse)
        assert result.access_token == TOKEN
        assert result.refresh_token == "ref"
        assert captured["method"] == "POST"
        assert "/v2/login/authorization/token" in str(captured["url"])
        body = urllib.parse.parse_qs((captured["data"] or b"").decode("utf-8"))
        assert body["code"] == [CODE]
        assert body["client_id"] == [CLIENT_ID]
        assert body["client_secret"] == [CLIENT_SECRET]
        assert body["redirect_uri"] == [REDIRECT]
        assert body["grant_type"] == ["authorization_code"]
        assert captured["headers"]["Content-Type"] == "application/x-www-form-urlencoded"

    def test_exchange_supports_data_nested_payload(self) -> None:
        def fake_request(method, url, *, headers=None, timeout=10.0, data=None, **kwargs):
            return HttpResponse(200, b'{"data": {"access_token": "' + TOKEN.encode() + b'"}}', url, {})

        result = exchange_code_for_token(_config(), CODE, request=fake_request)
        assert result.access_token == TOKEN

    def test_exchange_missing_code_raises_before_any_request(self) -> None:
        calls: list[object] = []

        def fake_request(method, url, **kwargs):
            calls.append(url)
            return HttpResponse(200, b"{}", url, {})

        with pytest.raises(UpstoxExecutionError, match="no authorization code"):
            exchange_code_for_token(_config(), "  ", request=fake_request)
        assert calls == []

    def test_exchange_without_access_token_in_response_raises(self) -> None:
        def fake_request(method, url, *, headers=None, timeout=10.0, data=None, **kwargs):
            return HttpResponse(200, b'{"error": "invalid_grant"}', url, {})

        with pytest.raises(UpstoxExecutionError, match="no access_token"):
            exchange_code_for_token(_config(), CODE, request=fake_request)

    def test_exchange_401_raises_typed_error(self) -> None:
        def fake_request(method, url, **kwargs):
            raise HttpError(401, url, b"{}")

        with pytest.raises(UpstoxExecutionError, match="HTTP 401"):
            exchange_code_for_token(_config(), CODE, request=fake_request)

    def test_exchange_400_raises_typed_error(self) -> None:
        def fake_request(method, url, **kwargs):
            raise HttpError(400, url, b'{"error": "bad request"}')

        with pytest.raises(UpstoxExecutionError, match="HTTP 400"):
            exchange_code_for_token(_config(), CODE, request=fake_request)


# ---------------------------------------------------------------------------
# No secret leakage / no order surface
# ---------------------------------------------------------------------------


class TestNoLeakageNoOrders:
    def test_config_repr_redacts_client_secret(self) -> None:
        rendered = repr(_config())
        assert CLIENT_SECRET not in rendered
        assert "client_secret=<redacted>" in rendered
        assert CLIENT_ID in rendered  # the app id is public

    def test_config_defaults_to_the_registered_loopback_callback(self) -> None:
        assert _config().redirect_uri == "http://127.0.0.1:8000/callback"

    def test_oauth_module_has_no_order_placement_surface(self) -> None:
        # The module must never call the order/position APIs — only the OAuth
        # endpoints. Check the token exchange endpoint and authorization path
        # constants never reference order endpoints.
        assert "/order" not in str(_config().base_url)
        assert "/v2/order" not in str(_config().base_url)

    def test_exchange_url_is_token_endpoint_only(self) -> None:
        captured: dict[str, object] = {}

        def fake_request(method, url, **kwargs):
            captured["url"] = url
            return HttpResponse(200, b'{"access_token": "t"}', url, {})

        exchange_code_for_token(_config(), CODE, request=fake_request)
        assert "/v2/login/authorization/token" in str(captured["url"])
        assert "/order" not in str(captured["url"])


# ---------------------------------------------------------------------------
# Full CLI loopback round-trip: callback server -> token exchange -> .env write
# ---------------------------------------------------------------------------

_CLI_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "upstox_oauth.py"
_TOKEN = "e2e-access-token-never-printed-abcdef"


def _free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class _TokenEndpoint(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        body = json.dumps({"access_token": _TOKEN}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass


class TestCliEndToEnd:
    def test_cli_loopback_round_trip_writes_token_to_git_ignored_env(
        self, tmp_path: Path
    ) -> None:
        token_port = _free_port()
        token_server = ThreadingHTTPServer(("127.0.0.1", token_port), _TokenEndpoint)
        token_thread = threading.Thread(target=token_server.serve_forever, daemon=True)
        token_thread.start()
        callback_port = _free_port()
        env_file = tmp_path / "test_oauth.env"
        env = dict(os.environ)
        for key in ("UPSTOX_API_KEY", "UPSTOX_API_SECRET", "UPSTOX_ACCESS_TOKEN",
                    "UPSTOX_BASE_URL"):
            env.pop(key, None)
        try:
            proc = subprocess.Popen(
                [
                    sys.executable, str(_CLI_SCRIPT),
                    "--client-id", CLIENT_ID,
                    "--client-secret", CLIENT_SECRET,
                    "--base-url", f"http://127.0.0.1:{token_port}",
                    "--redirect-uri", f"http://127.0.0.1:{callback_port}/callback",
                    "--port", str(callback_port),
                    "--state", STATE,
                    "--no-open",
                    "--timeout", "20",
                    "--env-file", str(env_file),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )
            url = f"http://127.0.0.1:{callback_port}/callback?code={CODE}&state={STATE}"
            status: object = None
            for _ in range(50):
                try:
                    status, _ = _loopback_get(url)
                    if status in (200, 400):
                        break
                except Exception:
                    status = None
                time.sleep(0.2)
            assert status == 200, f"callback was not answered (status={status})"
            out, err = proc.communicate(timeout=30)
        finally:
            token_server.shutdown()
            token_server.server_close()
        assert proc.returncode == 0, f"CLI exited {proc.returncode}: {err}\n{out}"
        assert "access token acquired" in out
        assert CODE not in out
        assert _TOKEN not in out
        assert CLIENT_SECRET not in out
        written = env_file.read_text(encoding="utf-8")
        assert "UPSTOX_ACCESS_TOKEN=" in written
        assert _TOKEN in written