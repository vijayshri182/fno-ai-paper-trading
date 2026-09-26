"""Hermetic tests for the Phase 12 historical options-data capability probe.

Covers the provider-neutral data-acquisition capability evidence pipeline:
probe construction, Upstox error/entitlement handling (HTTP 401 UDAPI1149
preserved verbatim, token redaction), live-chain field observation, v3 option
candle depth limits, source classification through ``assess_readiness`` and the
deterministic, credential-free ``diagnose`` verdict.

Safety: no network (``http_fn`` is injected), no wall-clock reads, no
credentials, no ``execution``/``credentials``/Upstox imports in the module
under test, no protected-OOS access, and synthetic fixtures can never present
observed market claims.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Callable

from fno_ai_paper_trading.options_research.capability import (
    SOURCE_BLOCKED,
    SOURCE_NOT_HISTORICAL,
)
from fno_ai_paper_trading.options_research.capability import (
    HISTORICAL_OPTIONS_DATA_UNAVAILABLE,
    classify_source,
)
from fno_ai_paper_trading.research.options.capability_probe import (
    EXPIRED_OPTION_CONTRACT_PATH,
    EXPIRED_HISTORICAL_PATH,
    EXPIRED_EXPIRIES_PATH,
    HISTORICAL_CANDLE_PATH,
    LIVE_CHAIN_PATH,
    LIVE_CONTRACT_PATH,
    REDACTED,
    EndpointProbe,
    diagnose,
    extract_upstox_error,
    observed_chain_fields,
    probe_expired_instruments,
    probe_live_chain,
    probe_live_contract,
    probe_v3_option_candle,
    redact,
    render_markdown,
    source_finding_for,
)
from fno_ai_paper_trading.utils.http import HttpError

ROOT = Path(__file__).resolve().parents[1]
PROBE_MODULE = ROOT / "src/fno_ai_paper_trading/research/options/capability_probe.py"

_API = "https://api.upstox.com"
_TOKEN = "tok-0123456789abcdef"


class _NotJson:
    pass


_NOT_JSON = _NotJson()


class _FakeResponse:
    def __init__(self, status: int, payload: object = None) -> None:
        self.status = status
        self._payload = payload

    @property
    def json(self) -> object:
        if self._payload is _NOT_JSON:
            raise json.JSONDecodeError("not json", "body", 0)
        return self._payload


def _raise_http(
    status: int,
    body_text: str,
    path: str = "/v2/option/chain",
) -> Callable:
    def handler(url, *, params=None, headers=None, timeout=None) -> object:
        raise HttpError(status, f"{_API}{path}", body_text.encode("utf-8"))

    return handler


def _reply(payload: object, status: int = 200) -> Callable:
    def handler(url, *, params=None, headers=None, timeout=None) -> object:
        return _FakeResponse(status, payload)

    return handler


def _capture_headers() -> tuple[list[dict[str, str]], Callable]:
    captured: list[dict[str, str]] = []

    def handler(url, *, params=None, headers=None, timeout=None) -> object:
        captured.append(dict(headers or {}))
        return _FakeResponse(200, {"data": []})

    return captured, handler


def _chain_record() -> dict[str, object]:
    return {
        "expiry": "2026-10-01",
        "strike_price": 25000,
        "underlying_spot_price": 25234.5,
        "call_options": {
            "instrument_key": "NSE_FO|NIFTY 01OCT2026 25000 CE",
            "expiry": "2026-10-01",
            "strike_price": 25000,
            "option_type": "CE",
            "market_data": {
                "ltp": 120.5,
                "bid_price": 120.0,
                "ask_price": 121.0,
                "volume": 50,
                "oi": 25000,
                "prev_oi": 24000,
            },
            "option_greeks": {"iv": 0.12, "delta": 0.55, "gamma": 0.0001, "theta": -0.01, "vega": 0.02},
        },
        "put_options": {
            "instrument_key": "NSE_FO|NIFTY 01OCT2026 25000 PE",
            "expiry": "2026-10-01",
            "strike_price": 25000,
            "option_type": "PE",
            "market_data": {"ltp": None, "bid_price": None, "ask_price": None, "volume": None, "oi": 100, "prev_oi": None},
        },
    }


# ------------------------------------------------------------------ redaction / errors

class TestRedactionAndErrors:
    def test_redact_replaces_token(self) -> None:
        assert redact(f"Bearer {_TOKEN} value {_TOKEN}", _TOKEN) == f"Bearer {REDACTED} value {REDACTED}"

    def test_redact_noop_without_token(self) -> None:
        assert redact("no token", "") == "no token"
        assert redact("plain text", _TOKEN) == "plain text"

    def test_extract_upstox_error_variants(self) -> None:
        assert extract_upstox_error(
            '{"error_code":"UDAPI1149","error_message":"Upstox Plus plan required"}'
        ) == ("UDAPI1149", "Upstox Plus plan required")
        assert extract_upstox_error('{"errors":[{"code":"A1","message":"boom"}]}') == ("A1", "boom")
        assert extract_upstox_error('{"title":"Error 1010"}') == (None, "Error 1010")
        assert extract_upstox_error('{"detail":"gone"}') == (None, "gone")
        assert extract_upstox_error("not json") == (None, None)
        assert extract_upstox_error("") == (None, None)
        assert extract_upstox_error("[]") == (None, None)

    def test_probe_sends_bearer_token_but_captures_never_echo(self) -> None:
        captured, handler = _capture_headers()
        probe = probe_live_chain(token=_TOKEN, expiry_date="2026-10-01", http_fn=handler)
        assert probe.reachable
        assert captured and captured[0]["Authorization"] == f"Bearer {_TOKEN}"
        serialized = json.dumps(probe.to_dict())
        assert _TOKEN not in serialized


# ------------------------------------------------------------------ live surfaces

class TestProbeLive:
    def test_live_contract_expiries(self) -> None:
        payload = {
            "data": [
                {"expiry": "2026-10-01", "strike_price": 25000},
                {"expiry": "2026-09-30", "strike_price": 24900},
            ]
        }
        probe = probe_live_contract(token=_TOKEN, http_fn=_reply(payload))
        assert probe.reachable
        assert probe.status == 200
        assert probe.role == "LIVE_CONTRACT"
        assert probe.expiries == ("2026-09-30", "2026-10-01")
        assert probe.rows == 2
        assert "underlying_spot_price" not in probe.observed_fields

    def test_live_chain_observes_available_fields_only(self) -> None:
        probe = probe_live_chain(
            token=_TOKEN, expiry_date="2026-10-01", http_fn=_reply({"data": [_chain_record()]})
        )
        assert probe.reachable
        assert probe.role == "LIVE_CHAIN"
        assert probe.rows == 1
        assert probe.first_option_key == "NSE_FO|NIFTY 01OCT2026 25000 CE"
        assert "underlying_spot_price" in probe.observed_fields
        assert "ltp" in probe.observed_fields
        assert "bid_price" in probe.observed_fields and "ask_price" in probe.observed_fields
        assert "volume" in probe.observed_fields
        assert "oi" in probe.observed_fields
        assert "iv" in probe.observed_fields
        assert "delta" in probe.observed_fields

    def test_entitlement_failure_preserved_verbatim_and_redacted(self) -> None:
        body = json.dumps({"error_code": "UDAPI1149", "error_message": f"Upstox Plus plan required {_TOKEN}"})
        handler = _raise_http(401, body, LIVE_CHAIN_PATH)
        probe = probe_live_contract(token=_TOKEN, http_fn=handler)
        assert probe.reachable
        assert probe.status == 401
        assert probe.provider_code == "UDAPI1149"
        assert _TOKEN not in (probe.provider_message or "")
        assert REDACTED in (probe.provider_message or "")
        assert probe.notes == ("non-2xx provider response; signalled entitlement/access preserved verbatim",)

    def test_non_json_body_noted(self) -> None:
        probe = probe_live_chain(
            token=_TOKEN, expiry_date="2026-10-01", http_fn=_reply(_NOT_JSON, status=200)
        )
        assert probe.reachable
        assert probe.notes == ("non-JSON response body",)


# ------------------------------------------------------------------ expired surfaces

class TestProbeExpired:
    def test_all_expired_endpoints_401(self) -> None:
        handler = _raise_http(
            401,
            json.dumps({"error_code": "UDAPI1149", "error_message": "Upstox Plus plan required"}),
        )
        expiries, contract, history = probe_expired_instruments(token=_TOKEN, http_fn=handler)
        for probe in (expiries, contract, history):
            assert probe.reachable
            assert probe.status == 401
            assert probe.provider_code == "UDAPI1149"
            assert probe.provider_message == "Upstox Plus plan required"
        assert expiries.endpoint == f"GET {EXPIRED_EXPIRIES_PATH}"
        assert contract.endpoint == f"GET {EXPIRED_OPTION_CONTRACT_PATH}"
        assert history.endpoint == f"GET {EXPIRED_HISTORICAL_PATH}"

    def test_expired_probes_send_documented_params(self) -> None:
        calls: list[dict[str, object]] = []

        def handler(url, *, params=None, headers=None, timeout=None) -> object:
            calls.append({"url": url, "params": params})
            raise HttpError(
                401,
                url,
                json.dumps({"error_code": "UDAPI1149", "error_message": "Upstox Plus plan required"}).encode(),
            )

        expiries, contract, history = probe_expired_instruments(token=_TOKEN, http_fn=handler)
        assert calls[0]["params"] == {"instrument_key": "NSE_INDEX|Nifty 50"}
        assert calls[1]["params"] == {
            "instrument_key": "NSE_INDEX|Nifty 50",
            "expiry_date": "2025-09-25",
        }
        assert str(calls[2]["url"]).endswith(
            "NSE_FO%7C73507%7C24-04-2025/5minute/2025-04-24/2025-04-24"
        )
        assert all(probe.status == 401 for probe in (expiries, contract, history))

    def test_expired_transport_failure_unreachable(self) -> None:
        def handler(url, *, params=None, headers=None, timeout=None) -> object:
            raise OSError("network down")

        expiries, _, _ = probe_expired_instruments(token=_TOKEN, http_fn=handler)
        assert not expiries.reachable
        assert expiries.status is None
        assert any("transport failure" in note for note in expiries.notes)


# ------------------------------------------------------------------ v3 option candle

class TestProbeV3OptionCandle:
    def test_zero_rows_pre_listing_window(self) -> None:
        probe = probe_v3_option_candle(
            token=_TOKEN,
            option_key="NSE_FO|NIFTY 01OCT2026 25000 CE",
            from_date="2024-01-02",
            to_date="2024-01-05",
            http_fn=_reply({"status": "success", "data": {"candles": []}}),
        )
        assert probe.reachable
        assert probe.rows == 0
        assert probe.notes == ("0 candles returned (no pre-listing history)",)
        assert probe.endpoint == f"GET {HISTORICAL_CANDLE_PATH}/{{...}}/minutes/5/{{to}}/{{from}}"

    def test_counts_returned_candles(self) -> None:
        candles = [[1704170700, 100, 101, 99, 100.5, 10, 0]] * 3
        probe = probe_v3_option_candle(
            token=_TOKEN,
            option_key="NSE_FO|NIFTY 01OCT2026 25000 CE",
            from_date="2026-09-01",
            to_date="2026-09-02",
            http_fn=_reply({"status": "success", "data": {"candles": candles}}),
        )
        assert probe.rows == 3
        assert probe.notes == ("3 candles returned",)


# ------------------------------------------------------------------ classification

class TestSourceFinding:
    def _live_probe(self) -> EndpointProbe:
        return probe_live_chain(
            token=_TOKEN, expiry_date="2026-10-01", http_fn=_reply({"data": [_chain_record()]})
        )

    def test_live_chain_is_not_historical(self) -> None:
        finding = source_finding_for(self._live_probe())
        assert not finding.historical_retrieval
        assert finding.observed_bid_ask and finding.observed_ltp and finding.observed_oi
        assert classify_source(finding) == SOURCE_NOT_HISTORICAL

    def test_expired_401_blocked_with_entitlement_note(self) -> None:
        handler = _raise_http(
            401,
            json.dumps({"error_code": "UDAPI1149", "error_message": "Upstox Plus plan required"}),
            EXPIRED_EXPIRIES_PATH,
        )
        (expiry_probe, _, _) = probe_expired_instruments(token=_TOKEN, http_fn=handler)
        finding = source_finding_for(expiry_probe)
        assert finding.historical_retrieval
        assert not finding.reproducible
        assert "Upstox Plus plan required (UDAPI1149)" in finding.licensing_access
        assert classify_source(finding) == SOURCE_BLOCKED

    def test_v3_option_candle_is_not_as_of_history(self) -> None:
        probe = probe_v3_option_candle(
            token=_TOKEN,
            option_key="NSE_FO|NIFTY 01OCT2026 25000 CE",
            from_date="2024-01-02",
            to_date="2024-01-05",
            http_fn=_reply({"status": "success", "data": {"candles": []}}),
        )
        finding = source_finding_for(probe)
        assert not finding.historical_retrieval
        assert classify_source(finding) == SOURCE_NOT_HISTORICAL


# ------------------------------------------------------------------ diagnose / render

def _assemble_probes() -> tuple[EndpointProbe, ...]:
    contract = probe_live_contract(
        token=_TOKEN, http_fn=_reply({"data": [{"expiry": "2026-10-01", "strike_price": 25000}]})
    )
    chain = probe_live_chain(token=_TOKEN, expiry_date="2026-10-01", http_fn=_reply({"data": [_chain_record()]}))
    expired = []
    handler = _raise_http(
        401,
        json.dumps({"error_code": "UDAPI1149", "error_message": "Upstox Plus plan required"}),
    )
    expired.extend(probe_expired_instruments(token=_TOKEN, http_fn=handler))
    v3 = probe_v3_option_candle(
        token=_TOKEN,
        option_key="NSE_FO|NIFTY 01OCT2026 25000 CE",
        from_date="2024-01-02",
        to_date="2024-01-05",
        http_fn=_reply({"status": "success", "data": {"candles": []}}),
    )
    return (contract, chain, *expired, v3)


class TestDiagnose:
    def test_verdict_unavailable_with_this_access(self) -> None:
        result = diagnose(_assemble_probes())
        assert result["verdict"] == HISTORICAL_OPTIONS_DATA_UNAVAILABLE
        assert result["credentials_in_output"] is False
        assert result["protected_oos"] == {"touched": False}
        assert "verified historical option-chain data is not retrievable" in result["reason"] or "live-only" in result["reason"]

    def test_deterministic_output(self) -> None:
        first = diagnose(_assemble_probes())
        second = diagnose(_assemble_probes())
        assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)

    def test_no_credentials_in_any_probe_dict(self) -> None:
        for probe in _assemble_probes():
            assert _TOKEN not in json.dumps(probe.to_dict())

    def test_render_markdown_never_leaks_token(self) -> None:
        body = json.dumps({"error_code": "UDAPI1149", "error_message": f"Upstox Plus plan required {_TOKEN}"})
        probes = (probe_live_contract(token=_TOKEN, http_fn=_raise_http(401, body)),)
        rendered = render_markdown(diagnose(probes))
        assert _TOKEN not in rendered
        assert REDACTED in rendered
        assert "HISTORICAL_OPTIONS_DATA_UNAVAILABLE" in rendered


# ------------------------------------------------------------------ safety

class TestProbeSafety:
    def test_module_import_safety(self) -> None:
        source = PROBE_MODULE.read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported.add(alias.name)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        roots = {name.split(".")[0] for name in imported}
        for banned in ("execution", "upstox", "credentials"):
            assert banned not in roots, f"capability_probe imports forbidden root {banned}"
        for banned_module in (
            "fno_ai_paper_trading.execution",
            "fno_ai_paper_trading.broker",
            "fno_ai_paper_trading.credentials",
            "fresh_oos",
            "fno_ai_paper_trading.options_research.windows",
        ):
            assert not any(name.startswith(banned_module) for name in imported), (
                f"capability_probe imports forbidden module {banned_module}"
            )
        assert "fno_ai_paper_trading.research.options.upstox_adapter" not in imported
        assert "fno_ai_paper_trading.research.options.upstox_diagnostic" not in imported

    def test_no_wall_clock_or_protected_window_references(self) -> None:
        source = PROBE_MODULE.read_text(encoding="utf-8")
        assert "datetime.now" not in source
        assert "utcnow" not in source
        assert "import time" not in source and "from time import" not in source
        assert "PROTECTED_OOS" not in source
        assert "fresh_oos" not in source
        assert "2025-10-06" not in source and "2026-09-11" not in source

    def test_fixtures_never_masquerade_as_observed(self) -> None:
        synthetic = EndpointProbe(
            source_id="SYNTHETIC_FIXTURE-candle",
            endpoint="GET /v3/historical-candle/{...}",
            role="V3_OPTION_CANDLE",
            reachable=True,
            status=200,
            rows=0,
        )
        finding = source_finding_for(synthetic)
        assert finding.synthetic is False
        assert classify_source(finding) == SOURCE_NOT_HISTORICAL
        assert diagnose((synthetic,))["verdict"] == HISTORICAL_OPTIONS_DATA_UNAVAILABLE