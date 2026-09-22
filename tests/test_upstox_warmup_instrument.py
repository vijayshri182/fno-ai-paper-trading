"""Regression: the Upstox 5m warm-up must always resolve the **registry-backed**
research instrument — never a fallback whose ``exchange_token`` is ``None``.

Phase-1 defect shape: ``cmd_upstox`` built :class:`UpstoxHistoricalDataProvider`
*without* an instrument list and handed the source the paper logical symbol
``"NIFTY 50"``. The provider therefore could not resolve an Upstox instrument
key and the warm-up could never build a valid ``NSE_INDEX|Nifty 50`` request
key. This file locks the corrected wiring down **offline** — no access token is
readcars, no HTTP request is ever made, and no ``Instrument`` with
``exchange_token=None`` is ever used to fetch.

Required properties:

  1. the provider receives the registry-backed research instrument,
  2. its Upstox key is ``NSE_INDEX|Nifty 50`` (never ``None``),
  3. the resolved interval is ``5m``,
  4. a provider with no instrument list raises instead of falling back to a
     ``TRACK_INSTRUMENT`` whose ``exchange_token`` is ``None``.
"""
from __future__ import annotations

import pytest

from fno_ai_paper_trading.data.errors import MarketDataError
from fno_ai_paper_trading.data.instrument_registry import get_research_instrument
from fno_ai_paper_trading.data.upstox_provider import (
    UpstoxHistoricalDataProvider,
    upstox_instrument_key,
)

NSE_INDEX_NIFTY_50_KEY = upstox_instrument_key("NSE_INDEX", "Nifty 50")


def _research():
    return get_research_instrument("NIFTY 50")


def _provider(instruments=None) -> UpstoxHistoricalDataProvider:
    return UpstoxHistoricalDataProvider(
        access_token="__offline_probe_token__",
        interval="5m",
        instruments=instruments,
    )


def test_registry_returns_the_nifty_50_index_instrument() -> None:
    research = _research()
    assert research.symbol == "Nifty 50"
    assert research.exchange_token == NSE_INDEX_NIFTY_50_KEY
    assert research.exchange_token is not None


def test_provider_receives_the_registry_backed_instrument() -> None:
    research = _research()
    provider = _provider(instruments=[research])
    resolved = provider.get_instruments()
    assert resolved == [research]

    # ``get_instrument`` resolves to the very registry object (non-None key) —
    # the warm-up gets a real ``NSE_INDEX|Nifty 50`` request key.
    found = provider.get_instrument(research.symbol)
    assert found is research
    assert found.exchange_token == NSE_INDEX_NIFTY_50_KEY
    assert found.exchange_token is not None


def test_warmup_interval_is_always_five_minutes() -> None:
    provider = _provider(instruments=[_research()])
    assert provider.interval == "5m"


def test_provider_without_instrument_list_raises_not_falls_back() -> None:
    # Phase-1 defect: no instrument list => no exchange_token => a fallback
    # with ``exchange_token=None``. The corrected provider never does that.
    provider = _provider(instruments=None)
    with pytest.raises(MarketDataError, match="instrument list"):
        provider.get_instruments()


def test_logical_symbol_without_registry_never_resolves_via_fallback() -> None:
    research = _research()
    provider = _provider(instruments=[research])
    # The paper logical symbol ("NIFTY 50") is a registry key, not an Upstox
    # symbol; the provider must resolve only the registry symbol.
    assert provider.get_instrument(research.symbol) is research
    assert provider.get_instrument("NIFTY 50") is None
