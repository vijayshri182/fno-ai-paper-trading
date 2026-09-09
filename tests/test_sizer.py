"""Tests for the V1 risk-based position sizer.

Verifies the pure sizing math (risk amount, stop distance/price, lot-rounded
floor quantity), the cash/no-leverage guard, single-position semantics and
rejection behaviour — all with explicit decision-time inputs.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from fno_ai_paper_trading.models.enums import InstrumentType
from fno_ai_paper_trading.models.instruments import Instrument
from fno_ai_paper_trading.risk.sizer import RiskBasedPositionSizer, SizerConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _index() -> Instrument:
    """NIFTY-style index with lot size 1 and multiplier 1."""
    return Instrument(symbol="NIFTY_INDEX", instrument_type=InstrumentType.INDEX, underlying_symbol="NIFTY")


def _future() -> Instrument:
    """Generic NSE future with the conventional lot size of 75."""
    return Instrument(symbol="NIFTY1", instrument_type=InstrumentType.FUTURE, underlying_symbol="NIFTY", lot_size=75)


def _multi() -> Instrument:
    """Instrument whose multiplier scales the cash/risk basis (lot size 1)."""
    return Instrument(symbol="MULTI", instrument_type=InstrumentType.FUTURE, underlying_symbol="NIFTY", lot_size=1, multiplier=5)


def _sizer(**overrides) -> RiskBasedPositionSizer:
    if not overrides:
        return RiskBasedPositionSizer()
    return RiskBasedPositionSizer(SizerConfig(**overrides))


# ---------------------------------------------------------------------------
# Approved sizing
# ---------------------------------------------------------------------------

class TestApprovedSizing:
    def test_index_matches_reference_numbers(self) -> None:
        # equity 100000 @ 1% risk, 2% stop, entry 24000, lot 1, multiplier 1.
        sizing = _sizer().size(
            equity=Decimal("100000"),
            available_cash=Decimal("100000"),
            entry_price=Decimal("24000"),
            instrument=_index(),
        )
        assert sizing.approved is True
        assert sizing.quantity == 2
        assert sizing.risk_amount == Decimal("1000")
        assert sizing.stop_distance == Decimal("480")
        assert sizing.stop_price == Decimal("23520")
        assert sizing.skip_reason == ""

    def test_rounds_down_by_lot_increments_never_up(self) -> None:
        # Raw quantity is 1200 / 480 = 2.5 -> 2, never rounded to 3.
        sizing = _sizer().size(
            equity=Decimal("120000"),
            available_cash=Decimal("1000000"),
            entry_price=Decimal("24000"),
            instrument=_index(),
        )
        assert sizing.approved is True
        assert sizing.quantity == 2

    def test_respects_non_trivial_lot_size(self) -> None:
        # equity 4,000,000 -> risk 40,000, stop 480 -> raw 83.33 -> 1 lot of 75.
        sizing = _sizer().size(
            equity=Decimal("4000000"),
            available_cash=Decimal("1000000000"),
            entry_price=Decimal("24000"),
            instrument=_future(),
        )
        assert sizing.approved is True
        assert sizing.quantity == 75

    def test_multiplier_scales_the_denominator(self) -> None:
        # Same equity/entry: multiplier 5 cuts the quantity by 5 (5000 / 2400).
        base = _sizer().size(
            equity=Decimal("500000"),
            available_cash=Decimal("1000000"),
            entry_price=Decimal("24000"),
            instrument=_index(),
        )
        scaled = _sizer().size(
            equity=Decimal("500000"),
            available_cash=Decimal("1000000"),
            entry_price=Decimal("24000"),
            instrument=_multi(),
        )
        assert base.quantity == 10
        assert scaled.quantity == 2

    def test_sizes_from_current_equity_input(self) -> None:
        # Same everything except the supplied (current) equity.
        big = _sizer().size(
            equity=Decimal("100000"),
            available_cash=Decimal("1000000"),
            entry_price=Decimal("24000"),
            instrument=_index(),
        )
        small = _sizer().size(
            equity=Decimal("50000"),
            available_cash=Decimal("1000000"),
            entry_price=Decimal("24000"),
            instrument=_index(),
        )
        assert big.quantity == 2
        assert small.quantity == 1


# ---------------------------------------------------------------------------
# Cash / no-leverage guard
# ---------------------------------------------------------------------------

class TestCashGuard:
    def test_reduces_quantity_to_fit_cash(self) -> None:
        # Raw basis says 2 lots at 24000; cash 40000 only covers 1 lot.
        sizing = _sizer().size(
            equity=Decimal("100000"),
            available_cash=Decimal("40000"),
            entry_price=Decimal("24000"),
            instrument=_index(),
        )
        assert sizing.approved is True
        assert sizing.quantity == 1

    def test_skips_when_cash_cannot_cover_minimum_lot(self) -> None:
        sizing = _sizer().size(
            equity=Decimal("100000"),
            available_cash=Decimal("10000"),
            entry_price=Decimal("24000"),
            instrument=_index(),
        )
        assert sizing.approved is False
        assert "insufficient available cash" in sizing.skip_reason

    def test_skips_zero_available_cash(self) -> None:
        sizing = _sizer().size(
            equity=Decimal("100000"),
            available_cash=Decimal("0"),
            entry_price=Decimal("24000"),
            instrument=_index(),
        )
        assert sizing.approved is False
        assert "insufficient available cash" in sizing.skip_reason


# ---------------------------------------------------------------------------
# Single-position semantics
# ---------------------------------------------------------------------------

class TestSinglePosition:
    def test_skips_when_long_position_open(self) -> None:
        sizing = _sizer().size(
            equity=Decimal("100000"),
            available_cash=Decimal("100000"),
            entry_price=Decimal("24000"),
            instrument=_index(),
            current_quantity=5,
        )
        assert sizing.approved is False
        assert "already open" in sizing.skip_reason

    def test_skips_when_short_position_open(self) -> None:
        sizing = _sizer().size(
            equity=Decimal("100000"),
            available_cash=Decimal("100000"),
            entry_price=Decimal("24000"),
            instrument=_index(),
            current_quantity=-3,
        )
        assert sizing.approved is False
        assert "already open" in sizing.skip_reason

    def test_rejects_non_integer_current_quantity(self) -> None:
        sizing = _sizer().size(
            equity=Decimal("100000"),
            available_cash=Decimal("100000"),
            entry_price=Decimal("24000"),
            instrument=_index(),
            current_quantity=2.0,
        )
        assert sizing.approved is False
        assert "integer" in sizing.skip_reason


# ---------------------------------------------------------------------------
# Rejections
# ---------------------------------------------------------------------------

class TestRejections:
    def test_skips_below_minimum_increment(self) -> None:
        sizing = _sizer().size(
            equity=Decimal("10000"),
            available_cash=Decimal("100000"),
            entry_price=Decimal("24000"),
            instrument=_index(),
        )
        assert sizing.approved is False
        assert "below the minimum" in sizing.skip_reason

    def test_skips_invalid_equity(self) -> None:
        for bad in (Decimal("0"), Decimal("-1"), float("nan"), float("inf"), "not-a-number"):
            sizing = _sizer().size(
                equity=bad,
                available_cash=Decimal("100000"),
                entry_price=Decimal("24000"),
                instrument=_index(),
            )
            assert sizing.approved is False
            assert "equity" in sizing.skip_reason

    def test_skips_invalid_entry_price(self) -> None:
        for bad in (Decimal("0"), Decimal("-5"), float("nan"), "abc"):
            sizing = _sizer().size(
                equity=Decimal("100000"),
                available_cash=Decimal("100000"),
                entry_price=bad,
                instrument=_index(),
            )
            assert sizing.approved is False
            assert "entry_price" in sizing.skip_reason

    def test_skips_non_finite_available_cash(self) -> None:
        sizing = _sizer().size(
            equity=Decimal("100000"),
            available_cash=float("nan"),
            entry_price=Decimal("24000"),
            instrument=_index(),
        )
        assert sizing.approved is False
        assert "available_cash" in sizing.skip_reason

    def test_requires_an_instrument(self) -> None:
        with pytest.raises(TypeError):
            _sizer().size(
                equity=Decimal("100000"),
                available_cash=Decimal("100000"),
                entry_price=Decimal("24000"),
                instrument=None,
            )
        with pytest.raises(TypeError):
            _sizer().size(
                equity=Decimal("100000"),
                available_cash=Decimal("100000"),
                entry_price=Decimal("24000"),
                instrument="NIFTY",
            )


# ---------------------------------------------------------------------------
# Configuration validation
# ---------------------------------------------------------------------------

class TestConfig:
    def test_rejects_invalid_configuration(self) -> None:
        with pytest.raises(ValueError):
            SizerConfig(risk_per_trade_pct=Decimal("0"))
        with pytest.raises(ValueError):
            SizerConfig(risk_per_trade_pct=Decimal("-0.01"))
        with pytest.raises(ValueError):
            SizerConfig(stop_loss_pct=Decimal("0"))
        with pytest.raises(ValueError):
            SizerConfig(stop_loss_pct=Decimal("1"))
        with pytest.raises(ValueError):
            SizerConfig(stop_loss_pct=Decimal("-0.02"))
        with pytest.raises(ValueError):
            SizerConfig(commission_rate=Decimal("-0.0003"))
        with pytest.raises(ValueError):
            SizerConfig(commission_fixed=Decimal("-1"))

    def test_accepts_boundary_valid_configuration(self) -> None:
        config = SizerConfig(
            risk_per_trade_pct=Decimal("0.005"),
            stop_loss_pct=Decimal("0.99"),
            commission_rate=Decimal("0"),
            commission_fixed=Decimal("0"),
        )
        sizing = RiskBasedPositionSizer(config).size(
            equity=Decimal("10000000"),
            available_cash=Decimal("100000000"),
            entry_price=Decimal("24000"),
            instrument=_index(),
        )
        assert sizing.approved is True


class TestAcceptanceProperty:
    def test_risk_budget_is_never_exceeded(self) -> None:
        # Acceptance criterion §13.6: qty * stop_distance * multiplier <= risk_amount.
        for equity in (Decimal("100000"), Decimal("120000"), Decimal("500000")):
            for lot_instrument in (_index(), _future(), _multi()):
                sizing = _sizer().size(
                    equity=equity,
                    available_cash=Decimal("1000000000"),
                    entry_price=Decimal("24000"),
                    instrument=lot_instrument,
                )
                if not sizing.approved:
                    continue
                basis = (
                    sizing.quantity
                    * sizing.stop_distance
                    * lot_instrument.multiplier
                )
                assert basis <= sizing.risk_amount


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------

class TestDeterminism:
    def test_same_inputs_yield_identical_results(self) -> None:
        kwargs = dict(
            equity=Decimal("100000"),
            available_cash=Decimal("100000"),
            entry_price=Decimal("24000"),
            instrument=_index(),
        )
        first = _sizer().size(**kwargs)
        second = _sizer().size(**kwargs)
        fresh = RiskBasedPositionSizer(SizerConfig()).size(**kwargs)
        assert first == second == fresh
        assert first.risk_amount == second.risk_amount
        assert first.stop_price == second.stop_price

    def test_rejection_is_stable(self) -> None:
        kwargs = dict(
            equity=Decimal("1000"),
            available_cash=Decimal("100000"),
            entry_price=Decimal("24000"),
            instrument=_index(),
        )
        assert _sizer().size(**kwargs) == _sizer().size(**kwargs)