"""Live-trading credential provider tests (isolated, deterministic).

No network, no Upstox, no orders, no real environment. Pins: presence probing,
runtime in-memory resolution, fail-closed refusal without the execution token,
strict separation from the analytics/data token (``FNO_UPSTOX_*``), repr/str
redaction, and the no-order surface of the provider.
"""
from __future__ import annotations

import pytest

from fno_ai_paper_trading.execution.vnext.errors import VNextError
from fno_ai_paper_trading.execution.vnext.live_credentials import (
    LIVE_API_KEY_ENV,
    LIVE_BASE_URL_ENV,
    LIVE_CREDENTIAL_ENV,
    LIVE_TIMEOUT_ENV,
    REDACTED,
    STATE_ABSENT,
    STATE_PRESENT,
    LiveCredentialProvider,
    LiveCredentialState,
    LiveCredentialsUnavailableError,
    LiveTradingCredentials,
    redact_text,
)
from fno_ai_paper_trading.execution.vnext.upstox_broker import VNextUpstoxCredentials

TOKEN = "live-token-Δsecret"
API_KEY = "client-123"


def _env(overrides: dict | None = None) -> dict[str, str]:
    base = {
        LIVE_CREDENTIAL_ENV: TOKEN,
        LIVE_API_KEY_ENV: API_KEY,
        LIVE_BASE_URL_ENV: "https://api.upstox.com",
        LIVE_TIMEOUT_ENV: "7.5",
    }
    if overrides:
        base.update(overrides)
    return base


class TestPresence:
    def test_probe_present(self):
        provider = LiveCredentialProvider(_env())
        state = provider.probe()
        assert isinstance(state, LiveCredentialState)
        assert state.is_present is True
        assert str(state) == STATE_PRESENT

    def test_probe_absent(self):
        provider = LiveCredentialProvider(_env({LIVE_CREDENTIAL_ENV: ""}))
        assert provider.probe().is_present is False
        assert str(provider.probe()) == STATE_ABSENT

    def test_probe_never_leaks_token(self):
        provider = LiveCredentialProvider(_env())
        assert TOKEN not in str(provider.probe())
        assert TOKEN not in repr(provider.probe())


class TestResolution:
    def test_resolve_returns_credentials_in_memory(self):
        credentials = LiveCredentialProvider(_env()).resolve()
        assert isinstance(credentials, LiveTradingCredentials)
        assert credentials.access_token == TOKEN
        assert credentials.api_key == API_KEY
        assert credentials.base_url == "https://api.upstox.com"
        assert credentials.timeout_seconds == 7.5

    def test_absent_resolution_fails_closed_before_any_http(self):
        provider = LiveCredentialProvider(_env({LIVE_CREDENTIAL_ENV: "  "}))
        with pytest.raises(LiveCredentialsUnavailableError, match=LIVE_CREDENTIAL_ENV):
            provider.resolve()

    def test_error_message_names_only_the_env_variable(self):
        provider = LiveCredentialProvider(_env({LIVE_CREDENTIAL_ENV: ""}))
        try:
            provider.resolve()
        except LiveCredentialsUnavailableError as exc:
            assert TOKEN not in str(exc)

    def test_api_key_optional(self):
        credentials = LiveTradingCredentials.from_env(
            _env({LIVE_API_KEY_ENV: ""})
        )
        assert credentials.configured is True
        assert "x-api-key" not in credentials.headers()

    def test_headers_carries_bearer_only_with_token(self):
        headers = LiveTradingCredentials.from_env(_env()).headers(json_body=True)
        assert headers["Authorization"] == f"Bearer {TOKEN}"
        assert headers["x-api-key"] == API_KEY
        assert headers["Content-Type"] == "application/json"


class TestSeparation:
    def test_never_reads_the_data_token(self):
        env = {
            "FNO_UPSTOX_ACCESS_TOKEN": "data-token-secret",
            LIVE_API_KEY_ENV: API_KEY,
        }
        provider = LiveCredentialProvider(env)
        assert provider.probe().is_present is False
        with pytest.raises(LiveCredentialsUnavailableError):
            provider.resolve()

    def test_no_fallback_to_data_token(self):
        provider = LiveCredentialProvider(
            {"FNO_UPSTOX_ACCESS_TOKEN": "data-token-secret"}
        )
        with pytest.raises(LiveCredentialsUnavailableError):
            provider.resolve()

    def test_from_env_ignores_fno_names(self):
        credentials = LiveTradingCredentials.from_env(
            {"FNO_UPSTOX_ACCESS_TOKEN": "data-token-secret"}
        )
        assert credentials.configured is False

    def test_provider_has_no_order_surface(self):
        assert not hasattr(LiveCredentialProvider, "submit_order")
        assert not hasattr(LiveTradingCredentials, "submit_order")

    def test_unavailable_is_a_vnext_error(self):
        assert issubclass(LiveCredentialsUnavailableError, VNextError)


class TestRedaction:
    def test_redact_text_scrubs_token(self):
        text = f"Authorization: Bearer {TOKEN} failed"
        assert TOKEN not in redact_text(text, TOKEN)
        assert REDACTED in redact_text(text, TOKEN)

    def test_redact_text_noop_without_token(self):
        assert redact_text("plain text", "") == "plain text"

    def test_provider_redact_text_uses_resolved_token(self):
        provider = LiveCredentialProvider(_env())
        assert provider.redact_text(f"err {TOKEN}") == f"err {REDACTED}"

    def test_provider_redact_text_passthrough_when_absent(self):
        provider = LiveCredentialProvider(_env({LIVE_CREDENTIAL_ENV: ""}))
        assert provider.redact_text("no credentials") == "no credentials"


class TestReprSafety:
    def test_repr_redacts_token(self):
        text = repr(LiveTradingCredentials(access_token=TOKEN))
        assert TOKEN not in text
        assert "<redacted>" in text

    def test_str_and_repr_of_state_are_safe(self):
        provider = LiveCredentialProvider(_env())
        state = provider.probe()
        assert TOKEN not in str(state)
        assert TOKEN not in repr(state)

    def test_adapter_alias_is_the_credential_type(self):
        assert VNextUpstoxCredentials is LiveTradingCredentials


class TestValidation:
    def test_nonpositive_timeout_rejected(self):
        with pytest.raises(ValueError):
            LiveTradingCredentials(timeout_seconds=0)

    def test_bad_timeout_env_defaults(self):
        assert LiveTradingCredentials.from_env(
            _env({LIVE_TIMEOUT_ENV: "not-a-number"})
        ).timeout_seconds == 10.0

    def test_base_url_strips_trailing_slash(self):
        assert LiveTradingCredentials.from_env(
            _env({LIVE_BASE_URL_ENV: "https://api.upstox.com/"})
        ).base_url == "https://api.upstox.com"