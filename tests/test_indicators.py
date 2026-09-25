"""Golden-value and invariant tests for the deterministic indicator library.

Hand-computed where possible; the behavioral fixtures reuse the same OHLCV
shape the live smoke harness uses so the numbers are reproducible offline.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from fno_ai_paper_trading.strategies import indicators


class TestValidation:
    def test_zero_period_rejected(self):
        with pytest.raises(ValueError):
            indicators.sma([Decimal("1")], 0)
        with pytest.raises(ValueError):
            indicators.ema([Decimal("1")], 0)
        with pytest.raises(ValueError):
            indicators.rsi([Decimal("1")], 0)
        with pytest.raises(ValueError):
            indicators.bollinger([Decimal("1")], 0)

    def test_mismatched_lengths_rejected(self):
        with pytest.raises(ValueError):
            indicators.atr([Decimal("1")], [Decimal("1")], [Decimal("1"), Decimal("2")])
        with pytest.raises(ValueError):
            indicators.stochastic([Decimal("1")], [Decimal("1")], [Decimal("1"), Decimal("2")])

    def test_empty_input_is_empty_output(self):
        assert indicators.sma([], 5) == []
        assert indicators.macd([], 12, 26, 9)[0] == []


class TestAverages:
    def test_sma_hand_computed(self):
        result = indicators.sma([Decimal("1"), Decimal("2"), Decimal("3"), Decimal("4")], 2)
        assert result == [None, Decimal("1.5"), Decimal("2.5"), Decimal("3.5")]

    def test_sma_insufficient_data(self):
        assert indicators.sma([Decimal("1"), Decimal("2")], 5) == [None, None]

    def test_ema_hand_computed_period_2(self):
        result = indicators.ema([Decimal("1"), Decimal("2"), Decimal("3"), Decimal("4")], 2)
        assert result == [None, Decimal("1.5"), Decimal("2.5"), Decimal("3.5")]

    def test_ema_warmup_is_none(self):
        assert indicators.ema([Decimal("1")], 5) == [None]


class TestMACD:
    def test_histogram_is_difference(self):
        closes = [Decimal(str(200 + i * 2)) for i in range(50)]
        line, signal, histogram = indicators.macd(closes, 12, 26, 9)
        for i in range(50):
            if line[i] is not None and signal[i] is not None:
                assert histogram[i] == pytest.approx(line[i] - signal[i])

    def test_macd_warmup_regions_are_none(self):
        closes = [Decimal(str(100 + i)) for i in range(40)]
        line, signal, histogram = indicators.macd(closes, 12, 26, 9)
        assert line[24] is None and line[25] is not None
        # signal needs >=9 MACD points; MACD first valid at 25 => signal at 33
        assert signal[32] is None and signal[33] is not None
        assert histogram[33] is not None


class TestMomentum:
    def test_rsi_all_gains_is_100(self):
        values = [Decimal(str(10 + i)) for i in range(20)]
        result = indicators.rsi(values, 14)
        assert result[13] is None and result[14] == Decimal("100")

    def test_rsi_all_losses_is_0(self):
        values = [Decimal(str(20 - i)) for i in range(20)]
        result = indicators.rsi(values, 14)
        assert result[14] == Decimal("0")

    def test_rsi_flat_series_is_50(self):
        result = indicators.rsi([Decimal("5")] * 20, 14)
        assert result[14] == Decimal("50")

    def test_stochastic_high_close_is_100(self):
        highs = [Decimal("10")] * 10
        lows = [Decimal("0")] * 10
        closes = [Decimal("10")] * 10  # close pinned to the window high
        k, d = indicators.stochastic(highs, lows, closes, 5, 3)
        assert k[9] == Decimal("100")
        assert k[3] is None and k[4] is not None
        assert d[9] is not None  # %D is an SMA of %K once warmed up

    def test_williams_r_range(self):
        highs = [Decimal("10")] * 10
        lows = [Decimal("0")] * 10
        closes = [Decimal("10")] * 10
        result = indicators.williams_r(highs, lows, closes, 5)
        assert result[9] == Decimal("0")  # close == window high
        closes = [Decimal("0")] * 10
        result = indicators.williams_r(highs, lows, closes, 5)
        assert result[9] == Decimal("-100")  # close == window low

    def test_mfi_all_positive_flow_is_100(self):
        highs = [Decimal(f"{100 + i}") for i in range(20)]
        lows = [Decimal(f"{90 + i}") for i in range(20)]
        closes = [Decimal(f"{95 + i}") for i in range(20)]
        volume = [100 + i for i in range(20)]
        result = indicators.mfi(highs, lows, closes, volume, 5)
        assert result[5] == Decimal("100")


class TestVolatility:
    def test_true_range_hand_computed(self):
        result = indicators.true_range(
            [Decimal("10"), Decimal("12")],
            [Decimal("9"), Decimal("11")],
            [Decimal("10"), Decimal("11")],
        )
        # TR = max(high-low=1, |high-prev_close|=2, |low-prev_close|=1) = 2
        assert result == [None, Decimal("2")]

    def test_atr_constant_range(self):
        n = 30
        highs = [Decimal("2520")] * n
        lows = [Decimal("2485")] * n
        closes = [Decimal("2500")] * n
        result = indicators.atr(highs, lows, closes, 14)
        assert result[13] is None
        assert result[14] == Decimal("35")  # TR = 35 constant
        assert result[-1] == Decimal("35")

    def test_bollinger_constant_band_shrinks_to_zero(self):
        closes = [Decimal("5")] * 10
        mid, upper, lower, bandwidth, percent_b = indicators.bollinger(closes, 5, 2)
        assert mid[-1] == Decimal("5")
        assert upper[-1] == Decimal("5") and lower[-1] == Decimal("5")
        assert bandwidth[-1] == Decimal("0")
        assert percent_b[-1] is None  # zero-width band carries no %B

    def test_bollinger_hand_mean(self):
        closes = [Decimal("1"), Decimal("2"), Decimal("3")]
        mid, *_ = indicators.bollinger(closes, 3, 2)
        assert mid[-1] == Decimal("2")

    def test_keltner_envelope_ordered(self):
        n = 30
        highs = [Decimal("2520")] * n
        lows = [Decimal("2485")] * n
        closes = [Decimal("2500")] * n
        mid, upper, lower, width = indicators.keltner(highs, lows, closes, 20, 10, 2)
        assert mid[-1] == Decimal("2500")
        assert lower[-1] < Decimal("2500") < upper[-1]
        assert width[-1] > Decimal("0")

    def test_supertrend_upward_direction(self):
        closes = [Decimal(str(200 + i * 3)) for i in range(30)]
        highs = [c + Decimal("5") for c in closes]
        lows = [c - Decimal("5") for c in closes]
        line, direction = indicators.supertrend(highs, lows, closes, 10, 3)
        assert direction[-1] == 1
        assert line[-1] is not None


class TestVolume:
    def test_obv_cumulative_signed(self):
        closes = [Decimal("10"), Decimal("12"), Decimal("12"), Decimal("11"), Decimal("13")]
        volume = [0, 5, 7, 3, 4]
        result = indicators.obv(closes, volume)
        assert result == [Decimal("0"), Decimal("5"), Decimal("5"), Decimal("2"), Decimal("6")]

    def test_vwap_weighted_average(self):
        highs = [Decimal("110"), Decimal("112"), Decimal("114")]
        lows = [Decimal("90"), Decimal("92"), Decimal("94")]
        closes = [Decimal("100"), Decimal("102"), Decimal("104")]
        volume = [1, 2, 5]
        result = indicators.vwap(highs, lows, closes, volume, anchor=3)
        typical = [(110 + 90 + 100) / 3, (112 + 92 + 102) / 3, (114 + 94 + 104) / 3]
        expected = (
            typical[0] * 1 + typical[1] * 2 + typical[2] * 5
        ) / (1 + 2 + 5)
        assert result[-1] == expected

    def test_vwap_zero_volume_falls_back_to_typical_mean(self):
        highs = [Decimal("12")] * 4
        lows = [Decimal("8")] * 4
        closes = [Decimal("10")] * 4
        result = indicators.vwap(highs, lows, closes, [0, 0, 0, 0], anchor=3)
        assert result[-1] == Decimal("10")