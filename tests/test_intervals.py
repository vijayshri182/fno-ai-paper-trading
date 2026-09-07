"""Tests for the canonical bar-interval normalization module."""
from __future__ import annotations

import pytest

from fno_ai_paper_trading.data.intervals import (
    CANONICAL_INTERVALS,
    canonical_interval,
    interval_minutes,
    is_valid_interval,
    kite_interval_token,
    upstox_unit_interval,
)


class TestCanonical:
    def test_canonical_tokens_round_trip(self) -> None:
        for token in CANONICAL_INTERVALS:
            assert canonical_interval(token) == token

    def test_legacy_aliases_resolve(self) -> None:
        assert canonical_interval("minute") == "1m"
        assert canonical_interval("5minute") == "5m"
        assert canonical_interval("60minute") == "1h"
        assert canonical_interval("day") == "1d"
        assert canonical_interval("daily") == "1d"
        assert canonical_interval("week") == "1w"
        assert canonical_interval("month") == "1M"

    def test_casing_and_whitespace_tolerated(self) -> None:
        assert canonical_interval(" 1D ") == "1d"
        assert canonical_interval("Daily") == "1d"

    def test_unknown_and_non_strings_are_none(self) -> None:
        assert canonical_interval("fortnight") is None
        assert canonical_interval("") is None
        assert canonical_interval(5) is None
        assert canonical_interval(None) is None

    def test_is_valid_interval(self) -> None:
        assert is_valid_interval("15m") is True
        assert is_valid_interval("15minute") is True
        assert is_valid_interval("2d") is False  # unknown cadence
        assert is_valid_interval(None) is False


class TestVendorMappings:
    def test_upstox_unit_interval_pairs(self) -> None:
        assert upstox_unit_interval("1m") == ("minutes", 1)
        assert upstox_unit_interval("5m") == ("minutes", 5)
        assert upstox_unit_interval("30m") == ("minutes", 30)
        assert upstox_unit_interval("1h") == ("hours", 1)
        assert upstox_unit_interval("4h") == ("hours", 4)
        assert upstox_unit_interval("1d") == ("days", 1)
        assert upstox_unit_interval("1w") == ("weeks", 1)
        assert upstox_unit_interval("1M") == ("months", 1)

    def test_upstox_legacy_day_resolves_to_days_one(self) -> None:
        assert upstox_unit_interval("day") == ("days", 1)

    def test_upstox_unknown_raises_value_error(self) -> None:
        with pytest.raises(ValueError):
            upstox_unit_interval("2d")

    def test_kite_tokens(self) -> None:
        assert kite_interval_token("1m") == "minute"
        assert kite_interval_token("30m") == "30minute"
        assert kite_interval_token("1d") == "day"
        assert kite_interval_token("1w") == "week"

    def test_kite_unknown_raises_value_error(self) -> None:
        with pytest.raises(ValueError):
            kite_interval_token("quarterly")


class TestIntervalMinutes:
    def test_fixed_minutes(self) -> None:
        assert interval_minutes("1m") == 1
        assert interval_minutes("15m") == 15
        assert interval_minutes("1h") == 60
        assert interval_minutes("1d") == 1440

    def test_week_and_month_are_none(self) -> None:
        assert interval_minutes("1w") is None
        assert interval_minutes("1M") is None

    def test_unknown_is_none(self) -> None:
        assert interval_minutes("snake-oil") is None