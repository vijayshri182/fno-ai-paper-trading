"""Read-only Upstox OAuth -> quote diagnostic (no orders, no persistence).

Purpose
-------
Run exactly one read-only market quote after a fresh browser OAuth, keep the
access token in process memory only, and leave behind a timestamped JSON
evidence report under ``reports/execution/``:

    OAuth (local loopback) -> in-memory access token -> ONE quote for
    ``NSE_FO|56983`` -> diagnostic report -> exit.

This deliberately reuses the existing execution layer instead of re-implementing
any part of it:

* ``execution/oauth.UpstoxOAuthConfig.from_env()`` — existing OAuth identity.
* ``execution/oauth.LocalCallbackServer`` — existing loopback-only callback.
* ``execution/oauth.authorize_url()`` / ``exchange_code_for_token()`` — existing flow.
* ``execution/upstox.UpstoxCredentials`` / ``UpstoxExecutionAdapter`` — existing
  Upstox adapter (``quote()`` -> ``GET /v2/market-quote/ohlc?instrument_key={key}&interval=1d``).

Safety properties (enforced here, not merely documented):

* The access token, authorization code and client secret are never printed,
  logged, or written anywhere. ``token_persisted`` and ``token_value_exposed``
  are always ``false``.
* Exactly one quote request is made: no retries, no sibling comparisons.
* No BUY/SELL order path exists here; ``orders_placed`` is always ``false``.
* No live-trading gate is required (quotes are read-only) and none is bypassed
  or weakened; ``live_trading_enabled`` is always ``false``.
* No scheduler is started; ``scheduler_enabled`` is always ``false``.
* The callback server binds to ``127.0.0.1`` only (existing implementation).
"""
from __future__ import annotations

import argparse
import json
import sys
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from fno_ai_paper_trading.data.errors import MarketDataError  # noqa: E402
from fno_ai_paper_trading.execution.errors import UpstoxExecutionError  # noqa: E402
from fno_ai_paper_trading.execution.oauth import (  # noqa: E402
    DEFAULT_CALLBACK_PORT,
    DEFAULT_REDIRECT_URI,
    LocalCallbackServer,
    UpstoxOAuthConfig,
    authorize_url,
    exchange_code_for_token,
    new_oauth_state,
)
from fno_ai_paper_trading.execution.upstox import (  # noqa: E402
    UpstoxCredentials,
    UpstoxExecutionAdapter,
)
from fno_ai_paper_trading.utils.http import (  # noqa: E402
    HttpError,
    HttpResponse,
    http_request,
)

INSTRUMENT_KEY = "NSE_FO|56983"
QUOTE_ENDPOINT_PREFIX = "/v2/market-quote/ohlc"
QUOTE_ENDPOINT_QUERY = "?instrument_key={key}&interval=1d"
DEFAULT_REPORT_DIR = REPO_ROOT / "reports" / "execution"


class _QuoteObserver:
    """Records the single quote response/error without ever touching the token.

    Observes only the quote transport: the response status and, on a non-2xx
    response, the Upstox ``errors[0]`` ``error_code``/``message`` fields. The
    access token lives in the request headers and never passes through here.
    """

    def __init__(self, request: Callable) -> None:
        self._request = request
        self.status: int | None = None
        self.error_code: str | None = None
        self.error_message: str | None = None

    def __call__(self, method: str, url: str, *, headers=None, timeout: float = 10.0, data: bytes | None = None, **kwargs):
        try:
            response = self._request(method, url, headers=headers, timeout=timeout, data=data, **kwargs)
        except HttpError as exc:
            self.status = exc.status
            self._capture_upstox_error(exc.text)
            raise
        self.status = response.status
        return response

    def _capture_upstox_error(self, text: str) -> None:
        try:
            payload = json.loads(text)
        except (TypeError, ValueError):
            return
        if not isinstance(payload, dict):
            return
        errors = payload.get("errors")
        if isinstance(errors, list) and errors and isinstance(errors[0], dict):
            self.error_code = str(errors[0].get("error_code") or "") or None
            self.error_message = str(errors[0].get("message") or "") or None


def _make_adapter(
    config: UpstoxOAuthConfig, access_token: str, request: Callable
) -> UpstoxExecutionAdapter:
    """Build the existing Upstox adapter from the in-memory token.

    ``dry_run=True`` and ``max_retries=0`` make the write path/retries
    impossible; only the read-only ``quote()`` is used here.
    """
    credentials = UpstoxCredentials(
        access_token=access_token,
        api_key=config.client_id,
        base_url=config.base_url,
        timeout_seconds=config.timeout_seconds,
        max_retries=0,
    )
    return UpstoxExecutionAdapter(credentials=credentials, dry_run=True, request=request)


def run_oauth_quote_diagnostic(
    *,
    config: UpstoxOAuthConfig,
    instrument_key: str = INSTRUMENT_KEY,
    port: int = DEFAULT_CALLBACK_PORT,
    timeout: float = 300.0,
    open_browser: bool = True,
    state: str | None = None,
    request: Callable | None = None,
    adapter_factory: Callable[[UpstoxOAuthConfig, str, Callable], UpstoxExecutionAdapter] | None = None,
) -> dict[str, Any]:
    """Execute the OAuth -> one-quote diagnostic and return the evidence report.

    The access token stays in this call stack and the adapter only; it is never
    written, printed, or included in the returned report dict.
    """
    transport = request or http_request
    report: dict[str, Any] = {
        "oauth_started": True,
        "oauth_completed": False,
        "access_token_received": False,
        "token_persisted": False,
        "token_value_exposed": False,
        "instrument_key": instrument_key,
        "quote_endpoint": f"{QUOTE_ENDPOINT_PREFIX}{QUOTE_ENDPOINT_QUERY.format(key=quote(instrument_key))}",
        "http_status": None,
        "upstox_error_code": None,
        "upstox_error_message": None,
        "quote_available": False,
        "reference_price": None,
        "classification": "NOT_RUN",
        "confidence": "0.0",
        "orders_placed": False,
        "live_trading_enabled": False,
        "scheduler_enabled": False,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
    }

    csrf_state = state or new_oauth_state()
    auth_url = authorize_url(config, csrf_state)
    if open_browser:
        webbrowser.open(auth_url)

    server = LocalCallbackServer(expected_state=csrf_state, port=port)
    outcome = server.serve_once(max(0.0, float(timeout)))
    report["oauth_completed"] = outcome.completed
    if not outcome.completed:
        report["classification"] = "OAUTH_FAILED"
        return report

    try:
        token = exchange_code_for_token(config, outcome.code, request=transport)
    except (UpstoxExecutionError, MarketDataError) as exc:
        report["upstox_error_message"] = str(exc)[:240] or None
        report["classification"] = "OAUTH_FAILED"
        return report
    except Exception as exc:  # noqa: BLE001 - last-resort diagnostic net; never touches secrets
        report["upstox_error_message"] = f"unexpected diagnostic error: {str(exc)[:240]}"
        report["classification"] = "OAUTH_FAILED"
        return report
    report["access_token_received"] = bool(token.access_token)
    if not token.access_token:
        report["classification"] = "OAUTH_FAILED"
        return report

    observer = _QuoteObserver(transport)
    adapter = (
        adapter_factory(config, token.access_token, observer)
        if adapter_factory is not None
        else _make_adapter(config, token.access_token, observer)
    )
    try:
        price = adapter.quote(instrument_key)
    except (UpstoxExecutionError, MarketDataError) as exc:
        report["http_status"] = observer.status
        report["upstox_error_code"] = observer.error_code
        report["upstox_error_message"] = observer.error_message or str(exc)[:240]
        report["classification"] = "QUOTE_UNAVAILABLE"
        return report
    except Exception as exc:  # noqa: BLE001 - last-resort diagnostic net; never touches secrets
        report["http_status"] = observer.status
        report["upstox_error_code"] = observer.error_code
        report["upstox_error_message"] = f"unexpected diagnostic error: {str(exc)[:240]}"
        report["classification"] = "QUOTE_UNAVAILABLE"
        return report

    report["http_status"] = observer.status
    report["quote_available"] = True
    report["reference_price"] = str(price)
    report["classification"] = "QUOTE_AVAILABLE"
    report["confidence"] = "1.0"
    return report


def _file_stamp(timestamp_utc: str) -> str:
    return str(timestamp_utc).replace(":", "").replace("+", "_")


def write_report(report: dict[str, Any], report_dir: Path | str = DEFAULT_REPORT_DIR) -> Path:
    """Write the evidence report as timestamped JSON (git-ignored reports/ dir)."""
    directory = Path(report_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"upstox_oauth_quote_diagnostic_{_file_stamp(report['timestamp_utc'])}.json"
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _print_summary(report: dict[str, Any], report_path: Path) -> None:
    print("Upstox OAuth + quote diagnostic (read-only; no orders)")
    print(f"  oauth started      : {report['oauth_started']}")
    print(f"  oauth completed    : {report['oauth_completed']}")
    print("  access token       : in-memory only; never persisted or printed")
    print(f"  instrument         : {report['instrument_key']}")
    print(f"  quote endpoint     : {report['quote_endpoint']}")
    print(f"  http status        : {report['http_status']}")
    print(f"  upstox error code  : {report['upstox_error_code']}")
    print(f"  quote available    : {report['quote_available']}")
    print(f"  reference price    : {report['reference_price']}")
    print(f"  classification     : {report['classification']} (confidence {report['confidence']})")
    print(f"  orders placed      : {report['orders_placed']}")
    print(f"  live trading enabled: {report['live_trading_enabled']}")
    print(f"  scheduler enabled  : {report['scheduler_enabled']}")
    print(f"  report             : {report_path}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="upstox_oauth_quote_diagnostic",
        description="OAuth -> in-memory token -> exactly ONE read-only quote for NSE_FO|56983 -> evidence report.",
        epilog=(
            "The access token, authorization code and client secret are never printed or persisted; "
            "no order is placed and no live trading or scheduler is enabled."
        ),
    )
    parser.add_argument("--redirect-uri", default="",
                        help=f"registered redirect URI (default {DEFAULT_REDIRECT_URI})")
    parser.add_argument("--port", type=int, default=DEFAULT_CALLBACK_PORT,
                        help="local callback port (must match the registered redirect URI)")
    parser.add_argument("--base-url", default="",
                        help="Upstox endpoint base URL (env: UPSTOX_BASE_URL)")
    parser.add_argument("--timeout", type=float, default=None,
                        help="seconds to wait for the browser callback (env: UPSTOX_TIMEOUT_SECONDS)")
    parser.add_argument("--no-open", action="store_true",
                        help="do not auto-open the browser; print the authorization URL")
    parser.add_argument("--report-dir", default=str(DEFAULT_REPORT_DIR),
                        help="where to write the timestamped JSON evidence report (default: reports/execution)")
    parser.add_argument("--state", default="",
                        help="optional explicit CSRF state (default: random; only for reproducible tests)")
    args = parser.parse_args(argv)

    base = UpstoxOAuthConfig.from_env()
    if not base.configured:
        print("OAuth configuration error: set UPSTOX_API_KEY and UPSTOX_API_SECRET in .env", file=sys.stderr)
        print("The client id (UPSTOX_API_KEY) is public; the client secret (UPSTOX_API_SECRET) must never be echoed.", file=sys.stderr)
        return 2
    if args.port <= 0:
        print("error: --port must be positive", file=sys.stderr)
        return 2

    config = UpstoxOAuthConfig(
        client_id=base.client_id,
        client_secret=base.client_secret,
        redirect_uri=args.redirect_uri or base.redirect_uri,
        base_url=(args.base_url or base.base_url).rstrip("/"),
        timeout_seconds=args.timeout if args.timeout is not None else base.timeout_seconds,
    )

    csrf_state = args.state.strip() or new_oauth_state()
    if args.no_open:
        print(f"authorization URL (contains only the public client id):\n    {authorize_url(config, csrf_state)}")

    report = run_oauth_quote_diagnostic(
        config=config,
        port=args.port,
        timeout=args.timeout if args.timeout is not None else base.timeout_seconds,
        open_browser=not args.no_open,
        state=csrf_state,
    )
    report_path = write_report(report, args.report_dir)
    _print_summary(report, report_path)
    if not report["oauth_completed"]:
        print("OAuth did not complete; no access token was obtained and no quote was attempted.", file=sys.stderr)
        return 2
    if not report["quote_available"]:
        print("Quote was not available; see the evidence report for the Upstox response details.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())