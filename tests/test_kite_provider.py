"""Tests for the Kite Connect adapter — every HTTP call is mocked.

No network access happens anywhere in this file. The adapter is exercised as a
pure function: responses in, normalized internal models out, plus its error and
retry behaviour.
"""
from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal

import pytest

from fno_ai_paper_trading.data.errors import (
    AuthenticationError,
    InstrumentNotFoundError,
    MarketDataError,
    ProviderConfigurationError,
    RateLimitError,
    UnavailableError,
)
from fno_ai_paper_trading.data.kite_provider import KiteConnectProvider
from fno_ai_paper_trading.models.enums import InstrumentType, MarketPhase
from fno_ai_paper_trading.models.instruments import Instrument
from fno_ai_paper_trading.utils.http import HttpError, HttpResponse

BASE = "https://api.kite.trade"

TOKEN = "408065"
KEY = f"NSE:{TOKEN}"


def _future() -> Instrument:
    return Instrument(
        symbol="NIFTY FUT",
        instrument_type=InstrumentType.FUTURE,
        underlying_symbol="NIFTY",
        exchange="NSE",
        exchange_token=TOKEN,
    )


def _json(status: int, payload: dict, url: str = f"{BASE}/x") -> HttpResponse:
    return HttpResponse(status=status, body=json.dumps(payload).encode(), url=url, headers={})


def _quote_payload() -> dict:
    return {
        "data": {
            KEY: {
                "instrument_token": int(TOKEN),
                "last_price": 24205.0,
                "timestamp": "2026-09-07T09:32:00+05:30",
                "volume": 1200,
                "oi": 2500,
                "ohlc": {"open": 24100.0, "high": 24300.0, "low": 24050.0, "close": 24120.0},
                "depth": {
                    "buy": [{"price": 24200.0, "quantity": 10}],
                    "sell": [{"price": 24210.0, "quantity": 5}],
                },
            }
        }
    }


def _candles_payload() -> dict:
    return {
        "data": {
            "candles": [
                [1767705300, 24100.0, 24300.0, 24050.0, 24205.0, 1200, 2500],
                [1767791700, 24205.0, 24400.0, 24150.0, 24300.0, 1300, 2600],
            ]
        }
    }


def _master_csv() -> str:
    rows = [
        "instrument_token,exchange_token,tradingsymbol,name,last_price,expiry,strike,tick_size,lot_size,instrument_type,segment,exchange",
        "26000,26000,NIFTY 1 Month Fut,NIFTY,24205,,,0.05,75,FUT,NFO,NSE",
        "26002,26002,NIFTY 24500 CE,NIFTY,120,06-12-2026,24500,0.05,75,CE,NFO,NSE",
        "26003,26003,NIFTY 24500 PE,NIFTY,80,06-12-2026,24500,0.05,75,PE,NFO,NSE",
        "6087,6087,RELIANCE,RELIANCE,2800,,,0.05,1,EQ,NSE,NSE",
    ]
    return "\n".join(rows)


def _provider(monkeypatch, responses=None, **kwargs) -> tuple[KiteConnectProvider, list]:
    """Build a provider whose http_get serves queued synthetic responses."""
    responses = list(responses or [])
    calls: list[dict] = []
    last_response: HttpResponse | BaseException | None = None

    def fake_get(url, params=None, headers=None, timeout=None):
        nonlocal last_response
        calls.append({"url": url, "params": params, "headers": headers})
        if responses:
            last_response = responses.pop(0)
        if isinstance(last_response, BaseException):
            raise last_response
        return last_response

    monkeypatch.setattr("fno_ai_paper_trading.data.kite_provider.http_get", fake_get)
    defaults = dict(
        api_key="k",
        access_token="t",
        now_fn=lambda: datetime(2026, 9, 7, 12, 0),
        sleep=lambda _s: None,
    )
    defaults.update(kwargs)
    return KiteConnectProvider(**defaults), calls


# ---------------------------------------------------------------------------
# Quotes / market price
# ---------------------------------------------------------------------------

class TestQuotes:
    def test_get_quote_maps_fields(self, monkeypatch) -> None:
        provider, calls = _provider(monkeypatch, [_json(200, _quote_payload())])
        quote = provider.get_quote(_future())

        assert quote.instrument == _future()
        assert quote.last_price == Decimal("24205")
        assert quote.open == Decimal("24100")
        assert quote.high == Decimal("24300")
        assert quote.low == Decimal("24050")
        assert quote.previous_close == Decimal("24120")
        assert quote.volume == 1200
        assert quote.open_interest == 2500
        assert quote.bid == Decimal("24200")
        assert quote.ask == Decimal("24210")
        assert quote.change == Decimal("85")
        assert quote.change_percent is not None
        assert abs(quote.change_percent - Decimal("0.3524")) < Decimal("0.001")
        assert quote.timestamp.tzinfo is None  # normalized to naive market time

        request = calls[0]
        assert request["url"] == f"{BASE}/quote"
        assert request["params"] == {"i": KEY}
        assert request["headers"]["X-Kite-Version"] == "3"
        assert "token k:t" in request["headers"]["Authorization"]

    def test_get_last_price(self, monkeypatch) -> None:
        payload = {"data": {KEY: {"last_price": 24202.5}}}
        provider, _ = _provider(monkeypatch, [_json(200, payload)])
        assert provider.get_last_price(_future()) == Decimal("24202.5")

    def test_get_market_price_builds_bar_from_quote(self, monkeypatch) -> None:
        provider, _ = _provider(monkeypatch, [_json(200, _quote_payload())])
        bar = provider.get_market_price(_future())
        assert bar.close == Decimal("24205")
        assert bar.high == Decimal("24300")
        assert bar.low == Decimal("24050")
        assert bar.open == Decimal("24100")
        assert bar.volume == 1200
        assert bar.open_interest == 2500

    def test_missing_exchange_token_raises(self, monkeypatch) -> None:
        provider, _ = _provider(monkeypatch)
        untracked = Instrument(
            symbol="X", instrument_type=InstrumentType.FUTURE, underlying_symbol="NIFTY", exchange_token=None
        )
        with pytest.raises(InstrumentNotFoundError):
            provider.get_quote(untracked)
        with pytest.raises(InstrumentNotFoundError):
            provider.get_last_price(untracked)

    def test_missing_credentials_raise_config_error(self, monkeypatch) -> None:
        provider, _ = _provider(monkeypatch, api_key="", access_token="")
        with pytest.raises(ProviderConfigurationError):
            provider.get_last_price(_future())


# ---------------------------------------------------------------------------
# Instruments
# ---------------------------------------------------------------------------

MASTER_TEXT = _master_csv()


class TestInstruments:
    def test_parse_master_filters_non_fo(self, monkeypatch) -> None:
        response = HttpResponse(status=200, body=MASTER_TEXT.encode(), url=f"{BASE}/instruments", headers={})
        provider, calls = _provider(monkeypatch, [response])
        instruments = provider.get_instruments()

        assert len(instruments) == 3
        future = instruments[0]
        assert future.exchange_token == "26000"
        assert future.exchange == "NSE"
        assert future.lot_size == 75
        assert future.is_option() is False

        call = [i for i in instruments if i.symbol == "NIFTY 24500 CE"][0]
        assert call.option_type == "CE"
        assert call.strike == Decimal("24500")
        assert call.expiry.year == 2026

        assert len(calls) == 1  # master is fetched once

    def test_get_instrument_looks_up_symbol(self, monkeypatch) -> None:
        response = HttpResponse(status=200, body=MASTER_TEXT.encode(), url=f"{BASE}/instruments", headers={})
        provider, _ = _provider(monkeypatch, [response])
        hit = provider.get_instrument("NIFTY 24500 PE")
        assert hit is not None and hit.option_type == "PE"
        assert provider.get_instrument("DOES NOT EXIST") is None


# ---------------------------------------------------------------------------
# Historical / OHLCV
# ---------------------------------------------------------------------------

class TestHistorical:
    def test_historical_parses_candles(self, monkeypatch) -> None:
        provider, calls = _provider(monkeypatch, [_json(200, _candles_payload())])
        bars = provider.get_historical_ohlcv(_future(), interval="day")

        assert len(bars) == 2
        assert bars[0].close == Decimal("24205")
        assert bars[0].volume == 1200
        assert bars[0].open_interest == 2500
        assert bars[1].close == Decimal("24300")
        assert bars[0].timestamp < bars[1].timestamp
        assert bars[0].timestamp.tzinfo is None

        request = calls[0]
        assert f"/instruments/historical/{TOKEN}/day" in request["url"]
        assert "from" in request["params"] and "to" in request["params"]
        assert request["params"]["from"] == "2025-09-06"  # now_fn - 366 days

    def test_get_ohlcv_applies_limit(self, monkeypatch) -> None:
        provider, _ = _provider(monkeypatch, [_json(200, _candles_payload())])
        bars = provider.get_ohlcv(_future(), limit=1)
        assert len(bars) == 1
        assert bars[0].close == Decimal("24300")

    def test_unsupported_interval_rejected(self, monkeypatch) -> None:
        provider, calls = _provider(monkeypatch)
        with pytest.raises(MarketDataError, match="interval"):
            provider.get_historical_ohlcv(_future(), interval="month")
        assert calls == []  # no request was made


# ---------------------------------------------------------------------------
# Market session
# ---------------------------------------------------------------------------

class TestMarketSession:
    def test_open_session_from_status(self, monkeypatch) -> None:
        payload = {"data": {"exchange_status": {"NSE": {"trading": True, "exchange": "NSE", "segment": "eq"}}}}
        provider, _ = _provider(monkeypatch, [_json(200, payload)])
        session = provider.get_market_session()
        assert session.is_open is True
        assert session.phase is MarketPhase.OPEN
        assert session.exchange == "NSE"

    def test_closed_session_from_status(self, monkeypatch) -> None:
        payload = {"data": {"exchange_status": {"NSE": {"trading": False, "exchange": "NSE"}}}}
        provider, _ = _provider(monkeypatch, [_json(200, payload)])
        session = provider.get_market_session()
        assert session.is_open is False
        assert session.phase is MarketPhase.CLOSED


# ---------------------------------------------------------------------------
# Errors and retries
# ---------------------------------------------------------------------------

class TestErrorsAndRetries:
    def test_401_raises_authentication_error(self, monkeypatch) -> None:
        provider, calls = _provider(monkeypatch, [HttpError(401, f"{BASE}/quote")])
        with pytest.raises(AuthenticationError):
            provider.get_last_price(_future())
        assert len(calls) == 1  # non-transient: no retry

    def test_404_raises_instrument_not_found(self, monkeypatch) -> None:
        provider, _ = _provider(monkeypatch, [HttpError(404, f"{BASE}/quote")])
        with pytest.raises(InstrumentNotFoundError):
            provider.get_quote(_future())

    def test_400_raises_generic_market_data_error(self, monkeypatch) -> None:
        provider, _ = _provider(monkeypatch, [HttpError(400, f"{BASE}/quote")])
        with pytest.raises(MarketDataError):
            provider.get_last_price(_future())

    def test_429_exhausted_raises_rate_limit_error(self, monkeypatch) -> None:
        provider, calls = _provider(
            monkeypatch,
            [HttpError(429, f"{BASE}/quote")],
            max_retries=2,
            retry_delay=0.01,
        )
        with pytest.raises(RateLimitError):
            provider.get_last_price(_future())
        assert len(calls) == 3  # 1 initial + 2 retries

    def test_503_exhausted_raises_unavailable_error(self, monkeypatch) -> None:
        provider, _ = _provider(monkeypatch, [HttpError(503, f"{BASE}/quote")], max_retries=1, retry_delay=0.01)
        with pytest.raises(UnavailableError):
            provider.get_last_price(_future())

    def test_network_failure_retried_then_unavailable(self, monkeypatch) -> None:
        provider, calls = _provider(
            monkeypatch,
            [ConnectionResetError("reset")],
            max_retries=2,
            retry_delay=0.01,
        )
        with pytest.raises(UnavailableError):
            provider.get_market_session()
        assert len(calls) == 3

    def test_retries_then_succeeds(self, monkeypatch) -> None:
        payload = {"data": {KEY: {"last_price": 24200.0}}}
        provider, calls = _provider(
            monkeypatch,
            [HttpError(429, f"{BASE}/quote"), _json(200, payload)],
            max_retries=3,
            retry_delay=0.01,
        )
        assert provider.get_last_price(_future()) == Decimal("24200")
        assert len(calls) == 2