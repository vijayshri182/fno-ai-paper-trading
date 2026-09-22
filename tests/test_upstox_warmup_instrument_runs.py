"""Regression: the Upstox warm-up must always resolve the **registry-backed**
research instrument — never a fallback whose ``exchange_token`` is ``None``.

The phase-1 defect that this file locks down: ``cmd_upstox`` built the
historical provider *without* an instrument list and handed the source the
paper logical symbol ``"NIFTY 50"``. The provider therefore could not resolve
an Upstox key, so ``ProviderBarsSource`` fell back to ``TRACK_INSTRUMENT`` and
produced an instrument whose ``exchange_token is None`` — the warm-up could
never build a valid ``NSE_INDEX|Nifty 50`` request key.

Every property below is asserted **offline** (no access token, no HTTP):
  1. the provider receives the registry-backed research instrument;
  2. its Upstox key is ``NSE_INDEX|Nifty 50`` (not ``None``);
  3. the resolved interval is ``5m``;
  4. no fallback instrument with ``exchange_token=None`` is ever used.
"""
from __future__ import annotations

from fno_ai_paper_trading.data.instrument_registry import (
    get_research_instrument,
)
from fno_ai_paper_trading.data.upstox_provider import UpstoxHistoricalDataProvider
from fno_ai_paper_trading.models.instruments import Instrument

NSE_INDEX_NIFTY_50_KEY = "NSE_INDEX|Nifty 50"


def _research() -> Instrument:
    return get_research_instrument("NIFTY 50")


def _provider(with_research: bool = True) -> UpstoxHistoricalDataProvider:
    instruments = [_research()] if with_research else None
    return UpstoxHistoricalDataProvider(
        access_token="__no_network_used__", interval="5m", instruments=instruments
    )


class TestResearchInstrument:
    def test_registry_instrument_has_real_upstox_key(self) -> None:
        inst = _research()
        assert inst.symbol == "Nifty 50"
        assert inst.exchange_token == NSE_INDEX_NIFTY_50_KEY
        assert inst.exchange_token is not None  # never a None-token fallback

    def test_lookup_is_case_insensitive_and_stable(self) -> None:
        assert get_research_instrument("NIFTY 50") is get_research_instrument("Nifty 50")


class TestProviderWiring:
    def test_provider_receives_registry_instrument(self) -> None:
        research = _research()
        provider = _provider()
        resolved = provider.get_instrument("Nifty 50")
        assert resolved is not None
        assert resolved is research  # the very object from the registry
        assert resolved.exchange_token == NSE_INDEX_NIFTY_50_KEY

    def test_interval_forced_to_five_minutes(self) -> None:
        assert _provider().interval == "5m"

    def test_no_instrument_with_exchange_token_none_reaches_source(self) -> None:
        provider = _provider()
        research = _research()
        # cmd_upstox hands the source `research.symbol`, which resolves to the
        # registry instrument — never the paper-logical fallback.
        resolved = provider.get_instrument(research.symbol)
        assert resolved is research
        assert resolved.exchange_token is not None

    def test_empty_instrument_list_still_raises_not_found(self) -> None:
        provider = _provider(with_research=False)
        assert provider.get_instrument("Nifty 50") is None
