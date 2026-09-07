"""Tests for the curated research-instrument registry and Upstox key builders.

The registry is the ready-to-use instrument entry point for real-data
research: index instruments with stable Upstox keys (``NSE_INDEX|...``), plus a
generic ``SEGMENT|SYMBOL`` builder shared by the acquisition CLI and the smoke
test.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from fno_ai_paper_trading.data.dataset_store import load_dataset, save_dataset
from fno_ai_paper_trading.data.instrument_registry import (
    RESEARCH_INSTRUMENTS,
    get_research_instrument,
    get_research_instruments,
    instrument_from_upstox_key,
)
from fno_ai_paper_trading.models.enums import InstrumentType
from fno_ai_paper_trading.models.market import MarketPrice


class TestRegistryContents:
    def test_registered_indices(self) -> None:
        assert set(RESEARCH_INSTRUMENTS) == {"NIFTY 50", "BANKNIFTY", "FINNIFTY"}

    def test_all_registry_instruments_are_indexes(self) -> None:
        for inst in get_research_instruments():
            assert inst.instrument_type is InstrumentType.INDEX
            assert inst.exchange == "NSE"
            assert inst.lot_size >= 1

    def test_registry_keys_match_upstox_index_format(self) -> None:
        for inst in get_research_instruments():
            key = inst.exchange_token
            assert key.startswith("NSE_INDEX|")
            assert inst.symbol in key

    def test_names_ordinary_dict_is_source_of_truth(self) -> None:
        assert list(get_research_instruments()) == list(RESEARCH_INSTRUMENTS.values())


class TestLookup:
    def test_case_insensitive_lookup(self) -> None:
        assert get_research_instrument("banknifty").symbol == "Nifty Bank"
        assert get_research_instrument("FinNifty") is get_research_instrument("FINNIFTY")

    def test_unknown_instrument_raises(self) -> None:
        with pytest.raises(KeyError, match="NIFTY MIDCAP"):
            get_research_instrument("NIFTY MIDCAP")

    def test_empty_name_raises(self) -> None:
        with pytest.raises(KeyError):
            get_research_instrument("")


class TestInstrumentFromKey:
    def test_index_segment_maps_to_index_type(self) -> None:
        inst = instrument_from_upstox_key("NSE_INDEX|Nifty 50")
        assert inst.instrument_type is InstrumentType.INDEX
        assert inst.exchange_token == "NSE_INDEX|Nifty 50"
        assert inst.symbol == "Nifty 50"

    def test_non_index_segment_is_nominal_future(self) -> None:
        inst = instrument_from_upstox_key("NSE_FO|NIFTY 27 MAR 2025")
        assert inst.instrument_type is InstrumentType.FUTURE
        assert inst.exchange_token == "NSE_FO|NIFTY 27 MAR 2025"

    def test_key_normalized_uppercase(self) -> None:
        inst = instrument_from_upstox_key("nse_index|Nifty 50")
        assert inst.exchange_token == "NSE_INDEX|Nifty 50"
        assert inst.instrument_type is InstrumentType.INDEX

    def test_malformed_key_raises(self) -> None:
        with pytest.raises(ValueError):
            instrument_from_upstox_key("no-pipe-here")
        with pytest.raises(ValueError):
            instrument_from_upstox_key("")
        with pytest.raises(ValueError):
            instrument_from_upstox_key("|")


class TestIndexDatasetRoundTrip:
    def test_index_instrument_round_trips_through_dataset_store(self, tmp_path: Path) -> None:
        inst = get_research_instrument("NIFTY 50")
        bars = [
            MarketPrice(
                instrument=inst,
                timestamp=datetime(2026, 1, 2, 9, 15),
                open=Decimal("24000"),
                high=Decimal("24100"),
                low=Decimal("23900"),
                close=Decimal("24050"),
                volume=1000,
            )
        ]
        saved = save_dataset(bars, instrument=inst, provider="upstox", interval="1d", directory=tmp_path)
        loaded = load_dataset(saved.path)
        assert loaded.instrument() == inst
        assert loaded.instrument().instrument_type is InstrumentType.INDEX
        assert loaded.instrument().exchange_token == "NSE_INDEX|Nifty 50"