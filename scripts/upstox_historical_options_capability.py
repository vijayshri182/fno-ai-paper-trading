"""Read-only historical options-data capability probe runner (DATA-003).

Executes the Phase 12 capability probes against the Upstox public data API and
writes a credential-free evidence artifact to
``reports/forensics/historical_options_capability_<YYYY-MM-DD>.{json,md}``
(git-ignored). The verdict itself is produced deterministically by
``fno_ai_paper_trading.research.options.capability_probe.diagnose``.

Probed surfaces (all HTTP GET, market data only):
* ``/v2/option/contract``            expiry resolution for the NIFTY underlying
* ``/v2/option/chain``               one live expiry's full strike x CE/PE matrix
* ``/v2/expired-instruments/{expiries|option/contract|historical-candle}``
* ``/v3/historical-candle`` 5m       a pre-listing window (zero-row expectation)

SAFETY: read-only option market-data GETs only. No order, no modification, no
cancellation, no live trade API is ever called. Uses the data-layer token
``FNO_UPSTOX_ACCESS_TOKEN`` only and never prints/token-values in headers or
output. Credentials never appear in the artifact (provider text is redacted).
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from fno_ai_paper_trading.research.options.capability_probe import (  # noqa: E402
    EndpointProbe,
    diagnose,
    probe_expired_instruments,
    probe_live_chain,
    probe_live_contract,
    probe_v3_option_candle,
    render_markdown,
)

# A historic window strictly before any currently-listed option's listing date
# proves the dated v3 endpoint cannot reconstruct a full-chain as-of surface
# (it returns zero rows for periods before the contract existed).
PRE_LISTING_FROM = "2024-01-02"
PRE_LISTING_TO = "2024-01-05"


def _skipped_probe(source_id: str, endpoint: str, reason: str) -> EndpointProbe:
    return EndpointProbe(
        source_id=source_id,
        endpoint=endpoint,
        role="SKIPPED",
        reachable=False,
        notes=(reason,),
    )


def main() -> int:
    load_dotenv(REPO_ROOT / ".env")
    token = os.getenv("FNO_UPSTOX_ACCESS_TOKEN", "")
    if not token:
        print("SKIP: FNO_UPSTOX_ACCESS_TOKEN is not set (opt-in, read-only runner).")
        return 2

    fetched_at = datetime.now()

    contract = probe_live_contract(token=token)
    expiry = contract.expiries[0] if contract.expiries else None
    chain = (
        probe_live_chain(token=token, expiry_date=expiry)
        if expiry
        else _skipped_probe(
            "upstox-live-chain",
            "GET /v2/option/chain",
            "no expiry observed from /v2/option/contract; chain probe skipped",
        )
    )
    expired_expiries, expired_contract, expired_history = probe_expired_instruments(token=token)
    option_key = chain.first_option_key if isinstance(chain.first_option_key, str) and chain.first_option_key else None
    v3 = (
        probe_v3_option_candle(
            token=token,
            option_key=option_key,
            from_date=PRE_LISTING_FROM,
            to_date=PRE_LISTING_TO,
        )
        if option_key
        else _skipped_probe(
            "upstox-v3-option-candle",
            "GET /v3/historical-candle/{...}/minutes/5/{to}/{from}",
            "no option instrument_key observed from /v2/option/chain; v3 probe skipped",
        )
    )

    probes = [
        contract,
        chain,
        expired_expiries,
        expired_contract,
        expired_history,
        v3,
    ]
    evidence_notes = [
        "read-only HTTP GET probes only; no dataset materialised and no strategy work performed",
        f"live chain expiry probed: {expiry or 'none observed'}",
        f"v3 candle window probed: {PRE_LISTING_FROM}..{PRE_LISTING_TO} (pre-listing for every currently-listed contract)",
        "provider entitlement failures (e.g. HTTP 401 UDAPI1149) are preserved verbatim in the probes",
    ]
    result = diagnose(probes, evidence_notes=evidence_notes)

    artifact = dict(result)
    artifact["fetched_at"] = fetched_at.isoformat(timespec="seconds")

    out_dir = REPO_ROOT / "reports" / "forensics"
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"historical_options_capability_{fetched_at.date().isoformat()}"
    (out_dir / f"{stem}.json").write_text(
        json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    markdown = render_markdown(result) + f"- Generated at: {artifact['fetched_at']}\n"
    (out_dir / f"{stem}.md").write_text(markdown, encoding="utf-8")
    print(f"wrote reports/forensics/{stem}.{{json,md}}")

    print(markdown)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())