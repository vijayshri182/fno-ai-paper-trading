"""Phase 2 — adversarial market-data testing (20 scenarios).

The track is fail-closed on data: a bad bar can never influence a signal or an
order. These tests push pathological batches through the validator/engine and
assert either a safe poison, a hard structural stop, or a clean no-op — never a
trade on bad data.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from fno_ai_paper_trading.models.market import MarketPrice
from fno_ai_paper_trading.paper_track.bars import BarValidator
from fno_ai_paper_trading.paper_track.errors import TrackValidationError
from fno_ai_paper_trading.paper_track.feed import SyntheticFeed, TRACK_INSTRUMENT
from fno_ai_paper_trading.paper_track.policy import SessionPolicy
from tests.paper_track_testkit import DAY0, day_ticks, drive, make_engine

VALID = datetime(2026, 9, 21, 9, 15, 0)
POLICY = SessionPolicy()


def _raw_bar(**fields: object) -> MarketPrice:
    """A MarketPrice instance bypassing the shared ctor (frozen dataclass hack).

    Lets us inject truly toxic values (negatives, zero, NaN) so the *track's
    defensive validator* is what gets tested — not the shared model's ctor.
    """
    obj = object.__new__(MarketPrice)
    default = {
        "instrument": TRACK_INSTRUMENT(),
        "timestamp": VALID,
        "open": Decimal("25000"),
        "high": Decimal("25001"),
        "low": Decimal("24999"),
        "close": Decimal("25000"),
        "volume": 1000,
        "open_interest": None,
    }
    default.update(fields)
    for key, value in default.items():
        object.__setattr__(obj, key, value)
    return obj


def _valid_bar(ts: datetime = VALID) -> MarketPrice:
    return MarketPrice(
        instrument=TRACK_INSTRUMENT(),
        timestamp=ts,
        open=Decimal("25000"),
        high=Decimal("25001"),
        low=Decimal("24999"),
        close=Decimal("25000"),
        volume=1000,
    )


def _validate(bars, *, now: datetime = VALID + timedelta(minutes=5), last_processed=None):
    validator = BarValidator(interval="5m", policy=POLICY, expected_symbol="NIFTY 50")
    return validator.validate(bars, now=now, last_processed=last_processed)


# --------------------------------------------------------------------------- #
# 1-9: individual toxic bars are poisoned (never tradeable, counted as skips)
# --------------------------------------------------------------------------- #

def test_poison_zero_or_negative_prices():
    for field, value in (
        ("open", Decimal("0")),
        ("open", Decimal("-5")),
        ("high", Decimal("-1")),
        ("low", Decimal("-1")),
        ("close", Decimal("0")),
        ("close", Decimal("-100")),
    ):
        result = _validate([_raw_bar(**{field: value})])
        assert result.poison_count == 1, (field, value)
        assert result.valid == ()


def test_poison_non_numeric_or_non_finite_price():
    for value in ("NaN", "Infinity", "abc"):
        result = _validate([_raw_bar(close=value)])
        assert result.poison_count == 1, value


def test_poison_high_below_open_or_close():
    result = _validate([_raw_bar(high=Decimal("100"))])
    assert result.poison_count == 1
    assert "high" in result.poison_reasons[0]


def test_poison_low_above_open_or_close():
    result = _validate([_raw_bar(low=Decimal("999999"))])
    assert result.poison_count == 1
    assert "low" in result.poison_reasons[0]


def test_poison_high_below_low():
    result = _validate([_raw_bar(high=Decimal("10"), low=Decimal("20"))])
    assert result.poison_count == 1


def test_poison_negative_volume():
    result = _validate([_raw_bar(volume=-10)])
    assert result.poison_count == 1
    assert "volume" in result.poison_reasons[0]


def test_poison_non_marketprice_element():
    result = _validate([_valid_bar(), "not-a-bar"])
    assert result.poison_count == 1
    assert "not a MarketPrice" in result.poison_reasons[0]


def test_poison_bar_from_non_trading_day():
    saturday = datetime(2026, 9, 26, 9, 15, 0)
    result = _validate([_valid_bar(saturday)], now=saturday + timedelta(minutes=5))
    assert result.poison_count == 1
    assert "session window" in result.poison_reasons[0]


def test_poison_bar_outside_session_window():
    early = datetime(2026, 9, 21, 9, 10, 0)
    late = datetime(2026, 9, 21, 15, 31, 0)
    assert _validate([_valid_bar(early)], now=early + timedelta(minutes=5)).poison_count == 1
    assert _validate([_valid_bar(late)], now=late + timedelta(minutes=5)).poison_count == 1


def test_poison_incomplete_bar():
    result = _validate([_valid_bar()], now=VALID + timedelta(minutes=2))
    assert result.poison_count == 1
    assert "not completed" in result.poison_reasons[0]


def test_poison_unexpected_instrument() -> None:
    from fno_ai_paper_trading.models.enums import InstrumentType
    from fno_ai_paper_trading.models.instruments import Instrument

    other = Instrument(symbol="BANKNIFTY", instrument_type=InstrumentType.INDEX, underlying_symbol="BANKNIFTY")
    bar = MarketPrice(
        instrument=other,
        timestamp=VALID,
        open=Decimal("25000"),
        high=Decimal("25001"),
        low=Decimal("24999"),
        close=Decimal("25000"),
        volume=1,
    )
    result = _validate([bar])
    assert result.poison_count == 1
    assert "unexpected instrument" in result.poison_reasons[0]


# --------------------------------------------------------------------------- #
# 13-16: structural breakage of the usable stream is a hard error
# --------------------------------------------------------------------------- #

def test_hard_stop_duplicate_timestamps():
    with pytest.raises(TrackValidationError):
        _validate([_valid_bar(), _valid_bar()])


def test_hard_stop_non_chronological_stream():
    rev = [_valid_bar(VALID + timedelta(minutes=10)), _valid_bar(VALID)]
    with pytest.raises(TrackValidationError):
        _validate(rev, now=VALID + timedelta(minutes=31))


def test_hard_stop_bars_not_a_list():
    with pytest.raises(TrackValidationError):
        _validate(None)


def test_hard_stop_unknown_interval_rejected_at_construction():
    with pytest.raises(TrackValidationError):
        BarValidator(interval="1M")


# --------------------------------------------------------------------------- #
# 17-20: soft anomalies, prior-day history and engine consumption behaviour
# --------------------------------------------------------------------------- #

def test_gap_flagged_as_anomaly_not_error():
    b = _valid_bar(VALID + timedelta(minutes=30))
    result = _validate([_valid_bar(), b], now=VALID + timedelta(minutes=51))
    assert result.ok
    assert len(result.valid) == 2
    assert any("gap" in x for x in result.anomalies)


def test_prior_day_bars_are_history_not_poison_not_tradeable():
    friday = datetime(2026, 9, 18, 9, 15, 0)  # last Thursday is 17th; 18th is Friday
    result = _validate([_valid_bar(friday), _valid_bar()], now=VALID + timedelta(minutes=5))
    assert result.poison_count == 0
    assert len(result.prior) == 1
    assert len(result.valid) == 1


def test_future_dated_bar_is_poisoned():
    tomorrow = datetime(2026, 9, 22, 9, 15, 0)
    result = _validate([_valid_bar(tomorrow)], now=VALID + timedelta(minutes=5))
    assert result.poison_count == 1


def test_repeated_delivery_of_same_bar_is_idempotent(tmp_path):
    engine, _, _ = make_engine(tmp_path, days=[DAY0])
    ticks = day_ticks(DAY0)
    drive(engine, ticks)
    before = len(engine.broker.fills)
    engine.clock.set(ticks[-1])
    result = engine.step()
    assert result.status.value in ("SKIPPED", "IDLE", "CLOSED")
    assert len(engine.broker.fills) == before
    assert engine.counters["data_skips"] == []


class PoisonOnceFeed(SyntheticFeed):
    """Injects one toxic bar at the first completed batch, then behaves normally."""

    def __init__(self, feed: SyntheticFeed, broken_bar: MarketPrice) -> None:
        super().__init__(feed.instrument, feed.sessions)
        self._broken = broken_bar
        self._injected = False

    def bars_up_to(self, moment: datetime):
        bars = super().bars_up_to(moment)
        if bars and not self._injected:
            self._injected = True
            bars = [self._broken] + bars[1:]
        return bars


def test_poisoned_bar_never_reaches_strategy_or_orders(tmp_path):
    engine, _, feed = make_engine(tmp_path, days=[DAY0])
    engine.bars_source = PoisonOnceFeed(feed, _raw_bar(high=Decimal("1")))
    drive(engine, day_ticks(DAY0))

    # The toxic delivery was recorded as a data skip; the session still traded
    # exactly one round trip (same as a clean run) and ended flat.
    assert len(engine.counters["data_skips"]) == 1
    assert "high" in engine.counters["data_skips"][0]["reason"]
    assert len(engine.consumed) == 74  # one tick lagged behind the injected skip; nothing fabricated
    assert engine.counters["entries_filled"] == 1
    assert engine.counters["exits_filled"] == 1
    assert engine.position_quantity == 0
    assert engine.counters["errors"] == []
    # No short opened anywhere in the day.
    assert all(f.quantity > 0 for f in engine.broker.fills)


def test_trades_only_on_valid_clean_data_day(tmp_path):
    engine, _, _ = make_engine(tmp_path, days=[DAY0])
    drive(engine, day_ticks(DAY0))
    assert engine.broker.fills
    assert engine.counters["data_skips"] == []
    assert engine.position_quantity == 0