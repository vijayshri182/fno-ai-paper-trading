"""Focused tests for the runtime, in-memory-only Upstox credential provider.

Covers the guarantees that matter for the deployed Task Scheduler path:

* the token is resolved **only at runtime** from the process environment;
* it is returned to the caller in memory only and is never persisted/printed;
* operators are shown exactly ``PRESENT`` / ``ABSENT`` -- never the value;
* failure is fail-closed (``CredentialsUnavailableError`` -> ``AUTH_REQUIRED``);
* the resolved token is stripped from downstream exception text before it can
  reach the manifest.
"""
from __future__ import annotations

from datetime import date

import pytest

from fresh_oos_testkit import FakeHistoricalDataClient, make_context
from fno_ai_paper_trading.data.upstox_provider import UpstoxHistoricalDataProvider
from fno_ai_paper_trading.fresh_oos.client import UpstoxHistoricalDataClient
from fno_ai_paper_trading.fresh_oos.credential_provider import (
    CREDENTIAL_ENV,
    RuntimeCredentialProvider,
    redact_text,
)
from fno_ai_paper_trading.fresh_oos.errors import CredentialsUnavailableError

TOKEN = "test-only-secret-token-8492"  # synthetic; never a real Upstox value


def _instrument():
    from fno_ai_paper_trading.data.instrument_registry import get_research_instrument

    return get_research_instrument("NIFTY 50")


class _NoNetworkProvider(UpstoxHistoricalDataProvider):
    """Provider stub whose transport can never reach the network."""

    def __init__(self) -> None:
        super().__init__(access_token="stub-token")
        self.get_historical_calls: list[date] = []

    def _get(self, path: str):  # pragma: no cover - must never be invoked
        raise AssertionError("_NoNetworkProvider._get must never run")

    def get_historical_ohlcv(self, instrument, interval, start, end) -> list:
        self.get_historical_calls.append(start.date())
        return []


def _provider_with(token: str | None) -> RuntimeCredentialProvider:
    env = {"FNO_UPSTOX_ACCESS_TOKEN": token} if token else {}
    return RuntimeCredentialProvider(environ=env)


# --------------------------------------------------------------------- #
# probe(): PRESENT/ABSENT only, never the value
# --------------------------------------------------------------------- #


def test_probe_reports_present_without_value():
    state = _provider_with(TOKEN).probe()
    assert state.state == "PRESENT"
    assert state.is_present is True
    assert TOKEN not in state.message
    assert TOKEN not in str(state)
    assert TOKEN not in repr(state)


def test_probe_reports_absent_without_value():
    state = _provider_with(None).probe()
    assert state.state == "ABSENT"
    assert state.is_present is False
    assert TOKEN not in state.message
    assert TOKEN not in str(state)
    assert TOKEN not in repr(state)
    assert CREDENTIAL_ENV in state.message  # names the missing variable, fine


# --------------------------------------------------------------------- #
# resolve(): in-memory only, fail-closed
# --------------------------------------------------------------------- #


def test_resolve_returns_token_to_caller_in_memory_only():
    assert _provider_with(TOKEN).resolve() == TOKEN


def test_resolve_fails_closed_with_auth_required_when_absent():
    with pytest.raises(CredentialsUnavailableError) as exc:
        _provider_with(None).resolve()
    assert CREDENTIAL_ENV in str(exc.value)
    assert TOKEN not in str(exc.value)


def test_resolve_uses_process_environment_by_default(monkeypatch):
    monkeypatch.setenv(CREDENTIAL_ENV, TOKEN)
    try:
        provider = RuntimeCredentialProvider()
        assert provider.probe().state == "PRESENT"
        assert provider.resolve() == TOKEN
    finally:
        monkeypatch.delenv(CREDENTIAL_ENV, raising=False)
    assert RuntimeCredentialProvider().probe().state == "ABSENT"


# --------------------------------------------------------------------- #
# redact_text(): token stripped from downstream exception text
# --------------------------------------------------------------------- #


def test_redact_text_strips_embedded_token():
    text = f"GET failed 401 Authorization: Bearer {TOKEN} at https://api.upstox.com"
    assert TOKEN not in redact_text(text, TOKEN)


def test_redact_text_noop_when_token_absent():
    sample = "plain error without any secret"
    assert redact_text(sample, "") == sample
    assert _provider_with(None).redact_text(sample) == sample


def test_redact_text_noop_when_text_has_no_token():
    sample = "plain error without any secret"
    assert redact_text(sample, TOKEN) == sample


# --------------------------------------------------------------------- #
# client: runtime resolution, no network, no CLI args
# --------------------------------------------------------------------- #


def test_client_resolves_token_at_fetch_time_via_provider():
    provider = _NoNetworkProvider()
    client = UpstoxHistoricalDataClient(
        credential_provider=_provider_with(TOKEN),
        provider=provider,
    )
    bars = client.fetch_5m_day(_instrument(), date(2026, 9, 15))
    assert bars == []
    assert provider.get_historical_calls == [date(2026, 9, 15)]


def test_client_fails_closed_when_provider_absent_even_with_injected_provider():
    client = UpstoxHistoricalDataClient(
        credential_provider=_provider_with(None),
        provider=_NoNetworkProvider(),
    )
    with pytest.raises(CredentialsUnavailableError) as exc:
        client.fetch_5m_day(_instrument(), date(2026, 9, 15))
    assert CREDENTIAL_ENV in str(exc.value)
    assert TOKEN not in str(exc.value)


def test_client_redact_error_text_delegates_to_provider():
    client = UpstoxHistoricalDataClient(
        credential_provider=_provider_with(TOKEN),
        provider=_NoNetworkProvider(),
    )
    leaky = f"boom under the hood {TOKEN}"
    assert TOKEN not in client.redact_error_text(leaky)
    assert client.redact_error_text("no secret") == "no secret"


# --------------------------------------------------------------------- #
# collector: token never reaches the manifest error text
# --------------------------------------------------------------------- #


class _LeakyClient(FakeHistoricalDataClient):
    """Fetch failure whose message embeds the token (worst-case downstream leak)."""

    def __init__(self, token: str) -> None:
        super().__init__()
        self.token = token

    def fetch_5m_day(self, instrument, day):
        self.calls.append(day)
        raise RuntimeError(f"upstream relay leaked {self.token} into the message")

    def redact_error_text(self, text: str) -> str:
        return text.replace(self.token, "<token-redacted>")


def test_manifest_error_redacts_embedded_token(tmp_path):
    from datetime import datetime

    leaky = _LeakyClient(TOKEN)
    ctx = make_context(tmp_path, now=datetime(2026, 9, 20, 12, 0), client=leaky)
    outcome = ctx["collector"].collect_once(force_date=date(2026, 9, 15))
    assert outcome.status  # recorded (SOURCE_ERROR here; redaction is what matters)
    row = ctx["manifest"].pool.get("2026-09-15", {})
    assert TOKEN not in row.get("error", "")


# --------------------------------------------------------------------- #
# status CLI: prints only PRESENT / ABSENT, never the value
# --------------------------------------------------------------------- #


def test_status_cli_credential_line_is_presence_only(tmp_path, monkeypatch, capsys):
    from fno_ai_paper_trading.fresh_oos import factory
    from fno_ai_paper_trading.fresh_oos import status as status_module

    ctx = make_context(tmp_path)
    original = status_module.factory.build_collector
    status_module.factory.build_collector = lambda **kw: ctx["collector"]
    try:
        monkeypatch.setenv(CREDENTIAL_ENV, TOKEN)
        code = status_module.main([])
    finally:
        monkeypatch.delenv(CREDENTIAL_ENV, raising=False)
        status_module.factory.build_collector = original
    assert code == 0
    out = capsys.readouterr().out
    assert "Credential" in out
    assert "PRESENT" in out
    assert TOKEN not in out