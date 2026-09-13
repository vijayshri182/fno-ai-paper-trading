"""WS 7.9 local Upstox OAuth token helper (controlled execution test).

Purpose: obtain a fresh ``UPSTOX_ACCESS_TOKEN`` through Upstox's
authorization-code flow using a **loopback-only** callback server, so the
operator never has to handle the token in a shared terminal or paste it into a
remote/CI pipeline.

Flow (all local; no real order is ever involved):

    Browser -> Upstox authorization -> http://127.0.0.1:8000/callback?code=...
        -> local callback handler (127.0.0.1 only) -> token exchange -> token

Safety rules honoured here:

* The callback server binds to ``127.0.0.1`` only (never a public interface).
* The authorization code and access token are **never printed, logged, or
  written to reports/dashboard/source files**. The default is to store the
  access token in the git-ignored ``.env`` secret store (``--no-write-env``
  disables even that, leaving the token only in this process — unusable outside
  it, by design, without an explicit operator choice).
* The client secret (``UPSTOX_API_SECRET``) is sent only in the token-exchange
  form body and is never echoed.
* This script places **no orders** — it only ever talks to the OAuth endpoints.
* A fresh token is NOT an enablement switch: real sends still require
  ``FNO_LIVE_EXECUTION_TEST_ENABLED=1``, an operator consent file whose sha256
  fingerprint matches the token, and ``--confirm-live-enablement`` in the same
  run (see ``project_state`` / ``run_live_execution_test.py``).
"""
from __future__ import annotations

import argparse
import secrets
import sys
import webbrowser
from pathlib import Path

from dotenv import load_dotenv, set_key

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from fno_ai_paper_trading.execution.oauth import (  # noqa: E402
    DEFAULT_CALLBACK_PORT,
    DEFAULT_REDIRECT_URI,
    LocalCallbackServer,
    UpstoxOAuthConfig,
    authorize_url,
    exchange_code_for_token,
    new_oauth_state,
)


def _build_config(args) -> UpstoxOAuthConfig:
    load_dotenv()
    client_id = (args.client_id or "").strip()
    client_secret = (args.client_secret or "").strip()
    if not client_id:
        raise ValueError("missing UPSTOX_API_KEY (the Upstox application id); set it in .env or pass --client-id")
    if not client_secret:
        raise ValueError("missing UPSTOX_API_SECRET (the Upstox application secret); set it in .env or pass --client-secret")
    return UpstoxOAuthConfig(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=args.redirect_uri,
        base_url=(args.base_url or "https://api.upstox.com").rstrip("/"),
        timeout_seconds=args.timeout,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="upstox_oauth",
        description="Local Upstox OAuth token helper (loopback-only; no orders).",
        epilog="The token is never printed or logged; it is stored in the git-ignored .env unless --no-write-env.",
    )
    parser.add_argument("--client-id", default="", help="Upstox application id (env: UPSTOX_API_KEY)")
    parser.add_argument("--client-secret", default="", help="Upstox application secret (env: UPSTOX_API_SECRET)")
    parser.add_argument("--redirect-uri", default=DEFAULT_REDIRECT_URI,
                        help="registered redirect URI (default http://127.0.0.1:8000/callback)")
    parser.add_argument("--port", type=int, default=DEFAULT_CALLBACK_PORT,
                        help="local callback port (must match the registered redirect URI)")
    parser.add_argument("--base-url", default="https://api.upstox.com", help="Upstox endpoint base URL")
    parser.add_argument("--timeout", type=float, default=300.0,
                        help="seconds to wait for the browser callback before failing")
    parser.add_argument("--no-open", action="store_true", help="do not auto-open the browser; print the authorization URL")
    parser.add_argument("--write-env", dest="write_env", action="store_true", default=True)
    parser.add_argument("--no-write-env", dest="write_env", action="store_false",
                        help="do NOT store the access token in .env (token stays in-process only, never printed)")
    parser.add_argument("--env-file", default=str(REPO_ROOT / ".env"),
                        help="git-ignored .env file to update (default: repo .env)")
    parser.add_argument("--state", default="",
                        help="optional explicit CSRF state (default: random; only set for reproducible tests)")
    args = parser.parse_args(argv)

    try:
        config = _build_config(args)
    except ValueError as exc:
        print(f"OAuth configuration error: {exc}", file=sys.stderr)
        print("The client id (UPSTOX_API_KEY) is public; the client secret (UPSTOX_API_SECRET) must never be echoed.", file=sys.stderr)
        return 2

    state = args.state.strip() or new_oauth_state()
    url = authorize_url(config, state)

    print("Upstox OAuth (local-only callback)")
    print(f"  client id      : configured (UPSTOX_API_KEY)")  # the id itself is public; not echoed here
    print(f"  client secret  : configured (UPSTOX_API_SECRET), never echoed")
    print(f"  redirect URI   : {config.redirect_uri}")
    print(f"  callback server: 127.0.0.1:{args.port} (loopback only)")
    print(f"  no orders      : this step only obtains a token; it cannot place a trade")

    if not args.no_open:
        opened = webbrowser.open(url)
        if opened:
            print("  browser        : authorization page opened automatically")
        else:
            print(f"  authorization URL (contains only the public client id):\n    {url}")
    else:
        print(f"  authorization URL (contains only the public client id):\n    {url}")

    server = LocalCallbackServer(expected_state=state, port=args.port)
    outcome = server.serve_once(args.timeout)
    if not outcome.completed:
        print(f"authorization failed or timed out: {outcome.error or 'no callback received'}", file=sys.stderr)
        return 2

    print("authorization code received — exchanging for an access token…")
    token = exchange_code_for_token(config, outcome.code)
    print("access token acquired.")
    if token.refresh_token:
        print("refresh token acquired (used only by the Upstox app, not stored here).")

    stored_at: str | None = None
    if args.write_env:
        destination = Path(args.env_file)
        destination.parent.mkdir(parents=True, exist_ok=True)
        set_key(str(destination), "UPSTOX_ACCESS_TOKEN", token.access_token)
        stored_at = str(destination)
        print(f"  stored in      : {stored_at} (git-ignored; never committed)")
    else:
        print("  stored in      : this process only (--no-write-env); the token was not written anywhere")

    print()
    print("REMINDERS:")
    print("  * Neither the authorization code nor the access token was printed, logged, or")
    print("    written to reports/dashboard/source files.")
    if stored_at:
        print("  * The token lives in the git-ignored .env secret store only; keep that file private.")
    print("  * A token does NOT enable live execution: a real send needs FNO_LIVE_EXECUTION_TEST_ENABLED=1,")
    print("    an operator consent file whose fingerprint matches this token, and --confirm-live-enablement")
    print("    in the same run. This helper placed no order and cannot place one.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())