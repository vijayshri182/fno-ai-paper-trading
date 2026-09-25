"""Fetch -> validate -> report the live Upstox option chain (READ-ONLY).

Safe diagnostic runner for Phase 6: fetches one NIFTY expiry's option chain via
the read-only ``GET /v2/option/chain`` + ``GET /v2/option/contract`` endpoints,
normalizes it through the Phase 5 stack, classifies it with ``ChainValidator``,
and writes a credential-free evidence artifact to
``reports/forensics/upstox_chain_verification_<EXPIRY>.{json,md}``.

SAFETY: only HTTP GET against option market-data endpoints. No order, no
modification, no cancellation, no live trade API is ever called. Uses the
data-layer token ``FNO_UPSTOX_ACCESS_TOKEN`` only (never the execution token)
and never prints token/header values.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from fno_ai_paper_trading.data.instrument_registry import get_research_instrument  # noqa: E402
from fno_ai_paper_trading.research.options.normalization import chain_fingerprint  # noqa: E402
from fno_ai_paper_trading.research.options.upstox_adapter import (  # noqa: E402
    UPSTOX_PROVIDER_ID,
    UpstoxOptionChainProvider,
)
from fno_ai_paper_trading.research.options.upstox_diagnostic import (  # noqa: E402
    build_chain_diagnostic,
    render_markdown,
)
from fno_ai_paper_trading.research.options.validation import ChainValidator  # noqa: E402


def main() -> int:
    load_dotenv(REPO_ROOT / ".env")
    token = os.getenv("FNO_UPSTOX_ACCESS_TOKEN", "")
    if not token:
        print("SKIP: FNO_UPSTOX_ACCESS_TOKEN is not set (opt-in, read-only runner).")
        return 2

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--underlying", default="NIFTY 50")
    parser.add_argument("--expiry", default=None, help="YYYY-MM-DD; defaults to the nearest upcoming expiry")
    parser.add_argument("--max-age-seconds", type=int, default=300)
    parser.add_argument("--no-save", action="store_true")
    args = parser.parse_args()

    provider = UpstoxOptionChainProvider(token, now_fn=datetime.now)
    underlying = get_research_instrument(args.underlying)

    expiry = datetime.strptime(args.expiry, "%Y-%m-%d").date() if args.expiry else None

    fetched_at = datetime.now()
    chain = provider.fetch_chain(underlying, expiry=expiry)
    validation = ChainValidator(max_age_seconds=args.max_age_seconds).validate(chain, ref_time=chain.timestamp)

    endpoints = ["/v2/option/chain", "/v2/option/contract"]
    diagnostic = build_chain_diagnostic(
        chain,
        validation,
        fetched_at=fetched_at,
        live=True,
        endpoints_used=endpoints,
        orders_called=False,
        historical_options_data="NOT_AVAILABLE",
        notes=[
            "Upstox sends no per-quote timestamp; the adapter stamps the receive instant "
            "(never fabricated as a provider field).",
            "oi_change is derived as oi - prev_oi (both provider-supplied).",
            "greeks rho is not provided by Upstox and stays UNAVAILABLE (never zeroed).",
        ],
    )
    diagnostic["fingerprint"] = chain_fingerprint(chain)
    diagnostic["auth"] = {
        "mechanism": "OAuth2 Bearer (data-layer token, FNO_UPSTOX_ACCESS_TOKEN)",
        "x_api_key_required": False,
        "token_value_present_in_output": False,
    }

    if not args.no_save:
        out_dir = REPO_ROOT / "reports" / "forensics"
        out_dir.mkdir(parents=True, exist_ok=True)
        stem = f"upstox_chain_verification_{chain.expiry.isoformat()}"
        (out_dir / f"{stem}.json").write_text(
            json.dumps(diagnostic, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        (out_dir / f"{stem}.md").write_text(render_markdown(diagnostic), encoding="utf-8")
        print(f"wrote reports/forensics/{stem}.{{json,md}}")

    print(render_markdown(diagnostic))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())