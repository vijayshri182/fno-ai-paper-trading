"""Tests for the local dataset store (CSV + JSON metadata manifest)."""
from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from fno_ai_paper_trading.data.dataset_store import (
    StoredDataset,
    dataset_hash,
    load_dataset,
    save_dataset,
)
from fno_ai_paper_trading.data.mock_provider import build_sample_ohlcv, build_sample_instruments
from fno_ai_paper_trading.models.enums import InstrumentType
from fno_ai_paper_trading.models.instruments import Instrument
from fno_ai_paper_trading.models.market import MarketPrice


def _future() -> Instrument:
    return build_sample_instruments()[0]


def _bars() -> list[MarketPrice]:
    return build_sample_ohlcv(_future(), bars=5)


class TestSaveAndRoundtrip:
    def test_save_writes_csv_and_meta(self, tmp_path: Path) -> None:
        bars = _bars()
        saved = save_dataset(
            bars, instrument=_future(), provider="upstox", interval="1d", directory=tmp_path,
        )
        assert isinstance(saved, StoredDataset)
        assert saved.path.name.endswith(".csv")
        assert saved.path.exists()
        meta = saved.path.with_name(saved.path.stem + ".meta.json")
        assert meta.exists()

        content = saved.path.read_text(encoding="utf-8")
        assert content.splitlines()[0] == "timestamp,open,high,low,close,volume,open_interest"
        assert len(content.splitlines()) == 6  # header + 5 bars

    def test_roundtrip_preserves_bars_and_instrument(self, tmp_path: Path) -> None:
        saved = save_dataset(_bars(), instrument=_future(), provider="upstox", interval="1d", directory=tmp_path)
        loaded = load_dataset(saved.path)
        assert loaded.bars == _bars()
        assert loaded.instrument() == _future()
        assert loaded.metadata["num_bars"] == 5
        assert loaded.metadata["provider"] == "upstox"
        assert loaded.metadata["interval"] == "1d"
        assert loaded.metadata["data_hash"] == saved.data_hash
        assert loaded.metadata["start_date"] == "2026-09-01"
        assert loaded.metadata["schema_version"] == "1"

    def test_meta_is_deterministic_json(self, tmp_path: Path) -> None:
        a = save_dataset(_bars(), instrument=_future(), provider="upstox", interval="1d",
                         directory=tmp_path, downloaded_at=datetime(2026, 1, 1))
        b = save_dataset(_bars(), instrument=_future(), provider="upstox", interval="1d",
                         directory=tmp_path / "two", downloaded_at=datetime(2026, 1, 1))
        meta_a = a.path.with_name(a.path.stem + ".meta.json").read_text(encoding="utf-8")
        meta_b = b.path.with_name(b.path.stem + ".meta.json").read_text(encoding="utf-8")
        assert meta_a == meta_b

    def test_hash_stable_and_deterministic(self) -> None:
        assert dataset_hash(_bars()) == dataset_hash(_bars())
        assert isinstance(dataset_hash(_bars()), str)


class TestValidationOnSave:
    def test_empty_bars_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="at least one bar"):
            save_dataset([], instrument=_future(), provider="upstox", interval="1d", directory=tmp_path)

    def test_out_of_order_bars_rejected(self, tmp_path: Path) -> None:
        bars = list(reversed(_bars()))
        with pytest.raises(ValueError, match="chronologically ordered"):
            save_dataset(bars, instrument=_future(), provider="upstox", interval="1d", directory=tmp_path)

    def test_duplicate_timestamps_rejected(self, tmp_path: Path) -> None:
        bars = _bars()
        dup = MarketPrice(
            instrument=_future(), timestamp=bars[0].timestamp,
            open=Decimal("100"), high=Decimal("110"), low=Decimal("90"),
            close=Decimal("105"),
        )
        with pytest.raises(ValueError, match="unique timestamps"):
            save_dataset([dup, *bars], instrument=_future(), provider="upstox", interval="1d",
                         directory=tmp_path)

    def test_unknown_interval_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="interval"):
            save_dataset(_bars(), instrument=_future(), provider="upstox", interval="2d", directory=tmp_path)


class TestLoadFailures:
    def test_missing_csv_raises(self, tmp_path: Path) -> None:
        with pytest.raises(OSError):
            load_dataset(tmp_path / "nope.csv")

    def test_missing_meta_raises(self, tmp_path: Path) -> None:
        csv_path = tmp_path / "orphan.csv"
        csv_path.write_text("timestamp,open,high,low,close,volume,open_interest\n", encoding="utf-8")
        with pytest.raises(FileNotFoundError, match="metadata"):
            load_dataset(csv_path)

    def test_tampered_csv_raises_hash_mismatch(self, tmp_path: Path) -> None:
        saved = save_dataset(_bars(), instrument=_future(), provider="upstox", interval="1d", directory=tmp_path)
        content = saved.path.read_text(encoding="utf-8")
        saved.path.write_text(content.replace("24175", "24125"), encoding="utf-8")
        with pytest.raises(ValueError, match="data_hash"):
            load_dataset(saved.path)

    def test_unexpected_columns_rejected(self, tmp_path: Path) -> None:
        csv_path = tmp_path / "bad.csv"
        csv_path.write_text("a,b,c\n1,2,3\n", encoding="utf-8")
        meta_path = tmp_path / "bad.meta.json"
        meta_path.write_text(json.dumps({
            "schema_version": "1", "provider": "x", "interval": "1d",
            "instrument": {"symbol": "S", "instrument_type": "FUTURE", "underlying_symbol": "U",
                           "expiry": None, "strike": None, "option_type": None, "exchange": "NSE",
                           "exchange_token": None, "lot_size": 1, "tick_size": "0.05", "multiplier": 1},
        }), encoding="utf-8")
        with pytest.raises(ValueError, match="columns"):
            load_dataset(csv_path)


class TestOptionInstrumentRoundtrip:
    def test_option_with_expiry_strike_round_trips(self, tmp_path: Path) -> None:
        option = Instrument(
            symbol="NIFTY1_24500_CE",
            instrument_type=InstrumentType.OPTION_CE,
            underlying_symbol="NIFTY",
            expiry=datetime(2026, 12, 24).date(),
            strike=Decimal("24500"),
            option_type="CE",
            lot_size=75,
            multiplier=25,
        )
        bars = build_sample_ohlcv(option, bars=3)
        saved = save_dataset(bars, instrument=option, provider="kite", interval="1d", directory=tmp_path)
        loaded = load_dataset(saved.path)
        assert loaded.instrument() == option
        assert loaded.instrument().is_option()
        assert loaded.instrument().strike == Decimal("24500")
        assert loaded.instrument().expiry == datetime(2026, 12, 24).date()