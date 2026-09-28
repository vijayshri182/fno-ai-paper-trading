"""Hermetic offline tests for WS 7.24B Stage A live-buy preparation.

Rules enforced here (no network, no orders, no tokens):

* ``place_order``/``cancel_order`` are **never** invoked on the adapter spy;
* the access token is never printed or persisted (only its sha256 fingerprint
  lands in the consent file under the existing schema field);
* the authoritative gate is used and its decision is authoritative (bug/never
  weakened); fail-closed when the gate is closed;
* the instrument contract and lot size come from the **injected master rows**
  (dynamic — the test never hard-codes 65, expiry, strike or instrument key);
* a non-FLAT position aborts with ``FAIL_CLOSED`` and no payload.
"""
from __future__ import annotations

import gzip
import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from fno_ai_paper_trading.config.settings import LiveExecutionTestSettings
from fno_ai_paper_trading.execution.gate import FINGERPRINT_FIELD, sha256_hex
from fno_ai_paper_trading.execution.live_prep import (
    FAIL_CLOSED,
    READY,
    PrepIO,
    prepare_live_buy,
)
from fno_ai_paper_trading.execution.oauth import UpstoxOAuthConfig

FAKE_CLIENT_ID = "dummy-app-id"
FAKE_CLIENT_SECRET = "dummy-client-secret"
FAKE_TOKEN = "s3cr3t-access-token-for-testing-only"

#: A fixed, deterministic reference "today" for the resolver.
REF_DATE = date(2026, 9, 22)
REF_NOW = datetime(2026, 9, 22, 9, 30, 0)


# ------------------------------------------------------------------ helpers
class _AdapterSpy:
    """Read-only fake adapter; asserts no order-write reachable.

    Guards: any call to ``place_order``/``cancel_order`` raises immediately.
    """

    def __init__(self, *, flat: bool = True, spot: str = "24500", premium: str = "120") -> None:
        self._flat = flat
        self._spot = spot
        self._premium = premium
        self.quote_calls: list[str] = []
        self.order_writes: list[str] = []

    def get_account_id(self) -> str:
        return "test-account-123"

    def quote(self, symbol: str) -> Decimal:
        self.quote_calls.append(symbol)
        if "NSE_INDEX|Nifty 50" in symbol:
            return Decimal(self._spot)
        return Decimal(self._premium)

    def get_positions(self):
        return []

    def is_flat(self, symbol: str) -> bool:
        return self._flat

    def place_order(self, *args, **kwargs):  # pragma: no cover - must never fire
        self.order_writes.append("place_order")
        raise AssertionError("STAGE A MUST NEVER PLACE AN ORDER")

    def cancel_order(self, *args, **kwargs):  # pragma: no cover - must never fire
        self.order_writes.append("cancel_order")
        raise AssertionError("STAGE A MUST NEVER CANCEL AN ORDER")


def _contract_row(
    *,
    underlying: str = "NIFTY",
    strike: str = "24500",
    oe: str = "CE",
    day: int = 24,
    mon: str = "SEP",
    yy: str = "26",
    lot: int = 75,
    token: int = 51418,
) -> dict:
    return {
        "segment": "NSE_FO",
        "instrument_type": oe,
        "trading_symbol": f"{underlying} {strike} {oe} {day} {mon} {yy}",
        "instrument_key": f"NSE_FO|{token}",
        "exchange_token": str(token),
        "lot_size": lot,
        "tick_size": "0.05",
    }


def _rows() -> list[dict]:
    return [
        _contract_row(strike="24450", token=51411, lot=75),
        _contract_row(strike="24500", token=51418, lot=75),
        _contract_row(strike="24550", token=51425, lot=75),
        _contract_row(strike="24500", oe="PE", token=51431, lot=75),
    ]


def _write_master(directory: Path) -> Path:
    master = directory / "NSE.json.gz"
    raw = json.dumps(_rows()).encode("utf-8")
    master.write_bytes(gzip.compress(raw))
    return master


def _settings(tmp_path: Path) -> LiveExecutionTestSettings:
    return LiveExecutionTestSettings(
        enabled=True,
        consent_file=str(tmp_path / "operator_consent.json"),
        hold_seconds=300,
        expiry_hours=24.0,
        dry_run=True,
    )


def _prep(tmp_path: Path, **overrides) -> LiveExecutionTestSettings:
    settings = overrides.pop("settings", None) or _settings(tmp_path)
    master_file = _write_master(tmp_path)
    base = dict(
        settings=settings,
        oauth_config=UpstoxOAuthConfig(
            client_id=FAKE_CLIENT_ID, client_secret=FAKE_CLIENT_SECRET
        ),
        consent_path=tmp_path / "operator_consent.json",
        operator="TestOperator",
        purpose="WS 7.24B hermetic prep test",
        master_file=master_file,
        now_fn=lambda: REF_NOW,
        oauth_exchange=lambda _cfg, _code: FAKE_TOKEN,
        adapter_factory=lambda: _AdapterSpy(),
    )
    base.update(overrides)
    io = PrepIO(**base)
    return settings, master_file, io


# ------------------------------------------------------------------ tests
def test_ready_builds_one_lot_market_buy_payload(tmp_path):
    settings, master_file, io = _prep(tmp_path)
    result = prepare_live_buy(io)

    assert result.status == READY
    assert result.ready is True
    assert result.stage == "prepared"
    assert result.operator == "TestOperator"

    contract = result.contract
    assert contract["underlying"] == "NIFTY"
    assert contract["option_type"] == "CE"
    assert contract["expiry"].startswith("2026-09-24")  # from master row, dynamic
    assert contract["strike"] == "24500"  # ATM from injected spot (24500)
    assert contract["instrument_key"] == "NSE_FO|51418"
    # Lot size comes from the master — dynamic, NOT hard-coded 65.
    assert contract["lot_size"] == 75

    payload = result.payload
    assert payload is not None
    assert payload["transaction_type"] == "BUY"
    assert payload["order_type"] == "MARKET"
    assert payload["product"] == "I"
    assert payload["validity"] == "DAY"
    assert payload["quantity"] == 75  # == current lot size (one lot, market)
    assert payload["instrument_token"] == 51418
    assert payload["is_amo"] is False
    assert payload["trigger_price"] == 0


def test_gate_is_authoritative_and_fail_closed_on_closed_gate(tmp_path):
    settings = _settings(tmp_path)
    settings = LiveExecutionTestSettings(
        enabled=False,  # master flag off => gate remains closed
        consent_file=settings.consent_file,
        expiry_hours=24.0,
    )
    _, _master, io = _prep(tmp_path, settings=settings)

    result = prepare_live_buy(io)

    assert result.status == FAIL_CLOSED
    assert result.stage == "gate"
    assert result.payload is None
    assert result.ready is False


def test_fail_closed_when_no_operator_authorized_and_no_consent_file(tmp_path):
    # No --operator and no existing consent file => no one authorized the run;
    # Stage A must fail closed rather than fabricate an operator.
    settings, _master_file, io = _prep(tmp_path, operator="", purpose="")
    if (tmp_path / "operator_consent.json").exists():
        (tmp_path / "operator_consent.json").unlink()

    result = prepare_live_buy(io)

    assert result.status == FAIL_CLOSED
    assert result.stage == "consent"
    assert result.payload is None


def test_consent_refresh_preserves_schema(tmp_path):
    from fno_ai_paper_trading.execution.live_prep import load_consent

    _, _master, io = _prep(tmp_path)
    result = prepare_live_buy(io)
    assert result.status == READY

    record = load_consent(tmp_path / "operator_consent.json")
    assert record is not None
    # Existing schema field names are preserved exactly.
    for key in ("operator", "purpose", "created_at", "expires_at", FINGERPRINT_FIELD):
        assert key in record
    assert record["operator"] == "TestOperator"
    # Only the sha256 fingerprint is stored — never the raw token.
    assert record[FINGERPRINT_FIELD] == sha256_hex(FAKE_TOKEN)
    assert FAKE_TOKEN not in json.dumps(record)
    assert record["created_at"] <= record["expires_at"]


def test_fingerprint_never_leaks_token_and_result_is_secret_free(tmp_path):
    _, _master, io = _prep(tmp_path)
    result = prepare_live_buy(io)

    blob = json.dumps(result.printable)
    assert FAKE_TOKEN not in blob
    assert FAKE_CLIENT_SECRET not in blob
    assert result.payload is not None  # payload preview exists and is safe
    assert FAKE_TOKEN not in json.dumps(result.payload)


def test_non_flat_position_aborts_before_any_payload(tmp_path):
    _, _master, io = _prep(tmp_path, adapter_factory=lambda: _AdapterSpy(flat=False))

    result = prepare_live_buy(io)

    assert result.status == FAIL_CLOSED
    assert result.stage == "position"
    assert result.payload is None
    assert result.ready is False


def test_place_order_never_called_on_ready_path(tmp_path):
    spy = _AdapterSpy()
    settings, _master_file, io = _prep(tmp_path, adapter_factory=lambda: spy)

    result = prepare_live_buy(io)
    assert result.status == READY
    assert spy.order_writes == []  # no write surface was reached


def test_stage_a_adapter_uses_fresh_oauth_credential_not_stale_env(tmp_path, monkeypatch):
    import fno_ai_paper_trading.execution.live_prep as live_prep_module

    monkeypatch.setenv("UPSTOX_ACCESS_TOKEN", "stale-env-token-that-must-not-win")
    captured = {}

    class _RecordingAdapter(_AdapterSpy):
        def __init__(self, credentials, *, dry_run):  # pragma: no cover - spy ctor
            captured["credentials"] = credentials
            captured["dry_run"] = dry_run
            super().__init__()

    class _FakeCredentials:
        @classmethod
        def from_env(cls, *, access_token=None):
            captured["from_env_access_token"] = access_token
            return "runtime-credential-sentinel"

    monkeypatch.setattr(live_prep_module, "UpstoxExecutionAdapter", _RecordingAdapter)
    monkeypatch.setattr(live_prep_module, "UpstoxCredentials", _FakeCredentials)

    io = PrepIO(
        settings=_settings(tmp_path),
        oauth_config=UpstoxOAuthConfig(
            client_id=FAKE_CLIENT_ID, client_secret=FAKE_CLIENT_SECRET
        ),
        consent_path=tmp_path / "operator_consent.json",
        operator="TestOperator",
        purpose="WS 7.24B hermetic prep test",
        master_file=_write_master(tmp_path),
        now_fn=lambda: REF_NOW,
        oauth_exchange=lambda _cfg, _code: FAKE_TOKEN,
        adapter_factory=None,  # exercise the default real-adapter construction seam
    )
    result = prepare_live_buy(io)

    assert result.status == READY
    # The adapter is built from the freshly exchanged OAuth token — the same
    # credential the consent fingerprint and the gate validated — never the
    # stale .env UPSTOX_ACCESS_TOKEN.
    assert captured["from_env_access_token"] == FAKE_TOKEN
    assert captured["credentials"] == "runtime-credential-sentinel"
    assert captured["dry_run"] is True
    assert FAKE_TOKEN not in json.dumps(result.printable)


def test_closed_gate_never_reaches_adapter_or_credential_construction(tmp_path, monkeypatch):
    import fno_ai_paper_trading.execution.live_prep as live_prep_module

    calls: list[str] = []

    monkeypatch.setattr(
        live_prep_module,
        "UpstoxExecutionAdapter",
        lambda *args, **kwargs: calls.append("adapter") or _AdapterSpy(),
    )

    class _FakeCredentials:
        @classmethod
        def from_env(cls, *, access_token=None):
            calls.append("from_env")
            return "runtime-credential-sentinel"

    monkeypatch.setattr(live_prep_module, "UpstoxCredentials", _FakeCredentials)

    settings = LiveExecutionTestSettings(
        enabled=False,  # master flag off => gate closed; tokens cannot open it
        consent_file=str(tmp_path / "operator_consent.json"),
        expiry_hours=24.0,
    )
    io = PrepIO(
        settings=settings,
        oauth_config=UpstoxOAuthConfig(
            client_id=FAKE_CLIENT_ID, client_secret=FAKE_CLIENT_SECRET
        ),
        consent_path=tmp_path / "operator_consent.json",
        operator="TestOperator",
        purpose="hermetic gate guard",
        master_file=_write_master(tmp_path),
        now_fn=lambda: REF_NOW,
        oauth_exchange=lambda _cfg, _code: FAKE_TOKEN,
        adapter_factory=None,
    )

    result = prepare_live_buy(io)

    assert result.status == FAIL_CLOSED
    assert result.stage == "gate"
    assert result.payload is None
    # Even with a live credential in hand, a closed gate short-circuits before
    # any adapter construction — the runtime token cannot bypass the gate.
    assert calls == []


def test_fail_closed_when_oauth_not_configured(tmp_path):
    io = PrepIO(
        settings=_settings(tmp_path),
        oauth_config=UpstoxOAuthConfig(client_id="", client_secret=""),
        consent_path=tmp_path / "operator_consent.json",
        master_file=_write_master(tmp_path),
        now_fn=lambda: REF_NOW,
        oauth_exchange=lambda _cfg, _code: FAKE_TOKEN,
        adapter_factory=lambda: _AdapterSpy(),
    )
    result = prepare_live_buy(io)

    assert result.status == FAIL_CLOSED
    assert result.stage == "oauth"
    assert result.payload is None


def test_google_style_console_output_marks_stop_before_buy(tmp_path):
    _, _master_file, io = _prep(tmp_path)
    result = prepare_live_buy(io)
    text = result.to_console()
    assert "READY_FOR_EXPLICIT_BUY_APPROVAL" in text
    assert "lot=75" in text
    assert "NSE_FO|51418" in text