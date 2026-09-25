"""Read-only OHLC URL-shape comparison diagnostic (no orders, no persistence).

Purpose
-------
Run one fresh browser OAuth, keep the access token in process memory only, and
make exactly TWO authenticated, read-only ``GET`` requests for the same
currently-trading instrument key ``NSE_FO|48704`` so the two OHLC URL shapes
Upstox accepts can be compared live:

    Request A (documented query-param form, now used by ``quote()``):
        GET /v2/market-quote/ohlc?instrument_key=NSE_FO%7C48704&interval=1d
    Request B (legacy path form formerly built by ``UpstoxExecutionAdapter.quote()``):
        GET /v2/market-quote/ohlc/NSE_FO%7C48704

The offline investigation found HIGH-confidence evidence that the legacy
adapter URL shape (/v2/market-quote/ohlc/{key}) did not match the documented
endpoint, so this diagnostic records both shapes side by side for the same
instrument.

Safety properties (enforced here, not merely documented):

* Exactly two market-quote ``GET`` requests are made — one per URL shape. No
  retries, no sibling instruments, no BOD refresh.
* The access token, authorization code and client secret are never printed,
  logged, or written anywhere. ``token_persisted`` and ``token_value_exposed``
  are always ``false``.
* No BUY/SELL order path exists here; ``orders_placed`` is always ``false``.
* No live-trading gate is required (quotes are read-only) and none is bypassed
  or weakened; ``live_trading_enabled`` is always ``false``. ``ExecutionMode``
  and gates are never touched.
* No scheduler is started; ``scheduler_enabled`` is always ``false``.
* No production code is changed; this script reuses the existing OAuth flow,
  credential object, and HTTP transport only.
"""
from __future__ import annotations

import argparse
import json
import sys
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, urlencode

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
    _quote_node,
)
from fno_ai_paper_trading.utils.http import (  # noqa: E402
    HttpError,
    http_request,
)

INSTRUMENT_KEY = "NSE_FO|48704"
INTERVAL = "1d"
QUOTE_PATH = "/v2/market-quote/ohlc"

#: Documented query-parameter form: GET /v2/market-quote/ohlc?instrument_key=...&interval=...
QUERY_OHLC_ENDPOINT = f"{QUOTE_PATH}?{urlencode({'instrument_key': INSTRUMENT_KEY, 'interval': INTERVAL})}"
#: Path form currently built by UpstoxExecutionAdapter.quote():
#: GET /v2/market-quote/ohlc/{instrument_key}
PATH_OHLC_ENDPOINT = f"{QUOTE_PATH}/{quote(INSTRUMENT_KEY)}"

DEFAULT_REPORT_DIR = REPO_ROOT / "reports" / "execution"


def request_a_endpoint() -> str:
    """Return the query-param endpoint (Request A) as a documentation snapshot."""
    return QUERY_OHLC_ENDPOINT


def request_b_endpoint() -> str:
    """Return the path-form endpoint (Request B) as a documentation snapshot."""
    return PATH_OHLC_ENDPOINT


def ohlc_url(base_url: str, endpoint: str) -> str:
    """Expand a diagnostic endpoint to the fully-qualified request URL.

    Mirrors ``UpstoxExecutionAdapter._url()`` (``base_url`` + ``path``) so the
    transport receives a scheme/host URL, never a bare path.
    """
    return f"{str(base_url or '').rstrip('/')}{endpoint}"


class _TransportObserver:
    """Record per-request safe metadata without ever touching the token.

    Observes the transport for one GET: the response status and, on a non-2xx
    response, the Upstox ``errors[0]`` ``error_code``/``message``. The access
    token lives in the request headers and never passes through here.
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
            self.error_code = str(
                errors[0].get("error_code") or errors[0].get("errorCode") or ""
            ) or None
            self.error_message = str(errors[0].get("message") or "") or None


def _request_ohlc(
    observer: _TransportObserver,
    headers: dict[str, str],
    base_url: str,
    endpoint: str,
    timeout: float,
) -> dict[str, Any]:
    """Perform one read-only OHLC GET and return its per-request evidence record.

    ``endpoint`` is the documented API path (e.g. ``/v2/market-quote/ohlc?...``);
    the transport receives the fully-qualified ``ohlc_url(base_url, endpoint)``
    URL — the same expansion ``UpstoxExecutionAdapter._url()`` performs — so the
    underlying curl/urllib transport never sees a bare path (curl exit 3).
    """
    url = ohlc_url(base_url, endpoint)
    record: dict[str, Any] = {
        "endpoint": endpoint,
        "http_status": None,
        "upstox_error_code": None,
        "upstox_error_message": None,
        "quote_available": False,
        "reference_price": None,
    }
    try:
        response = observer("GET", url, headers=headers, timeout=timeout)
    except HttpError as exc:
        record["http_status"] = observer.status
        record["upstox_error_code"] = observer.error_code
        record["upstox_error_message"] = observer.error_message or str(exc)[:240]
        return record
    except Exception as exc:  # noqa: BLE001 - last-resort diagnostic net; never touches secrets
        record["http_status"] = observer.status
        record["upstox_error_code"] = observer.error_code
        record["upstox_error_message"] = f"unexpected diagnostic error: {str(exc)[:240]}"
        return record

    record["http_status"] = observer.status
    try:
        payload = response.json
    except (TypeError, ValueError):
        record["upstox_error_message"] = "unparseable response body"
        return record
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        record["upstox_error_message"] = "response carried no data node"
        return record

    node = _quote_node(payload["data"], INSTRUMENT_KEY)
    if not isinstance(node, dict):
        return record
    ohlc = node.get("ohlc") or {}
    price = ohlc.get("close")
    if price is None:
        price = node.get("last_price")
    if price is None:
        return record
    record["quote_available"] = True
    record["reference_price"] = str(price)
    return record


def _make_credentials(config: UpstoxOAuthConfig, access_token: str) -> UpstoxCredentials:
    """Build the existing credentials object from the in-memory token.

    ``max_retries=0`` keeps the transport one-shot; only read-only GETs are
    issued through it here.
    """
    return UpstoxCredentials(
        access_token=access_token,
        api_key=config.client_id,
        base_url=config.base_url,
        timeout_seconds=config.timeout_seconds,
        max_retries=0,
    )


def _classify(requests: list[dict[str, Any]]) -> str:
    a_available = requests[0]["quote_available"]
    b_available = requests[1]["quote_available"]
    if a_available and b_available:
        return "QUERY_AND_PATH_AVAILABLE"
    if a_available:
        return "QUERY_FORM_AVAILABLE_ONLY"
    if b_available:
        return "PATH_FORM_AVAILABLE_ONLY"
    if requests[0]["http_status"] == requests[1]["http_status"] == 404:
        return "BOTH_404"
    return "QUOTE_UNAVAILABLE"


def run_ohlc_comparison(
    *,
    config: UpstoxOAuthConfig,
    instrument_key: str = INSTRUMENT_KEY,
    port: int = DEFAULT_CALLBACK_PORT,
    timeout: float = 300.0,
    open_browser: bool = True,
    state: str | None = None,
    request: Callable | None = None,
) -> dict[str, Any]:
    """Execute OAuth -> exactly two read-only OHLC GETs and return the evidence report.

    The access token stays in this call stack and inside the credentials object
    only; it is never written, printed, or included in the report.
    """
    transport = request or http_request
    report: dict[str, Any] = {
        "instrument_key": instrument_key,
        "oauth_started": True,
        "oauth_completed": False,
        "access_token_received": False,
        "token_persisted": False,
        "token_value_exposed": False,
        "orders_placed": False,
        "live_trading_enabled": False,
        "scheduler_enabled": False,
        "requests": [
            _request_ohlc_placeholder(QUERY_OHLC_ENDPOINT),
            _request_ohlc_placeholder(PATH_OHLC_ENDPOINT),
        ],
        "classification": "NOT_RUN",
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
        report["requests"] = []
        report["classification"] = "OAUTH_FAILED"
        return report

    try:
        token = exchange_code_for_token(config, outcome.code, request=transport)
    except (UpstoxExecutionError, MarketDataError) as exc:
        report["requests"] = []
        report["classification"] = "OAUTH_FAILED"
        report["oauth_error"] = str(exc)[:240]
        return report
    except Exception as exc:  # noqa: BLE001 - last-resort diagnostic net; never touches secrets
        report["requests"] = []
        report["classification"] = "OAUTH_FAILED"
        report["oauth_error"] = f"unexpected diagnostic error: {str(exc)[:240]}"
        return report
    report["access_token_received"] = bool(token.access_token)
    if not token.access_token:
        report["requests"] = []
        report["classification"] = "OAUTH_FAILED"
        return report

    credentials = _make_credentials(config, token.access_token)
    headers = credentials.headers()
    base_url = credentials.base_url

    query_record = _request_ohlc(
        _TransportObserver(transport), headers, base_url, QUERY_OHLC_ENDPOINT, config.timeout_seconds
    )
    path_record = _request_ohlc(
        _TransportObserver(transport), headers, base_url, PATH_OHLC_ENDPOINT, config.timeout_seconds
    )
    report["requests"] = [query_record, path_record]
    report["classification"] = _classify(report["requests"])
    return report


def _request_ohlc_placeholder(endpoint: str) -> dict[str, Any]:
    return {
        "endpoint": endpoint,
        "http_status": None,
        "upstox_error_code": None,
        "upstox_error_message": None,
        "quote_available": False,
        "reference_price": None,
    }


def _file_stamp(timestamp_utc: str) -> str:
    return str(timestamp_utc).replace(":", "").replace("+", "_")


def write_report(report: dict[str, Any], report_dir: Path | str = DEFAULT_REPORT_DIR) -> Path:
    """Write the evidence report as timestamped JSON (git-ignored reports/ dir)."""
    directory = Path(report_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"upstox_oauth_ohlc_comparison_{_file_stamp(report['timestamp_utc'])}.json"
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _print_summary(report: dict[str, Any], report_path: Path) -> None:
    print("Upstox OHLC URL-shape comparison diagnostic (read-only; no orders)")
    print(f"  instrument           : {report['instrument_key']}")
    print(f"  oauth completed      : {report['oauth_completed']}")
    print("  access token         : in-memory only; never persisted or printed")
    for index, record in enumerate(report["requests"], start=1):
        print(f"  request {index}            : {record['endpoint']}")
        print(f"    http status        : {record['http_status']}")
        print(f"    upstox error code  : {record['upstox_error_code']}")
        print(f"    quote available    : {record['quote_available']}")
        print(f"    reference price    : {record['reference_price']}")
    print(f"  classification       : {report['classification']}")
    print(f"  orders placed        : {report['orders_placed']}")
    print(f"  live trading enabled : {report['live_trading_enabled']}")
    print(f"  scheduler enabled    : {report['scheduler_enabled']}")
    print(f"  report               : {report_path}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="upstox_oauth_ohlc_comparison",
        description="OAuth -> in-memory token -> exactly TWO read-only OHLC GETs (query-form vs path-form) for NSE_FO|48704 -> evidence report.",
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

    report = run_ohlc_comparison(
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
    if not any(record["quote_available"] for record in report["requests"]):
        print("Neither URL shape returned a quote; see the evidence report.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())