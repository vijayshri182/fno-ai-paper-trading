"""WS 7.24B Stage A: live-buy preparation that **STOPS before BUY**.

Prepares exactly one tradable NIFTY current-week ATM CE contract (DYNAMIC
expiry/strike/CE-PE/lot from the authoritative Upstox master — nothing
hard-coded) for a single one-lot MARKET BUY, then stops. The prepared payload
is printed as a redacted preview only; **no order is placed here**.

Safety rules honoured (same spirit as ``upstox_oauth.py`` / WS 7.9):

* OAuth ends on a loopback-only callback server (127.0.0.1); the access token
  stays in-process — never printed, never persisted, never committed.
* Consent refresh preserves the existing ``reports/execution/operator_consent.json``
  schema exactly (operator/purpose kept, created/expires/fingerprint refreshed).
* The authoritative :class:`LiveExecutionTestGate` must be OPEN
  (``FNO_LIVE_EXECUTION_TEST_ENABLED=1`` + consent file + matching in-process
  token) in mode ``LIVE_EXECUTION_TEST``; this script never overrides it.
* Read-only account id, index quote, FLAT position check, client-side margin
  sanity; then the one-lot MARKET BUY payload is built and **stopped**.
* ``place_order``/``cancel_order`` are never invoked by this script.
* Dry-run (``--dry-run``) is the default; a real send is only possible via the
  separate, explicit, operator-authorized Stage B path.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from fno_ai_paper_trading.execution.oauth import (  # noqa: E402
    DEFAULT_CALLBACK_PORT,
    UpstoxOAuthConfig,
)
from fno_ai_paper_trading.execution.live_prep import (  # noqa: E402
    FAIL_CLOSED,
    READY,
    PrepIO,
    prepare_live_buy,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="upstox_prepare_live_buy",
        description=(
            "WS 7.24B Stage A: prepare the exact one-lot MARKET BUY for a NIFTY "
            "current-week ATM CE and STOP before placing anything."
        ),
        epilog=(
            "Safety: no order is sent by this script; the token is kept in-process "
            "only and never printed; consent is refreshed preserving the existing schema."
        ),
    )
    parser.add_argument("--operator", default="", help="operator authorizing this run (overrides consent file)")
    parser.add_argument("--purpose", default="", help="purpose recorded in the consent file")
    parser.add_argument("--consent-file", default="",
                        help="consent file to refresh (default: reports/execution/operator_consent.json)")
    parser.add_argument("--master-file", default="",
                        help="offline Upstox BOD instrument master (NSE.json/.gz) for deterministic/offline resolution")
    parser.add_argument("--port", type=int, default=DEFAULT_CALLBACK_PORT,
                        help="loopback callback port (must match the registered redirect URI)")
    parser.add_argument("--timeout", type=float, default=300.0,
                        help="seconds to wait for the browser callback before failing")
    parser.add_argument("--no-open", action="store_true", help="do not auto-open the browser")
    parser.add_argument("--dry-run", dest="dry_run", action="store_true", default=True,
                        help="never contact a broker write endpoint (DEFAULT; kept for explicitness)")
    parser.add_argument("--no-dry-run", dest="dry_run", action="store_false",
                        help="allow adapter calls (STILL no order is placed by this script)")
    args = parser.parse_args(argv)

    try:
        config = UpstoxOAuthConfig.from_env()
    except Exception as exc:  # noqa: BLE001
        print(f"OAuth configuration error: {exc}", file=sys.stderr)
        return 2

    if not config.configured:
        print("OAuth configuration error: missing UPSTOX_API_KEY / UPSTOX_API_SECRET (.env).", file=sys.stderr)
        return 2

    io = PrepIO(
        oauth_config=config,
        operator=args.operator,
        purpose=args.purpose,
        consent_path=args.consent_file or None,
        master_file=args.master_file or None,
        loopback_port=args.port,
        callback_timeout=args.timeout,
        open_browser=not args.no_open,
        dry_run=args.dry_run,
    )

    result = prepare_live_buy(io)

    print()
    print(result.to_console())
    print()

    if result.status == READY:
        print("READY FOR EXPLICIT BUY APPROVAL: YES")
        print("DO NOT PLACE THE BUY automatically.")
        print("No order was sent. To actually buy, use the separate, explicit,")
        print("operator-authorized Stage B path (run_live_execution_test.py) with")
        print("FNO_LIVE_EXECUTION_TEST_ENABLED=1 and --confirm-live-enablement.")
        return 0

    if result.status == FAIL_CLOSED:
        print("READY FOR EXPLICIT BUY APPROVAL: NO")
        print("No order payload was constructed; nothing was sent to any broker.")
        return 1

    print("READY FOR EXPLICIT BUY APPROVAL: NO")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())