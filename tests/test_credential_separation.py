"""Credential-separation tests for the WS 7.9 execution layer.

Guarantees the analytics/data Upstox credential (``FNO_UPSTOX_ACCESS_TOKEN``)
and the execution credential (``UPSTOX_ACCESS_TOKEN``) are fully disjoint:

* the data layer reads only ``FNO_UPSTOX_*`` and never falls back to the
  execution token,
* the execution adapter reads only ``UPSTOX_*`` and never falls back to the
  analytics token,
* paper trading never requires the execution token,
* the presence of ``UPSTOX_ACCESS_TOKEN`` alone can never open the live gate,
* a missing/invalid execution token fails **before** any real order POST,
* the token value never leaks into errors, reprs, reasons, or audit output.

Everything here is deterministic and offline — no HTTP write is ever possible.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from fno_ai_paper_trading.config.settings import (
    LiveExecutionTestSettings,
    load_settings,
    load_upstox_settings,
)
from fno_ai_paper_trading.data.errors import ProviderConfigurationError
from fno_ai_paper_trading.data.instrument_registry import get_research_instrument
from fno_ai_paper_trading.data.upstox_provider import UpstoxHistoricalDataProvider
from fno_ai_paper_trading.execution.audit import ExecutionAudit, redact
from fno_ai_paper_trading.execution.errors import UpstoxExecutionError
from fno_ai_paper_trading.execution.gate import (
    ExecutionMode,
    LiveExecutionTestGate,
    consent_fingerprint,
)
from fno_ai_paper_trading.execution.instrument import resolve_fno_instrument
from fno_ai_paper_trading.execution.upstox import (
    UpstoxCredentials,
    UpstoxExecutionAdapter,
)
from fno_ai_paper_trading.models.enums import OrderSide
from fno_ai_paper_trading.models.order import Order
from fno_ai_paper_trading.utils.http import HttpError

DATA_TOKEN = "DATA-TOKEN-1234567890"
EXEC_TOKEN = "EXEC-TOKEN-0987654321"
EXEC_SECRET = "app-secret-never-leaked-abcdef"
HEX_TOKEN = "abcdef0123456789abcdef0123456789abcdef01"
KEY = "NSE_FO|NIFTY 24 DEC 2026 24500 CE"
LOT = 75
NOW = datetime(2026, 9, 14, 12, 0)


def _option() -> object:
    return resolve_fno_instrument(
        underlying="NIFTY",
        expiry=date(2026, 12, 24),
        strike=Decimal("24500"),
        option_type="CE",
        exchange_token=KEY,
        lot_size=LOT,
    )


def _buy_order() -> Order:
    return Order(instrument=_option(), side=OrderSide.BUY, quantity=LOT)


def _open_consent(token: str) -> dict[str, object]:
    created = NOW - timedelta(hours=1)
    return {
        "operator": "tester",
        "purpose": "controlled live execution integration test",
        "created_at": created.isoformat(timespec="seconds"),
        "expires_at": (created + timedelta(hours=6)).isoformat(timespec="seconds"),
        "token_fingerprint_sha256": consent_fingerprint(token),
    }


@dataclass
class _Spy:
    calls: list = None

    def __post_init__(self) -> None:
        self.calls = []

    def __call__(self, method, url, *, headers=None, timeout=10.0, data=None, **kwargs):
        self.calls.append((method, url))
        raise HttpError(401, url, b"{}")


def _raise_http_error(method, url, *, headers=None, timeout=10.0, data=None, **kwargs):
    raise HttpError(401, url, b"{}")


# ---------------------------------------------------------------------------
# Analytics / data-layer credentials
# ---------------------------------------------------------------------------


class TestAnalyticsDataCredentials:
    def test_data_settings_ignore_execution_token(self, monkeypatch) -> None:
        monkeypatch.setenv("UPSTOX_ACCESS_TOKEN", EXEC_TOKEN)
        monkeypatch.setenv("FNO_UPSTOX_ACCESS_TOKEN", "")
        settings = load_upstox_settings()
        assert settings.access_token == ""
        assert settings.configured is False

    def test_data_settings_read_only_the_analytics_token(self, monkeypatch) -> None:
        monkeypatch.setenv("UPSTOX_ACCESS_TOKEN", EXEC_TOKEN)
        monkeypatch.setenv("FNO_UPSTOX_ACCESS_TOKEN", DATA_TOKEN)
        settings = load_upstox_settings()
        assert settings.access_token == DATA_TOKEN
        assert settings.configured is True

    def test_data_provider_never_consumes_execution_token(self, monkeypatch) -> None:
        monkeypatch.setenv("UPSTOX_ACCESS_TOKEN", EXEC_TOKEN)
        monkeypatch.setenv("FNO_UPSTOX_ACCESS_TOKEN", "")
        provider = UpstoxHistoricalDataProvider(access_token="")
        with pytest.raises(ProviderConfigurationError, match="FNO_UPSTOX_ACCESS_TOKEN"):
            provider.get_ohlcv(get_research_instrument("NIFTY 50"), limit=1)

    def test_paper_mode_does_not_require_execution_token(self, monkeypatch) -> None:
        monkeypatch.setenv("UPSTOX_ACCESS_TOKEN", "")
        monkeypatch.setenv("FNO_UPSTOX_ACCESS_TOKEN", "")
        # Paper settings load cleanly with zero Upstox credentials.
        paper = load_settings()
        assert paper is not None
        # The data provider object builds fine; it only fails-closed at the
        # point of a real fetch, and the failure names the DATA token — never
        # the execution token.
        UpstoxHistoricalDataProvider(access_token="")
        with pytest.raises(ProviderConfigurationError, match="FNO_UPSTOX_ACCESS_TOKEN"):
            UpstoxHistoricalDataProvider(access_token="")._headers


# ---------------------------------------------------------------------------
# Execution-layer credentials
# ---------------------------------------------------------------------------


class TestExecutionCredentials:
    def test_execution_credentials_read_execution_env_only(self, monkeypatch) -> None:
        monkeypatch.setenv("UPSTOX_ACCESS_TOKEN", EXEC_TOKEN)
        monkeypatch.setenv("FNO_UPSTOX_ACCESS_TOKEN", DATA_TOKEN)
        credentials = UpstoxCredentials.from_env()
        assert credentials.access_token == EXEC_TOKEN

    def test_execution_credentials_never_fall_back_to_analytics_token(self, monkeypatch) -> None:
        monkeypatch.setenv("UPSTOX_ACCESS_TOKEN", "")
        monkeypatch.setenv("FNO_UPSTOX_ACCESS_TOKEN", DATA_TOKEN)
        credentials = UpstoxCredentials.from_env()
        assert credentials.access_token == ""
        assert credentials.configured is False

    def test_unconfigured_credentials_raise_before_any_headers(self) -> None:
        with pytest.raises(UpstoxExecutionError, match="UPSTOX_ACCESS_TOKEN"):
            UpstoxCredentials().headers()

    def test_adapter_requires_token_for_non_dry_run_and_never_posts(self) -> None:
        spy = _Spy()
        adapter = UpstoxExecutionAdapter(
            UpstoxCredentials(),
            dry_run=False,
            instrument_tokens={KEY: 123456},
            request=spy,
            now_fn=lambda: NOW,
        )
        with pytest.raises(UpstoxExecutionError, match="UPSTOX_ACCESS_TOKEN"):
            adapter.place_order(_buy_order())
        assert spy.calls == []  # zero HTTP writes — the failure happens up-front

    def test_adapter_does_not_fall_back_to_analytics_token(self, monkeypatch) -> None:
        monkeypatch.setenv("UPSTOX_ACCESS_TOKEN", "")
        monkeypatch.setenv("FNO_UPSTOX_ACCESS_TOKEN", DATA_TOKEN)
        spy = _Spy()
        adapter = UpstoxExecutionAdapter(
            UpstoxCredentials.from_env(),
            dry_run=False,
            instrument_tokens={KEY: 123456},
            request=spy,
            now_fn=lambda: NOW,
        )
        with pytest.raises(UpstoxExecutionError):
            adapter.place_order(_buy_order())
        assert spy.calls == []

    def test_invalid_token_yields_typed_failure_and_no_ack(self) -> None:
        adapter = UpstoxExecutionAdapter(
            UpstoxCredentials(access_token=EXEC_TOKEN),
            dry_run=False,
            instrument_tokens={KEY: 123456},
            request=_raise_http_error,
            now_fn=lambda: NOW,
        )
        with pytest.raises(Exception) as excinfo:
            adapter.place_order(_buy_order())
        assert "Upstox rejected the access token" in str(excinfo.value)

    # ------------------------------------------------------------------
    # UPSTOX_API_SECRET role: OAuth exchange only, never on API calls
    # ------------------------------------------------------------------

    def test_execution_credentials_read_api_key_and_secret(self, monkeypatch) -> None:
        monkeypatch.setenv("UPSTOX_API_KEY", "app-key")
        monkeypatch.setenv("UPSTOX_API_SECRET", EXEC_SECRET)
        monkeypatch.setenv("UPSTOX_ACCESS_TOKEN", EXEC_TOKEN)
        credentials = UpstoxCredentials.from_env()
        assert credentials.api_key == "app-key"
        assert credentials.api_secret == EXEC_SECRET

    def test_execution_never_falls_back_to_analytics_secret(self, monkeypatch) -> None:
        monkeypatch.setenv("UPSTOX_API_SECRET", "")
        monkeypatch.setenv("FNO_UPSTOX_CLIENT_SECRET", "analytics-ss-flow-secret")
        credentials = UpstoxCredentials.from_env()
        assert credentials.api_secret == ""

    def test_api_secret_never_enters_request_headers(self) -> None:
        credentials = UpstoxCredentials(
            access_token=EXEC_TOKEN, api_key="app-key", api_secret=EXEC_SECRET
        )
        headers = credentials.headers()
        joined = "\n".join(str(k) + ": " + str(v) for k, v in headers.items())
        assert EXEC_SECRET not in joined
        assert "api_secret" not in headers

    def test_api_secret_never_appears_in_repr(self) -> None:
        credentials = UpstoxCredentials(api_secret=EXEC_SECRET)
        rendered = repr(credentials)
        assert EXEC_SECRET not in rendered
        assert "api_secret_configured=True" in rendered


# ---------------------------------------------------------------------------
# Enablement gating — a token is necessary but never sufficient
# ---------------------------------------------------------------------------


class TestEnablementGating:
    def test_token_alone_can_never_open_the_gate(self) -> None:
        gate = LiveExecutionTestGate(
            LiveExecutionTestSettings(enabled=False),
            now_fn=lambda: NOW,
            token_loader=lambda: EXEC_TOKEN,
            consent_loader=lambda: _open_consent(EXEC_TOKEN),
        )
        decision = gate.decision()
        assert decision.ok is False
        assert any("FNO_LIVE_EXECUTION_TEST_ENABLED" in r for r in decision.reasons)

    def test_token_without_consent_is_still_closed(self) -> None:
        gate = LiveExecutionTestGate(
            LiveExecutionTestSettings(enabled=True),
            now_fn=lambda: NOW,
            token_loader=lambda: EXEC_TOKEN,
            consent_loader=lambda: None,
        )
        decision = gate.decision()
        assert decision.ok is False
        assert any("consent file" in r for r in decision.reasons)

    def test_token_must_match_consent_fingerprint(self) -> None:
        gate = LiveExecutionTestGate(
            LiveExecutionTestSettings(enabled=True),
            now_fn=lambda: NOW,
            token_loader=lambda: EXEC_TOKEN,
            consent_loader=lambda: _open_consent("SOME-OTHER-TOKEN"),
        )
        decision = gate.decision()
        assert decision.ok is False
        assert any("fingerprint" in r for r in decision.reasons)

    def test_full_chain_required_before_live_test_mode(self) -> None:
        gate = LiveExecutionTestGate(
            LiveExecutionTestSettings(enabled=True),
            now_fn=lambda: NOW,
            token_loader=lambda: EXEC_TOKEN,
            consent_loader=lambda: _open_consent(EXEC_TOKEN),
        )
        decision = gate.decision()
        assert decision.ok is True
        assert decision.mode is ExecutionMode.LIVE_EXECUTION_TEST
        assert decision.mode.can_write_orders is True


# ---------------------------------------------------------------------------
# No token leakage into representations / reasons / audit output
# ---------------------------------------------------------------------------


class TestNoTokenLeakage:
    def test_repr_never_contains_the_token(self) -> None:
        credentials = UpstoxCredentials(access_token=EXEC_TOKEN, api_key="api-x")
        rendered = repr(credentials)
        assert EXEC_TOKEN not in rendered
        assert "<redacted>" in rendered

    def test_gate_reasons_never_contain_the_token_value(self) -> None:
        gate = LiveExecutionTestGate(
            LiveExecutionTestSettings(enabled=True),
            now_fn=lambda: NOW,
            token_loader=lambda: EXEC_TOKEN,
            consent_loader=lambda: None,
        )
        summary = gate.decision().summary
        assert EXEC_TOKEN not in summary

    def test_redact_covers_long_hex_and_bearer_tokens(self) -> None:
        text = f"order ok token={HEX_TOKEN} then Bearer {EXEC_TOKEN} and done"
        sanitized = redact(text)
        assert HEX_TOKEN not in sanitized
        assert "Bearer <redacted>" in sanitized
        assert "<redacted>" in sanitized

    def test_audit_output_contains_no_token_value(self) -> None:
        rows: list[str] = []

        def sink(payload) -> None:
            rows.append(str(payload))

        audit = ExecutionAudit("sep-test", sink=sink)
        audit.record("probe", note=f"Authorization: Bearer {EXEC_TOKEN} hex {HEX_TOKEN}")
        joined = "\n".join(rows)
        assert EXEC_TOKEN not in joined
        assert HEX_TOKEN not in joined
        assert "Bearer <redacted>" in joined