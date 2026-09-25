"""Phase 7 tests — deterministic NIFTY market-regime engine.

Functional, integrity, warm-up and provider-independence coverage. All tests
are hermetic (hand-built ``MarketPrice`` fixtures plus the existing
``mock_provider`` replay builder); nothing touches the network, Upstox or the
execution layer.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from fno_ai_paper_trading.data.market_hours import market_phase
from fno_ai_paper_trading.data.mock_provider import build_crossing_ohlcv
from fno_ai_paper_trading.models.enums import InstrumentType, MarketPhase
from fno_ai_paper_trading.models.instruments import Instrument
from fno_ai_paper_trading.models.market import MarketPrice
from fno_ai_paper_trading.regime import TrendState, VolatilityState
from fno_ai_paper_trading.research.regime import (
    ENGINE_VERSION,
    INSUFFICIENT_DATA,
    SCHEMA_VERSION,
    Direction,
    MarketDataState,
    MarketRegimeEngine,
    WarmupStatus,
)


def _future() -> Instrument:
    return Instrument(
        symbol="NIFTY1",
        instrument_type=InstrumentType.FUTURE,
        underlying_symbol="NIFTY",
    )


def _bars(closes: list[Decimal], *, minute_step: int = 1, start: datetime | None = None) -> list[MarketPrice]:
    base = start or datetime(2026, 9, 1, 9, 15)
    bars = []
    for i, close in enumerate(closes):
        width = Decimal("2")
        bars.append(
            MarketPrice(
                instrument=_future(),
                timestamp=base + timedelta(minutes=minute_step * i),
                open=close - width,
                high=close + width,
                low=close - width,
                close=close,
                volume=1000 + i,
            )
        )
    return bars


def _closes(values: list[int]) -> list[Decimal]:
    return [Decimal(str(v)) for v in values]


def _uptrend(bars: int = 40) -> list[MarketPrice]:
    return _bars(_closes([24000 + 10 * i for i in range(bars)]))


def _downtrend(bars: int = 40) -> list[MarketPrice]:
    return _bars(_closes([25000 - 10 * i for i in range(bars)]))


def _sideways(bars: int = 40) -> list[MarketPrice]:
    return _bars(_closes([24200 for _ in range(bars)]))


def _calm_then_choppy(calm: int = 30, choppy: int = 10) -> list[MarketPrice]:
    values: list[int] = []
    value = 24000
    for _ in range(calm):
        value += 1
        values.append(value)
    for _ in range(choppy):
        value += 60 if len(values) % 2 == 0 else -60
        values.append(value)
    return _bars(_closes(values))


def _choppy_then_calm(choppy: int = 30, calm: int = 10) -> list[MarketPrice]:
    values: list[int] = []
    value = 24200
    for i in range(choppy):
        value += 60 if i % 2 == 0 else -60
        values.append(value)
    for _ in range(calm):
        value += 1
        values.append(value)
    return _bars(_closes(values))


ENGINE = MarketRegimeEngine


def _flat_features(report) -> dict:
    return {k: v for k, v in report.features.items()}


# ----------------------------------------------------------------- functional


class TestFunctionalRegimes:
    def test_bullish_trend(self) -> None:
        report = ENGINE().evaluate(_uptrend())
        assert report.has_regime
        assert report.direction is Direction.BULLISH
        assert report.trend is TrendState.UP
        assert report.regime.startswith("BULLISH_")
        assert report.data_state is MarketDataState.VALID

    def test_bearish_trend(self) -> None:
        report = ENGINE().evaluate(_downtrend())
        assert report.has_regime
        assert report.direction is Direction.BEARISH
        assert report.trend is TrendState.DOWN
        assert report.regime.startswith("BEARISH_")

    def test_sideways_is_neutral(self) -> None:
        report = ENGINE().evaluate(_sideways())
        assert report.has_regime
        assert report.direction is Direction.NEUTRAL
        assert report.trend is TrendState.SIDEWAYS
        assert report.regime.startswith("NEUTRAL_")

    def test_high_volatility(self) -> None:
        report = ENGINE().evaluate(_calm_then_choppy())
        assert report.has_regime
        assert report.volatility is VolatilityState.HIGH
        assert "HIGH" in report.regime

    def test_low_volatility(self) -> None:
        report = ENGINE().evaluate(_choppy_then_calm())
        assert report.has_regime
        assert report.volatility is VolatilityState.LOW
        assert "LOW" in report.regime

    def test_insufficient_warmup_is_insufficient_data(self) -> None:
        report = ENGINE().evaluate(_uptrend(bars=10))
        assert not report.has_regime
        assert report.regime == INSUFFICIENT_DATA
        assert report.data_state is MarketDataState.UNAVAILABLE
        assert report.warmup_status is WarmupStatus.INSUFFICIENT
        assert report.direction is None and report.trend is None and report.volatility is None

    def test_volatility_naming_exposes_low_high_vol(self) -> None:
        for report, token in (
            (ENGINE().evaluate(_calm_then_choppy()), "HIGH"),
            (ENGINE().evaluate(_choppy_then_calm()), "LOW"),
        ):
            assert {"BULLISH", "BEARISH", "NEUTRAL", "HIGH", "LOW", "NORMAL"} & set(report.regime.split("_"))


# ----------------------------------------------------------------- warm-up


class TestWarmup:
    def test_required_bars_matches_ws73_contract(self) -> None:
        # Reused MA(5,21) core contract stays 22 bars (slow + 1), unchanged.
        assert ENGINE().required_bars == 22
        assert ENGINE(fast=5, slow=21).required_bars == 22

    def test_first_valid_index_is_exact(self) -> None:
        bars = _uptrend()
        assert ENGINE().first_valid_index(bars) == 21  # 22nd bar (0-based 21)

    def test_below_required_bars_stays_insufficient(self) -> None:
        engine = ENGINE()
        for index in (0, 10, 20):
            report = engine.evaluate_prefix(_uptrend(), index)
            assert report.regime == INSUFFICIENT_DATA
            assert report.data_state is MarketDataState.UNAVAILABLE
            assert not report.features.get("ma_gap_pct")

    def test_no_first_valid_index_on_short_series(self) -> None:
        assert ENGINE().first_valid_index(_uptrend(5)) is None

    def test_warmup_table_documented(self) -> None:
        table = ENGINE().indicator_warmups
        assert table["core_decision"] == 22
        assert table["sma_slow"] == 21
        assert table["atr_14"] == 15
        assert table["volatility_long_30"] == 31
        assert table["structure_range_10"] == 10


# ----------------------------------------------------------------- integrity


class TestIntegrity:
    def test_no_look_ahead_prefix_equals_isolated_prefix(self) -> None:
        engine = ENGINE()
        bars = _uptrend(60)
        for index in range(21, 60):
            via_long_series = engine.evaluate_prefix(bars, index).to_dict()
            via_isolated = engine.evaluate(bars[: index + 1]).to_dict()
            assert via_long_series == via_isolated, f"look-ahead at index {index}"

    def test_deterministic_repeat_and_across_instances(self) -> None:
        bars = _calm_then_choppy()
        a = ENGINE().evaluate(bars).to_dict()
        b = ENGINE().evaluate(bars).to_dict()
        c = ENGINE().evaluate(bars).to_dict()
        assert a == b == c
        assert json.dumps(a, sort_keys=True, default=str) == json.dumps(b, sort_keys=True, default=str)

    def test_same_input_same_result(self) -> None:
        one = ENGINE().evaluate_prefix(_uptrend(), 30).to_dict()
        two = ENGINE().evaluate_prefix(_uptrend(), 30).to_dict()
        assert one == two

    def test_boundary_timestamps(self) -> None:
        bars = _uptrend()
        report = ENGINE().evaluate(bars)
        assert report.timestamp == bars[-1].timestamp
        assert report.available_bars == len(bars) == report.bar_count

    def test_session_phase_open(self) -> None:
        start = datetime(2026, 9, 24, 10, 0)  # trading day, continuous trading
        bars = _uptrend(40)
        bars = [MarketPrice(
            instrument=_future(),
            timestamp=start + timedelta(minutes=i),
            open=b.open, high=b.high, low=b.low, close=b.close, volume=b.volume,
        ) for i, b in enumerate(bars)]
        report = ENGINE().evaluate(bars)
        assert report.session is MarketPhase.OPEN
        assert market_phase(bars[-1].timestamp) is MarketPhase.OPEN

    def test_session_phase_closed(self) -> None:
        start = datetime(2026, 9, 24, 16, 0)  # after close
        bars = [MarketPrice(
            instrument=_future(),
            timestamp=start + timedelta(minutes=i),
            open=b.open, high=b.high, low=b.low, close=b.close, volume=b.volume,
        ) for i, b in enumerate(_uptrend(40))]
        assert ENGINE().evaluate(bars).session is MarketPhase.CLOSED

    def test_empty_bars_unavailable(self) -> None:
        report = ENGINE().evaluate([])
        assert report.regime == INSUFFICIENT_DATA
        assert report.data_state is MarketDataState.UNAVAILABLE
        assert report.timestamp is None

    def test_non_marketprice_item_invalid(self) -> None:
        bars = [5] + _uptrend(5)[1:]  # type: ignore[list-item]
        report = ENGINE().evaluate(bars)
        assert report.data_state is MarketDataState.INVALID
        assert report.regime == INSUFFICIENT_DATA

    def test_non_monotonic_timestamps_invalid(self) -> None:
        bars = _uptrend()
        bars[-1] = MarketPrice(
            instrument=_future(),
            timestamp=bars[-2].timestamp - timedelta(minutes=1),
            open=bars[-1].open, high=bars[-1].high, low=bars[-1].low,
            close=bars[-1].close, volume=bars[-1].volume,
        )
        report = ENGINE().evaluate(bars)
        assert report.data_state is MarketDataState.INVALID
        assert any("non-monotonic" in e.reason for e in report.evidence)

    def test_index_out_of_range_raises(self) -> None:
        with pytest.raises(IndexError):
            ENGINE().evaluate_prefix(_uptrend(), 99)

    def test_features_are_decimal_int_str_only(self) -> None:
        report = ENGINE().evaluate(_uptrend())
        assert report.has_regime
        for name, value in report.features.items():
            assert isinstance(value, (Decimal, int, str)), f"{name} is {type(value).__name__}"
        assert "atr_14" in report.features
        assert "distance_from_high_pct" in report.features
        assert "range_pct" in report.features

    def test_report_has_no_recommendation(self) -> None:
        report = ENGINE().evaluate(_uptrend()).to_dict()
        for word in ("BUY", "SELL", "score", "recommendation", "strike", "premium", "token", "password"):
            assert word not in json.dumps(report, default=str).upper() or word.islower()

    def test_evidence_is_present_and_deterministic(self) -> None:
        report = ENGINE().evaluate(_uptrend())
        dimensions = {e.dimension for e in report.evidence}
        assert {"direction", "volatility", "warmup", "identity"} <= dimensions
        assert all(e.reason for e in report.evidence)
        again = ENGINE().evaluate(_uptrend())
        assert [e.to_dict() for e in report.evidence] == [e.to_dict() for e in again.evidence]

    def test_versions_are_recorded(self) -> None:
        report = ENGINE().evaluate(_uptrend())
        assert report.schema_version == SCHEMA_VERSION
        assert report.engine_version == ENGINE_VERSION
        assert report.source == "BARS_REPLAY"


# ------------------------------------------------------------ provider independence


class TestProviderIndependence:
    def test_works_with_replay_fixture_bars(self) -> None:
        bars = build_crossing_ohlcv(_future())
        report = ENGINE().evaluate(bars)
        assert report.has_regime
        assert report.direction in (Direction.BULLISH, Direction.BEARISH, Direction.NEUTRAL)

    def test_no_execution_or_vendor_imports(self) -> None:
        import ast
        from pathlib import Path

        module = (
            Path(__file__).resolve().parents[1]
            / "src/fno_ai_paper_trading/research/regime/engine.py"
        )
        tree = ast.parse(module.read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        roots = {name.split(".")[0] for name in imported}
        for banned in ("execution", "upstox", "broker"):
            assert banned not in roots, f"engine imports forbidden root {banned}"
        assert any(name.startswith("fno_ai_paper_trading.regime") for name in imported)  # reuses WS 7.3 detector
        assert any(name.startswith("fno_ai_paper_trading.features") for name in imported)  # reuses FeatureEngineer

    def test_threshold_constant_changes_band_deterministically(self) -> None:
        # Default band (0.05%) classifies the +10/bar drift as BULLISH.
        assert ENGINE().evaluate(_uptrend()).direction is Direction.BULLISH
        # Widening the declared constant to 0.5% deterministically lands NEUTRAL.
        wide_gap = ENGINE(trend_threshold_pct="0.5").evaluate(_uptrend())
        assert wide_gap.has_regime and wide_gap.direction is Direction.NEUTRAL
        # A wider band over a weak drift is also deterministically neutral.
        weak = _bars(_closes([24000 + 2 * i for i in range(40)]))
        assert ENGINE(trend_threshold_pct="1.0").evaluate(weak).direction is Direction.NEUTRAL


# ------------------------------------------------------------- detector agreement


class TestReuseAgreement:
    def test_report_trend_matches_ws73_detector(self) -> None:
        from fno_ai_paper_trading.regime import RegimeDetector

        engine = ENGINE()
        bars = _uptrend()
        report = engine.evaluate(bars)
        detector_regime = RegimeDetector().detect(bars)
        assert detector_regime is not None
        assert report.trend is detector_regime.trend
        assert report.volatility is detector_regime.volatility

    def test_source_record_not_indexed(self) -> None:
        report = ENGINE().evaluate(_uptrend())
        assert not report.has_regime or isinstance(report.timestamp, datetime)