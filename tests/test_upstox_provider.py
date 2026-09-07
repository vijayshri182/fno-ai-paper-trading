"""Tests for the Upstox historical-data adapter — every HTTP call is mocked.

No network access happens anywhere in this file. The adapter is exercised as a
pure function: V3 historical-candle responses in, normalized internal models out,
plus its error and retry behaviour. Coverage targets, in order:

 1. request path construction (base URL, instrument key, unit/interval, dates)
 2. bearer authentication header and missing-token configuration error
 3. interval mapping (canonical + legacy + unknown)
 4. timestamp parsing (ISO +05:30, epoch seconds, epoch millis, naive IST result)
 5. duplicate timestamps rejected
 6. out-of-order candles rejected
 7. invalid OHLC semantics rejected
 8. empty candle payload handled safely
 9. optional open_interest column
10. HTTP error mapping (401/403/404/429/5xx/400-with-UDAPI-code)
11. retry behaviour and injected sleep
12. ProviderConfigurationError for missing credentials
13. normalized MarketPrice values (instrument preserved, naive timestamps)
14. get_ohlcv limit semantics (0 / negative / truncation)
15. quote methods derived read-only from the historical endpoint
16. get_instruments/get_instrument from a supplied local list (or clear error)
17. deterministic market-session state with no network call
18. smoke-test script is opt-in (exits non-zero without credentials)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from fno_ai_paper_trading.data.errors import (
    AuthenticationError,
    InstrumentNotFoundError,
    MarketDataError,
    ProviderConfigurationError,
    RateLimitError,
    UnavailableError,
)
from fno_ai_paper_trading.data.provider import MarketDataProvider
from fno_ai_paper_trading.data.upstox_provider import (
    UPSTOX_BASE_URL,
    UpstoxHistoricalDataProvider,
    upstox_instrument_key,
)
from fno_ai_paper_trading.models.enums import InstrumentType, MarketPhase
from fno_ai_paper_trading.models.instruments import Instrument
from fno_ai_paper_trading.utils.http import HttpError, HttpResponse

BASE = "https://api.upstox.com"
KEY = "NSE_INDEX|Nifty 50"

NOW = datetime(2026, 9, 7, 12, 0)


def _index() -> Instrument:
    return Instrument(
        symbol="Nifty 50",
        instrument_type=InstrumentType.FUTURE,  # nominal type; index OHLCV data
        underlying_symbol="Nifty 50",
        exchange="NSE",
        exchange_token=KEY,
        tick_size=Decimal("0.05"),
        multiplier=1,
        lot_size=1,
    )


def _json(status: int, payload: dict, url: str = f"{BASE}/x") -> HttpResponse:
    return HttpResponse(status=status, body=json.dumps(payload).encode(), url=url, headers={})


def _ok_json(payload: dict, url: str = f"{BASE}/x") -> HttpResponse:
    body = {"status": "success", "data": payload}
    return _json(200, body, url)


def _candles_payload() -> dict:
    return {
        "candles": [
            ["2026-08-06T09:15:00+05:30", 24100.0, 24300.0, 24050.0, 24205.0, 1200, 2500],
            ["2026-08-07T09:15:00+05:30", 24205.0, 24400.0, 24150.0, 24300.0, 1300, 2600],
        ]
    }


def _provider(monkeypatch, responses=None, **kwargs):
    """Build a provider whose http_get serves queued synthetic responses."""
    responses = list(responses or [])
    calls: list[dict] = []
    last_response: HttpResponse | BaseException | None = None

    def fake_get(url, params=None, headers=None, timeout=None):
        nonlocal last_response
        calls.append({"url": url, "params": params, "headers": headers, "timeout": timeout})
        if responses:
            last_response = responses.pop(0)
        if isinstance(last_response, BaseException):
            raise last_response
        return last_response

    monkeypatch.setattr("fno_ai_paper_trading.data.upstox_provider.http_get", fake_get)
    defaults = dict(
        access_token="t",
        base_url=BASE,
        now_fn=lambda: NOW,
        sleep=lambda _s: None,
    )
    defaults.update(kwargs)
    provider = UpstoxHistoricalDataProvider(**defaults)
    return provider, calls


def _http_error(status: int, payload: dict | None = None) -> HttpError:
    body = json.dumps(payload).encode() if payload is not None else b""
    return HttpError(status, f"{BASE}/v3/historical-candle/x", body)


# ---------------------------------------------------------------------------
# Request construction + auth
# ---------------------------------------------------------------------------

class TestRequestShape:
    def test_history_url_uses_v3_path_and_key(self, monkeypatch) -> None:
        provider, calls = _provider(monkeypatch, [_ok_json(_candles_payload())])
        provider.get_historical_ohlcv(
            _index(), interval="5m",
            start=datetime(2026, 8, 1), end=datetime(2026, 8, 8),
        )
        request = calls[0]
        assert f"/v3/historical-candle/{KEY}/minutes/5/2026-08-08/2026-08-01" in request["url"]
        assert request["url"].startswith(BASE)
        assert request["params"] is None  # dates live in the path, not the query string

    def test_day_legacy_interval_maps_to_days_one(self, monkeypatch) -> None:
        provider, calls = _provider(monkeypatch, [_ok_json(_candles_payload())])
        provider.get_historical_ohlcv(_index(), interval="day",
                                      start=datetime(2026, 8, 1), end=datetime(2026, 8, 8))
        assert "/days/1/" in calls[0]["url"]

    def test_default_window_uses_end_minus_default_days(self, monkeypatch) -> None:
        provider, calls = _provider(monkeypatch, [_ok_json(_candles_payload())], default_days=30)
        provider.get_historical_ohlcv(_index(), interval="1d")  # end defaults to now_fn()
        assert calls[0]["url"].endswith("/2026-09-07/2026-08-08")

    def test_bearer_auth_header(self, monkeypatch) -> None:
        provider, calls = _provider(monkeypatch, [_ok_json(_candles_payload())])
        provider.get_historical_ohlcv(_index(), interval="1d")
        assert calls[0]["headers"]["Authorization"] == "Bearer t"
        assert calls[0]["headers"]["Accept"] == "application/json"

    def test_missing_token_raises_config_error(self, monkeypatch) -> None:
        provider, _ = _provider(monkeypatch, access_token="")
        with pytest.raises(ProviderConfigurationError, match="UPSTOX_ACCESS_TOKEN"):
            provider.get_historical_ohlcv(_index(), interval="1d")

    def test_invalid_instrument_key_rejected_before_request(self, monkeypatch) -> None:
        provider, calls = _provider(monkeypatch)
        bad = Instrument(
            symbol="X", instrument_type=InstrumentType.FUTURE,
            underlying_symbol="X", exchange_token="no-pipe-here",
        )
        with pytest.raises(MarketDataError, match="instrument key"):
            provider.get_historical_ohlcv(bad, interval="1d")
        assert calls == []

    def test_missing_instrument_token_raises_not_found(self, monkeypatch) -> None:
        provider, calls = _provider(monkeypatch)
        untracked = Instrument(
            symbol="X", instrument_type=InstrumentType.FUTURE, underlying_symbol="X",
            exchange_token=None,
        )
        with pytest.raises(InstrumentNotFoundError, match="instrument key"):
            provider.get_historical_ohlcv(untracked, interval="1d")
        assert calls == []

    def test_invalid_date_order_rejected(self, monkeypatch) -> None:
        provider, calls = _provider(monkeypatch)
        with pytest.raises(MarketDataError, match="start"):
            provider.get_historical_ohlcv(
                _index(), interval="1d",
                start=datetime(2026, 8, 8), end=datetime(2026, 8, 1),
            )
        assert calls == []

    def test_unknown_interval_rejected(self, monkeypatch) -> None:
        provider, calls = _provider(monkeypatch)
        with pytest.raises(MarketDataError, match="interval"):
            provider.get_historical_ohlcv(_index(), interval="2d")
        assert calls == []


# ---------------------------------------------------------------------------
# Candle normalization
# ---------------------------------------------------------------------------

class TestNormalization:
    def test_candles_normalized_to_market_price(self, monkeypatch) -> None:
        provider, calls = _provider(monkeypatch, [_ok_json(_candles_payload())])
        bars = provider.get_historical_ohlcv(_index(), interval="5m")
        assert len(bars) == 2
        bar = bars[0]
        assert bar.instrument == _index()
        assert bar.open == Decimal("24100")
        assert bar.high == Decimal("24300")
        assert bar.low == Decimal("24050")
        assert bar.close == Decimal("24205")
        assert bar.volume == 1200
        assert bar.open_interest == 2500
        assert bar.timestamp == datetime(2026, 8, 6, 9, 15)
        assert bar.timestamp.tzinfo is None
        assert bars[0].timestamp < bars[1].timestamp

    def test_iso_with_z_suffix_parsed(self, monkeypatch) -> None:
        payload = {"candles": [["2026-08-06T04:00:00Z", 100.0, 105.0, 99.0, 102.0, 10, None],
                               ["2026-08-07T04:00:00Z", 102.0, 108.0, 101.0, 106.0, 11, None]]}
        provider, _ = _provider(monkeypatch, [_ok_json(payload)])
        bars = provider.get_historical_ohlcv(_index(), interval="1d")
        # 04:00 UTC == 09:30 IST.
        assert bars[0].timestamp == datetime(2026, 8, 6, 9, 30)
        assert bars[0].timestamp.tzinfo is None

    def test_epoch_seconds_and_millis_parsed(self) -> None:
        from fno_ai_paper_trading.data.upstox_provider import UpstoxHistoricalDataProvider

        seconds = "1754451900"  # 2025-08-06 09:15 IST (epoch 1754451900)
        millis = 1754451900000
        assert UpstoxHistoricalDataProvider._parse_timestamp(seconds) == (
            UpstoxHistoricalDataProvider._parse_timestamp(millis)
        )
        parsed = UpstoxHistoricalDataProvider._parse_timestamp(seconds)
        assert parsed is not None
        assert parsed.tzinfo is None
        assert parsed.isoformat().startswith("2025-08-06T09:")

    def test_malformed_timestamp_rejected(self, monkeypatch) -> None:
        payload = {"candles": [["not-a-timestamp", 1.0, 2.0, 1.0, 1.5, 10, None]]}
        provider, _ = _provider(monkeypatch, [_ok_json(payload)])
        with pytest.raises(MarketDataError, match="timestamp"):
            provider.get_historical_ohlcv(_index(), interval="1d")

    def test_duplicate_timestamps_rejected(self, monkeypatch) -> None:
        payload = {
            "candles": [
                ["2026-08-06T09:15:00+05:30", 100.0, 105.0, 99.0, 102.0, 10, None],
                ["2026-08-06T09:15:00+05:30", 102.0, 108.0, 101.0, 106.0, 11, None],
            ]
        }
        provider, _ = _provider(monkeypatch, [_ok_json(payload)])
        with pytest.raises(MarketDataError, match="duplicate"):
            provider.get_historical_ohlcv(_index(), interval="1d")

    def test_out_of_order_candles_rejected(self, monkeypatch) -> None:
        payload = {
            "candles": [
                ["2026-08-07T09:15:00+05:30", 102.0, 108.0, 101.0, 106.0, 11, None],
                ["2026-08-06T09:15:00+05:30", 100.0, 105.0, 99.0, 102.0, 10, None],
            ]
        }
        provider, _ = _provider(monkeypatch, [_ok_json(payload)])
        with pytest.raises(MarketDataError, match="out of order"):
            provider.get_historical_ohlcv(_index(), interval="1d")

    def test_invalid_ohlc_semantics_rejected(self, monkeypatch) -> None:
        payload = {"candles": [["2026-08-06T09:15:00+05:30", 100.0, 99.0, 98.0, 102.0, 10, None]]}
        provider, _ = _provider(monkeypatch, [_ok_json(payload)])
        with pytest.raises(MarketDataError, match="OHLC"):
            provider.get_historical_ohlcv(_index(), interval="1d")

    def test_non_numeric_price_rejected(self, monkeypatch) -> None:
        payload = {"candles": [["2026-08-06T09:15:00+05:30", "abc", 2.0, 1.0, 1.5, 10, None]]}
        provider, _ = _provider(monkeypatch, [_ok_json(payload)])
        with pytest.raises(MarketDataError, match="non-numeric"):
            provider.get_historical_ohlcv(_index(), interval="1d")

    def test_empty_candles_return_empty_list(self, monkeypatch) -> None:
        provider, calls = _provider(monkeypatch, [_ok_json({"candles": []})])
        assert provider.get_historical_ohlcv(_index(), interval="1d") == []
        assert len(calls) == 1

    def test_candle_without_open_interest(self, monkeypatch) -> None:
        payload = {
            "candles": [
                ["2026-08-06T09:15:00+05:30", 24100.0, 24300.0, 24050.0, 24205.0, 1200],
                ["2026-08-07T09:15:00+05:30", 24205.0, 24400.0, 24150.0, 24300.0, 1300],
            ]
        }
        provider, _ = _provider(monkeypatch, [_ok_json(payload)])
        bars = provider.get_historical_ohlcv(_index(), interval="5m")
        assert bars[0].open_interest is None

    def test_malformed_candle_row_rejected(self, monkeypatch) -> None:
        payload = {"candles": [["2026-08-06T09:15:00+05:30", 1.0, 2.0]]}
        provider, _ = _provider(monkeypatch, [_ok_json(payload)])
        with pytest.raises(MarketDataError, match="candle"):
            provider.get_historical_ohlcv(_index(), interval="1d")

    def test_unsuccessful_payload_rejected(self, monkeypatch) -> None:
        provider, _ = _provider(monkeypatch, [_json(200, {"status": "error", "errors": []})])
        with pytest.raises(MarketDataError, match="unsuccessful"):
            provider.get_historical_ohlcv(_index(), interval="1d")


# ---------------------------------------------------------------------------
# Interface, windowed quotes and instruments
# ---------------------------------------------------------------------------

class TestInterfaceAndWindows:
    def test_satisfies_market_data_provider_abc(self) -> None:
        provider = UpstoxHistoricalDataProvider(access_token="t")
        assert isinstance(provider, MarketDataProvider)
        assert not provider.__class__.__abstractmethods__

    def test_get_ohlcv_truncates_to_limit(self, monkeypatch) -> None:
        provider, _ = _provider(monkeypatch, [_ok_json(_candles_payload())])
        bars = provider.get_ohlcv(_index(), limit=1)
        assert len(bars) == 1
        assert bars[0].close == Decimal("24300")

    def test_get_ohlcv_zero_and_negative_limit(self, monkeypatch) -> None:
        provider, _ = _provider(monkeypatch, [_ok_json(_candles_payload())])
        assert provider.get_ohlcv(_index(), limit=0) == []
        with pytest.raises(ValueError):
            provider.get_ohlcv(_index(), limit=-1)

    def test_get_ohlcv_uses_default_interval(self, monkeypatch) -> None:
        provider, calls = _provider(monkeypatch, [_ok_json(_candles_payload())])
        provider.get_ohlcv(_index())
        assert "/days/1/" in calls[0]["url"]

    def test_quote_methods_derive_from_history_read_only(self, monkeypatch) -> None:
        provider, calls = _provider(monkeypatch, [_ok_json(_candles_payload())])
        bar = provider.get_market_price(_index())
        last = provider.get_last_price(_index())
        quote = provider.get_quote(_index())
        assert bar.close == last == quote.last_price == Decimal("24300")
        assert len(calls) == 3  # one historical request each, no other endpoints
        assert all("/v3/historical-candle/" in call["url"] for call in calls)

    def test_get_market_price_no_data_raises(self, monkeypatch) -> None:
        provider, _ = _provider(monkeypatch, [_ok_json({"candles": []})])
        with pytest.raises(MarketDataError, match="no market data"):
            provider.get_market_price(_index())

    def test_instruments_from_supplied_list(self, monkeypatch) -> None:
        provider, _ = _provider(monkeypatch, instruments=[_index()])
        assert provider.get_instruments() == [_index()]
        assert provider.get_instrument("Nifty 50") == _index()
        assert provider.get_instrument("Nope") is None

    def test_instruments_without_list_raise_clear_error(self, monkeypatch) -> None:
        provider, _ = _provider(monkeypatch)
        with pytest.raises(MarketDataError, match="local instrument list"):
            provider.get_instruments()

    def test_market_session_is_deterministic_and_offline(self, monkeypatch) -> None:
        provider, calls = _provider(monkeypatch)
        session = provider.get_market_session()
        assert session.exchange == "NSE"
        assert session.phase in (MarketPhase.OPEN, MarketPhase.CLOSED)
        assert calls == []  # local session state, no network call


# ---------------------------------------------------------------------------
# Errors and retries
# ---------------------------------------------------------------------------

class TestErrorsAndRetries:
    def test_401_raises_authentication_error(self, monkeypatch) -> None:
        provider, calls = _provider(monkeypatch, [_http_error(401)])
        with pytest.raises(AuthenticationError):
            provider.get_historical_ohlcv(_index(), interval="1d")
        assert len(calls) == 1

    def test_403_raises_authentication_error(self, monkeypatch) -> None:
        provider, _ = _provider(monkeypatch, [_http_error(403)])
        with pytest.raises(AuthenticationError):
            provider.get_historical_ohlcv(_index(), interval="1d")

    def test_404_raises_instrument_not_found(self, monkeypatch) -> None:
        provider, _ = _provider(monkeypatch, [_http_error(404)])
        with pytest.raises(InstrumentNotFoundError):
            provider.get_historical_ohlcv(_index(), interval="1d")

    def test_400_with_instrument_error_code_raises_not_found(self, monkeypatch) -> None:
        body = {"status": "error", "errors": [{"code": "UDAPI100011", "message": "invalid key"}]}
        provider, calls = _provider(monkeypatch, [_http_error(400, body)])
        with pytest.raises(InstrumentNotFoundError, match="invalid key"):
            provider.get_historical_ohlcv(_index(), interval="1d")
        assert len(calls) == 1  # non-transient 4xx: no retry

    def test_400_with_interval_error_code_raises_market_data_error(self, monkeypatch) -> None:
        body = {"status": "error", "errors": [{"code": "UDAPI1147", "message": "bad interval"}]}
        provider, _ = _provider(monkeypatch, [_http_error(400, body)])
        with pytest.raises(MarketDataError, match="interval"):
            provider.get_historical_ohlcv(_index(), interval="1d")

    def test_400_with_dates_error_code_raises_market_data_error(self, monkeypatch) -> None:
        body = {"status": "error", "errors": [{"code": "UDAPI1022", "message": "to_date required"}]}
        provider, _ = _provider(monkeypatch, [_http_error(400, body)])
        with pytest.raises(MarketDataError, match="date range"):
            provider.get_historical_ohlcv(_index(), interval="1d")

    def test_400_unmapped_raises_market_data_error(self, monkeypatch) -> None:
        provider, _ = _provider(monkeypatch, [_http_error(400)])
        with pytest.raises(MarketDataError, match="unexpected HTTP"):
            provider.get_historical_ohlcv(_index(), interval="1d")

    def test_429_exhausted_raises_rate_limit_error(self, monkeypatch) -> None:
        provider, calls = _provider(
            monkeypatch, [_http_error(429)], max_retries=2, retry_delay=0.01,
        )
        with pytest.raises(RateLimitError):
            provider.get_historical_ohlcv(_index(), interval="1d")
        assert len(calls) == 3  # 1 initial + 2 retries

    def test_503_exhausted_raises_unavailable_error(self, monkeypatch) -> None:
        provider, _ = _provider(monkeypatch, [_http_error(503)], max_retries=1, retry_delay=0.01)
        with pytest.raises(UnavailableError):
            provider.get_historical_ohlcv(_index(), interval="1d")

    def test_network_failure_retried_then_unavailable(self, monkeypatch) -> None:
        provider, calls = _provider(
            monkeypatch, [ConnectionResetError("reset")], max_retries=2, retry_delay=0.01,
        )
        with pytest.raises(UnavailableError):
            provider.get_historical_ohlcv(_index(), interval="1d")
        assert len(calls) == 3

    def test_retries_then_succeeds(self, monkeypatch) -> None:
        provider, calls = _provider(
            monkeypatch,
            [_http_error(429), _ok_json(_candles_payload())],
            max_retries=3,
            retry_delay=0.01,
        )
        bars = provider.get_historical_ohlcv(_index(), interval="1d")
        assert len(bars) == 2
        assert len(calls) == 2

    def test_sleep_is_injected(self, monkeypatch) -> None:
        slept: list[float] = []

        def fake_sleep(seconds: float) -> None:
            slept.append(seconds)

        provider, _ = _provider(
            monkeypatch, [_http_error(429), _ok_json(_candles_payload())],
            max_retries=2, retry_delay=0.1, sleep=fake_sleep,
        )
        provider.get_historical_ohlcv(_index(), interval="1d")
        assert slept[0] == pytest.approx(0.1)


# ---------------------------------------------------------------------------
# Instrument key helper
# ---------------------------------------------------------------------------

class TestInstrumentKeyHelper:
    def test_key_builder(self) -> None:
        assert upstox_instrument_key("nse_fo", "NIFTY 27 MAR 2025") == "NSE_FO|NIFTY 27 MAR 2025"
        assert upstox_instrument_key("NSE_INDEX", "Nifty 50") == "NSE_INDEX|Nifty 50"

    def test_key_builder_rejects_empty(self) -> None:
        with pytest.raises(ValueError):
            upstox_instrument_key("", "NIFTY")
        with pytest.raises(ValueError):
            upstox_instrument_key("NSE_FO", "")

    def test_upstox_base_url_constant(self) -> None:
        assert UPSTOX_BASE_URL == "https://api.upstox.com"


# ---------------------------------------------------------------------------
# Smoke-test script is opt-in
# ---------------------------------------------------------------------------

class TestSmokeScriptOptIn:
    def test_smoke_script_exits_nonzero_without_token(self, tmp_path) -> None:
        script = Path(__file__).resolve().parents[1] / "scripts" / "upstox_smoke_test.py"
        env = dict(os.environ)
        env["UPSTOX_ACCESS_TOKEN"] = ""
        env.pop("UPSTOX_TEST_INSTRUMENT_KEY", None)
        result = subprocess.run(
            [sys.executable, str(script), "--no-save", "--days", "1"],
            cwd=str(tmp_path), env=env, capture_output=True, text=True, timeout=60,
        )
        assert result.returncode == 2
        assert "UPSTOX_ACCESS_TOKEN" in (result.stderr or result.stdout)