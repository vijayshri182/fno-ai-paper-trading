"""Read-only historical NIFTY options-data capability probe (DATA-003).

Deterministic, credential-free capability evidence over the Upstox public
data APIs that decides -- with the CURRENT provider access -- whether a
verified historical options-chain dataset can be materialised under the
frozen ``options_data_contract``.

Probed surfaces:

* ``/v2/option/contract`` + ``/v2/option/chain``    live option-chain snapshot
* ``/v2/expired-instruments/{expiries|option/contract|historical-candle}``
                                                     expired F&O history
* ``/v3/historical-candle`` 5m per-option            per-contract candle depth

Guarantees:

* read-only HTTP GET only; the transport (``http_fn``) is injectable so tests
  never touch the network and every result is deterministic;
* the access token never leaves the probe instance and any echoed provider
  text is token-redacted (:func:`redact`);
* provider entitlement failures (e.g. HTTP 401 UDAPI1149) are preserved
  verbatim (exact code + message) and never bypassed;
* no strategy, no orders, no pricing, no simulation and no protected-OOS
  interaction: evidence is classified through the Phase 12 capability gate
  (:class:`fno_ai_paper_trading.options_research.capability`) and the
  protected window is not referenced anywhere in this module.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from fno_ai_paper_trading.options_research.capability import (
    SourceFinding,
    assess_readiness,
    classify_source,
)
from fno_ai_paper_trading.utils.http import HttpError, http_get

UPSTOX_BASE_URL = "https://api.upstox.com"
NIFTY_UNDERLYING_KEY = "NSE_INDEX|Nifty 50"
PROBE_TIMEOUT_SECONDS = 10.0

LIVE_CONTRACT_PATH = "/v2/option/contract"
LIVE_CHAIN_PATH = "/v2/option/chain"
EXPIRED_EXPIRIES_PATH = "/v2/expired-instruments/expiries"
EXPIRED_OPTION_CONTRACT_PATH = "/v2/expired-instruments/option/contract"
EXPIRED_HISTORICAL_PATH = (
    "/v2/expired-instruments/historical-candle/{expired_instrument_key}/{interval}/{to_date}/{from_date}"
)
HISTORICAL_CANDLE_PATH = "/v3/historical-candle"

SOURCE_UPSTOX_LIVE_CHAIN = "upstox-live-chain"
SOURCE_UPSTOX_EXPIRED = "upstox-expired-contracts"
SOURCE_UPSTOX_V3_OPTION_CANDLE = "upstox-v3-option-candle"

REDACTED = "<REDACTED>"

# Documented input examples used to shape the (blocked) expired-instruments
# probes so they advance past request-parameter validation to the entitlement
# gate instead of failing on a missing field.
EXPIRED_OPTION_EXPIRY_DATE = "2025-09-25"
EXPIRED_SAMPLE_INSTRUMENT_KEY = "NSE_FO|73507|24-04-2025"
EXPIRED_CANDLE_INTERVAL = "5minute"
EXPIRED_CANDLE_FROM = "2025-04-24"
EXPIRED_CANDLE_TO = "2025-04-24"

# Role names annotate which semantic endpoint class a probe covers.
_ROLE_LIVE_CONTRACT = "LIVE_CONTRACT"
_ROLE_LIVE_CHAIN = "LIVE_CHAIN"
_ROLE_EXPIRED_EXPIRIES = "EXPIRED_EXPIRIES"
_ROLE_EXPIRED_OPTION_CONTRACT = "EXPIRED_OPTION_CONTRACT"
_ROLE_EXPIRED_HISTORICAL = "EXPIRED_HISTORICAL"
_ROLE_V3_OPTION_CANDLE = "V3_OPTION_CANDLE"


@dataclass(frozen=True)
class EndpointProbe:
    """One read-only probe result (never contains credentials).

    ``reachable`` means the transport answered with an HTTP status (including
    a non-2xx entitlement response). ``provider_code``/``provider_message``
    carry the exact provider signalled entitlement text, token-redacted.
    """

    source_id: str
    endpoint: str
    role: str = ""
    reachable: bool = False
    status: int | None = None
    provider_code: str | None = None
    provider_message: str | None = None
    observed_fields: tuple[str, ...] = ()
    expiries: tuple[str, ...] = ()
    first_option_key: str | None = None
    rows: int = 0
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "source_id": self.source_id,
            "endpoint": self.endpoint,
            "role": self.role,
            "reachable": self.reachable,
            "status": self.status,
            "provider_code": self.provider_code,
            "provider_message": self.provider_message,
            "observed_fields": list(self.observed_fields),
            "expiries": list(self.expiries),
            "first_option_key": self.first_option_key,
            "rows": self.rows,
            "notes": list(self.notes),
        }


def redact(text: str, token: str) -> str:
    """Replace any occurrence of ``token`` (the bearer value) with ``<REDACTED>``."""
    if not token:
        return text
    return text.replace(token, REDACTED)


def extract_upstox_error(text: str) -> tuple[str | None, str | None]:
    """Return ``(code, message)`` from an Upstox / Cloudflare error body.

    Accepts Upstox's ``error_code``/``error_message`` shape, its ``errors[0]
    {code,message}`` shape, and Cloudflare ``title`` fallbacks. Returns
    ``(None, None)`` when the body carries no structured error.
    """
    if not text:
        return None, None
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None, None
    if not isinstance(payload, Mapping):
        return None, None
    code = str(payload.get("error_code") or "")
    message = str(payload.get("error_message") or payload.get("detail") or payload.get("title") or "")
    errors = payload.get("errors")
    if isinstance(errors, list) and errors and isinstance(errors[0], Mapping):
        first = errors[0]
        code = code or str(first.get("code") or "")
        message = message or str(first.get("message") or "")
    return (code or None), (message or None)


def observed_chain_fields(rows: Sequence[Any]) -> tuple[str, ...]:
    """Field names actually present under the first call/put market node.

    Only keys with non-``None`` values on the first observed CE/PE record are
    reported -- absent fields stay absent, nothing is estimated or injected.
    """
    observed: set[str] = set()
    for record in rows:
        if not isinstance(record, Mapping):
            continue
        if record.get("underlying_spot_price") is not None:
            observed.add("underlying_spot_price")
        for side in ("call_options", "put_options"):
            node = record.get(side)
            if not isinstance(node, Mapping):
                continue
            market = node.get("market_data")
            if isinstance(market, Mapping):
                for field in ("ltp", "bid_price", "ask_price", "volume", "oi", "prev_oi"):
                    if market.get(field) is not None:
                        observed.add(field)
            greeks = node.get("option_greeks")
            if isinstance(greeks, Mapping):
                for field in ("iv", "delta", "gamma", "theta", "vega"):
                    if greeks.get(field) is not None:
                        observed.add(field)
            for field in ("instrument_key", "expiry", "strike_price", "option_type"):
                if node.get(field) is not None:
                    observed.add(field)
    return tuple(sorted(observed))


def _extract_expiries(rows: Sequence[Any]) -> tuple[str, ...]:
    seen: list[str] = []
    for record in rows:
        if isinstance(record, Mapping) and record.get("expiry"):
            value = str(record["expiry"])
            if value not in seen:
                seen.append(value)
    return tuple(sorted(seen))


def _extract_first_option_key(rows: Sequence[Any]) -> str | None:
    for record in rows:
        if not isinstance(record, Mapping):
            continue
        for side in ("call_options", "put_options"):
            node = record.get(side)
            if isinstance(node, Mapping) and node.get("instrument_key"):
                return str(node["instrument_key"])
    return None


def _probe_get(
    source_id: str,
    endpoint: str,
    role: str,
    *,
    token: str,
    path: str,
    params: Mapping[str, Any] | None = None,
    base_url: str = UPSTOX_BASE_URL,
    http_fn: Callable[..., Any] = http_get,
    timeout: float = PROBE_TIMEOUT_SECONDS,
) -> EndpointProbe:
    url = f"{base_url}{path}"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    try:
        response = http_fn(url, params=params if params else None, headers=headers, timeout=timeout)
    except HttpError as exc:
        code, message = extract_upstox_error(exc.text)
        return EndpointProbe(
            source_id=source_id,
            endpoint=endpoint,
            role=role,
            reachable=True,
            status=exc.status,
            provider_code=redact(code, token) if code else None,
            provider_message=redact(message, token) if message else None,
            notes=("non-2xx provider response; signalled entitlement/access preserved verbatim",),
        )
    except OSError as exc:
        return EndpointProbe(
            source_id=source_id,
            endpoint=endpoint,
            role=role,
            reachable=False,
            notes=(f"transport failure: {exc}",),
        )

    try:
        payload = response.json
    except (ValueError, json.JSONDecodeError):
        return EndpointProbe(
            source_id=source_id,
            endpoint=endpoint,
            role=role,
            reachable=True,
            status=response.status,
            notes=("non-JSON response body",),
        )

    if not isinstance(payload, Mapping):
        return EndpointProbe(
            source_id=source_id,
            endpoint=endpoint,
            role=role,
            reachable=True,
            status=response.status,
            notes=("unexpected top-level payload shape (not a JSON object)",),
        )

    if role == _ROLE_V3_OPTION_CANDLE:
        data = payload.get("data")
        candles = data.get("candles") if isinstance(data, Mapping) else None
        rows = len(candles) if isinstance(candles, list) else 0
        note = (
            "0 candles returned (no pre-listing history)" if rows == 0
            else f"{rows} candles returned"
        )
        return EndpointProbe(
            source_id=source_id,
            endpoint=endpoint,
            role=role,
            reachable=True,
            status=response.status,
            rows=rows,
            notes=(note,),
        )

    data = payload.get("data")
    if not isinstance(data, list):
        return EndpointProbe(
            source_id=source_id,
            endpoint=endpoint,
            role=role,
            reachable=True,
            status=response.status,
            rows=0,
            notes=("provider returned no list under 'data'",),
        )

    fields = observed_chain_fields(data) if role in (_ROLE_LIVE_CONTRACT, _ROLE_LIVE_CHAIN) else ()
    expiries = _extract_expiries(data) if role == _ROLE_LIVE_CONTRACT else ()
    option_key = _extract_first_option_key(data) if role == _ROLE_LIVE_CHAIN else None
    return EndpointProbe(
        source_id=source_id,
        endpoint=endpoint,
        role=role,
        reachable=True,
        status=response.status,
        observed_fields=fields,
        expiries=expiries,
        first_option_key=option_key,
        rows=len(data),
    )


def probe_live_contract(
    *,
    token: str,
    base_url: str = UPSTOX_BASE_URL,
    http_fn: Callable[..., Any] = http_get,
    timeout: float = PROBE_TIMEOUT_SECONDS,
) -> EndpointProbe:
    """Probe the live option-contract universe (expiry resolution)."""
    return _probe_get(
        SOURCE_UPSTOX_LIVE_CHAIN,
        f"GET {LIVE_CONTRACT_PATH}",
        _ROLE_LIVE_CONTRACT,
        token=token,
        path=LIVE_CONTRACT_PATH,
        params={"instrument_key": NIFTY_UNDERLYING_KEY},
        base_url=base_url,
        http_fn=http_fn,
        timeout=timeout,
    )


def probe_live_chain(
    *,
    token: str,
    expiry_date: str,
    base_url: str = UPSTOX_BASE_URL,
    http_fn: Callable[..., Any] = http_get,
    timeout: float = PROBE_TIMEOUT_SECONDS,
) -> EndpointProbe:
    """Probe one expiry's live option chain (full strike x CE/PE matrix)."""
    return _probe_get(
        SOURCE_UPSTOX_LIVE_CHAIN,
        f"GET {LIVE_CHAIN_PATH}",
        _ROLE_LIVE_CHAIN,
        token=token,
        path=LIVE_CHAIN_PATH,
        params={"instrument_key": NIFTY_UNDERLYING_KEY, "expiry_date": expiry_date},
        base_url=base_url,
        http_fn=http_fn,
        timeout=timeout,
    )


def probe_expired_instruments(
    *,
    token: str,
    base_url: str = UPSTOX_BASE_URL,
    http_fn: Callable[..., Any] = http_get,
    timeout: float = PROBE_TIMEOUT_SECONDS,
) -> tuple[EndpointProbe, EndpointProbe, EndpointProbe]:
    """Probe the documented expired-instruments surface (read-only).

    Probes queried exactly as documented, so the request passes parameter
    binding and lands on the entitlement gate of the current token:

    * ``GET /v2/expired-instruments/expiries``                 ``instrument_key``
    * ``GET /v2/expired-instruments/option/contract``          ``instrument_key`` + ``expiry_date``
    * ``GET /v2/expired-instruments/historical-candle/{key}/{interval}/{to}/{from}``

    Returns ``(expiries, option/contract, historical-candle)`` probes. An
    ``HTTP 401 UDAPI1149`` entitlement failure is captured verbatim in the
    probe and never bypassed.
    """
    from urllib.parse import quote

    historical_path = (
        f"{EXPIRED_HISTORICAL_PATH.split('{')[0]}"
        f"{quote(EXPIRED_SAMPLE_INSTRUMENT_KEY)}/"
        f"{EXPIRED_CANDLE_INTERVAL}/{EXPIRED_CANDLE_TO}/{EXPIRED_CANDLE_FROM}"
    )
    return (
        _probe_get(
            SOURCE_UPSTOX_EXPIRED,
            f"GET {EXPIRED_EXPIRIES_PATH}",
            _ROLE_EXPIRED_EXPIRIES,
            token=token,
            path=EXPIRED_EXPIRIES_PATH,
            params={"instrument_key": NIFTY_UNDERLYING_KEY},
            base_url=base_url,
            http_fn=http_fn,
            timeout=timeout,
        ),
        _probe_get(
            SOURCE_UPSTOX_EXPIRED,
            f"GET {EXPIRED_OPTION_CONTRACT_PATH}",
            _ROLE_EXPIRED_OPTION_CONTRACT,
            token=token,
            path=EXPIRED_OPTION_CONTRACT_PATH,
            params={
                "instrument_key": NIFTY_UNDERLYING_KEY,
                "expiry_date": EXPIRED_OPTION_EXPIRY_DATE,
            },
            base_url=base_url,
            http_fn=http_fn,
            timeout=timeout,
        ),
        _probe_get(
            SOURCE_UPSTOX_EXPIRED,
            f"GET {EXPIRED_HISTORICAL_PATH}",
            _ROLE_EXPIRED_HISTORICAL,
            token=token,
            path=historical_path,
            base_url=base_url,
            http_fn=http_fn,
            timeout=timeout,
        ),
    )


def probe_v3_option_candle(
    *,
    token: str,
    option_key: str,
    from_date: str,
    to_date: str,
    base_url: str = UPSTOX_BASE_URL,
    http_fn: Callable[..., Any] = http_get,
    timeout: float = PROBE_TIMEOUT_SECONDS,
) -> EndpointProbe:
    """Probe 5m OHLCV+OI candles for one currently-listed option contract.

    The dated endpoint serves no bars before the contract listing date, so a
    window inside the protected research period returns zero rows -- the
    documented depth limitation, not a full-chain as-of history.
    """
    from urllib.parse import quote

    path = (
        f"{HISTORICAL_CANDLE_PATH}/{quote(option_key)}/minutes/5/{to_date}/{from_date}"
    )
    return _probe_get(
        SOURCE_UPSTOX_V3_OPTION_CANDLE,
        f"GET {HISTORICAL_CANDLE_PATH}/{{...}}/minutes/5/{{to}}/{{from}}",
        _ROLE_V3_OPTION_CANDLE,
        token=token,
        path=path,
        base_url=base_url,
        http_fn=http_fn,
        timeout=timeout,
    )


def source_finding_for(probe: EndpointProbe) -> SourceFinding:
    """Map one probe to the provider-neutral Phase 12 source classification."""
    if probe.role.startswith("EXPIRED"):
        blocked = (probe.status == 401) or not (probe.reachable and probe.status == 200)
        if blocked:
            note = (
                f"provider signalled {probe.status} {probe.provider_code or ''} "
                f"({probe.provider_message or 'no message'})".strip()
            )
        else:
            note = "expired F&O endpoint reachable; no rows observed"
        return SourceFinding(
            source_id=probe.source_id,
            provider="UPSTOX",
            product="Expired NIFTY F&O history (5m OHLCV+OI per contract)",
            date_coverage="expired contracts inside the research window (if entitled)",
            granularity="5m",
            historical_retrieval=True,
            observed_bid_ask=False,
            observed_ltp=False,
            observed_volume=False,
            observed_oi=False,
            observed_iv=False,
            observed_greeks=False,
            timezone="IST",
            licensing_access=(
                "Upstox Plus plan required (UDAPI1149) when entitlement is absent"
                if probe.status == 401
                else "FNO_UPSTOX_ACCESS_TOKEN (data token)"
            ),
            reproducible=not blocked,
            checksum_provenance=False,
            supports_realistic_simulation=False,
            synthetic=False,
            notes=(note,),
        )
    if probe.role in (_ROLE_LIVE_CONTRACT, _ROLE_LIVE_CHAIN):
        observed = set(probe.observed_fields)
        return SourceFinding(
            source_id=probe.source_id,
            provider="UPSTOX_V2_OPTION_CHAIN",
            product="Live NIFTY option-chain snapshot",
            date_coverage="current session only",
            granularity="snapshot",
            historical_retrieval=False,
            observed_bid_ask=("bid_price" in observed and "ask_price" in observed),
            observed_ltp="ltp" in observed,
            observed_volume="volume" in observed,
            observed_oi="oi" in observed,
            observed_iv="iv" in observed,
            observed_greeks="delta" in observed,
            timezone="IST (receive-stamped snapshots)",
            licensing_access="OAuth2 data token (FNO_UPSTOX_ACCESS_TOKEN)",
            reproducible=False,
            checksum_provenance=False,
            supports_realistic_simulation=False,
            synthetic=False,
            notes=(
                "live-snapshot only; the chain endpoints expose no as-of parameter "
                "so they can never serve historical chains",
            ),
        )
    return SourceFinding(
        source_id=probe.source_id,
        provider="UPSTOX_V3_HISTORICAL_CANDLE",
        product="Per-contract 5m OHLCV+OI option candles (currently-listed contracts)",
        date_coverage="contrived listing-date window only",
        granularity="5m",
        historical_retrieval=False,
        observed_bid_ask=False,
        observed_ltp=False,
        observed_volume=True,
        observed_oi=True,
        observed_iv=False,
        observed_greeks=False,
        timezone="IST",
        licensing_access="OAuth2 data token (FNO_UPSTOX_ACCESS_TOKEN)",
        reproducible=True,
        checksum_provenance=False,
        supports_realistic_simulation=False,
        synthetic=False,
        notes=(
            "returns bars only from the contract listing date (probe returned 0 "
            "pre-listing rows); it does not reconstruct an as-of full-chain surface",
        ),
    )


def diagnose(
    probes: Sequence[EndpointProbe],
    *,
    evidence_notes: Sequence[str] = (),
) -> dict[str, object]:
    """Combine probes into the provider-neutral Phase 12 gate verdict.

    Credential-free and deterministic: the returned mapping carries no token
    values, no timestamps and no wall-clock dependence; ordering is sorted so
    two runs over the same evidence are byte-identical.
    """
    findings = tuple(source_finding_for(probe) for probe in probes)
    assessment = assess_readiness(sources=findings, evidence_notes=tuple(evidence_notes))
    ordered = sorted(probes, key=lambda p: (p.role, p.endpoint))
    return {
        "workstream": "historical_options_data_capability",
        "verdict": assessment.verdict,
        "reason": assessment.reason,
        "sources": {finding.source_id: assessment.source_verdicts.get(finding.source_id) for finding in findings},
        "source_classifications": {f.source_id: classify_source(f) for f in findings},
        "probes": [probe.to_dict() for probe in ordered],
        "evidence_notes": list(assessment.evidence_notes),
        "credentials_in_output": False,
        "protected_oos": {"touched": False},
    }


def render_markdown(result: Mapping[str, object]) -> str:
    """Render a :func:`diagnose` payload as a concise markdown report."""
    lines = [
        "# Historical NIFTY options-data capability probe (read-only)",
        "",
        f"- Verdict gate: **`{result['verdict']}`**",
        f"- Reason: {result['reason']}",
        "- Source classifications:",
    ]
    sources = result.get("source_classifications", {})
    if not isinstance(sources, Mapping) or not sources:
        lines.append("  - (none)")
    else:
        for source_id in sorted(sources):
            lines.append(f"  - `{source_id}` -> `{sources[source_id]}`")
    lines.append("")
    lines.append("## Endpoints probed")
    lines.append("")
    probes = result.get("probes", [])
    if isinstance(probes, list) and probes:
        rows = ["- reachable,status,provider_code,provider_message,rows,observed_fields"]
        for probe in probes:
            if not isinstance(probe, Mapping):
                continue
            rows.append(
                f"- {probe.get('endpoint')}, {probe.get('reachable')}, "
                f"{probe.get('status')}, {probe.get('provider_code')}, "
                f"{probe.get('provider_message')}, {probe.get('rows')}, "
                f"{','.join(probe.get('observed_fields') or [])}"
            )
        lines.extend(rows)
    else:
        lines.append("- (none)")
    lines.append("")
    lines.append(f"- Credentials in output: `{result.get('credentials_in_output')}`")
    lines.append(f"- Protected OOS touched: `{result['protected_oos']['touched']}`")
    for note in result.get("evidence_notes", []):
        lines.append(f"- Note: {note}")
    return "\n".join(lines) + "\n"


__all__ = [
    "UPSTOX_BASE_URL",
    "NIFTY_UNDERLYING_KEY",
    "SOURCE_UPSTOX_LIVE_CHAIN",
    "SOURCE_UPSTOX_EXPIRED",
    "SOURCE_UPSTOX_V3_OPTION_CANDLE",
    "LIVE_CONTRACT_PATH",
    "LIVE_CHAIN_PATH",
    "EXPIRED_EXPIRIES_PATH",
    "EXPIRED_OPTION_CONTRACT_PATH",
    "EXPIRED_HISTORICAL_PATH",
    "EXPIRED_OPTION_EXPIRY_DATE",
    "EXPIRED_SAMPLE_INSTRUMENT_KEY",
    "EXPIRED_CANDLE_INTERVAL",
    "EXPIRED_CANDLE_FROM",
    "EXPIRED_CANDLE_TO",
    "HISTORICAL_CANDLE_PATH",
    "REDACTED",
    "EndpointProbe",
    "redact",
    "extract_upstox_error",
    "observed_chain_fields",
    "probe_live_contract",
    "probe_live_chain",
    "probe_expired_instruments",
    "probe_v3_option_candle",
    "source_finding_for",
    "diagnose",
    "render_markdown",
]