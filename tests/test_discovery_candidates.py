"""Contract tests for the discovery candidate providers (d1..d6).

Each candidate is a pure, decision-time, daily-reset signal provider: BUY/SELL/
HOLD with the frozen NO_TRADE reason codes. These tests pin the invariants that
make the discovery engine's results reconstructible and free of look-ahead.
"""
from __future__ import annotations

import pytest

from fno_ai_paper_trading.models.enums import Signal, InstrumentType
from fno_ai_paper_trading.models.instruments import Instrument
from fno_ai_paper_trading.models.market import MarketPrice
from fno_ai_paper_trading.strategies import discovery_candidates as dc


def _nifty() -> Instrument:
    return Instrument(
        symbol="Nifty 50",
        instrument_type=InstrumentType.INDEX,
        underlying_symbol="NIFTY",
        lot_size=1,
        multiplier=1,
    )


# The 13 frozen decision codes reject a tradeable opportunity; WARM_UP / HOLDING
# / TIME_NOT_OPEN are the documented mechanical state NO-TRADEs (indicator
# warm-up, flat-until-exit holding, outside the entry window). Together they are
# the only HOLD reasons a provider may emit.
NO_TRADE_REASON_CODES = {
    "SIDEWAYS_MARKET", "WEAK_TREND", "LOW_VOLATILITY", "EXTREME_VOLATILITY",
    "NO_PULLBACK", "NO_MOMENTUM", "NO_BREAKOUT", "POOR_RISK_REWARD",
    "INSUFFICIENT_EXPECTED_EDGE", "HIGH_COST", "TIME_CUTOFF",
    "RISK_LIMIT", "DAILY_LOSS_LIMIT",
    "WARM_UP", "HOLDING", "TIME_NOT_OPEN",
}


def _make_bars(n_days: int, bars_per_day: int = 75, start="2025-01-06", seed: int = 0) -> list[MarketPrice]:
    """Synthetic intraday bars: alternating trend/range regimes, deterministic."""
    import random
    from datetime import date, datetime, time, timedelta
    from decimal import Decimal

    rng = random.Random(seed)
    base = date(*map(int, start.split("-")))

    bars: list[MarketPrice] = []
    for d in range(n_days):
        day = base + timedelta(days=d)
        # skip weekends for realism
        if day.weekday() >= 5:
            continue
        open_price = 100.0 + d * 0.2 + seed
        for i in range(bars_per_day):
            ts = datetime.combine(day, time(9, 15)) + timedelta(minutes=5 * i)
            drift = 0.02 if (d % 3 != 1) else -0.015
            open_price += drift + rng.uniform(-0.05, 0.05)
            close = open_price + rng.uniform(-0.1, 0.1)
            bars.append(MarketPrice(
                instrument=_nifty(),
                timestamp=ts,
                open=Decimal(str(round(open_price, 2))),
                high=Decimal(str(round(max(open_price, close) + 0.05 + abs(rng.uniform(-0.1, 0.1)), 2))),
                low=Decimal(str(round(min(open_price, close) - 0.05 - abs(rng.uniform(-0.1, 0.1)), 2))),
                close=Decimal(str(round(close, 2))),
                volume=int(rng.uniform(1000, 5000)),
            ))
    return bars


@pytest.fixture(scope="module")
def bars() -> list[MarketPrice]:
    return _make_bars(n_days=12, bars_per_day=75, seed=0)


@pytest.mark.parametrize("cid", sorted(dc.PROVIDERS))
def test_provider_len_matches_bars(cid: str, bars: list[MarketPrice]) -> None:
    signals = dc.PROVIDERS[cid](bars, dc.PARAMS[cid]())
    assert len(signals) == len(bars)


@pytest.mark.parametrize("cid", sorted(dc.PROVIDERS))
def test_provider_deterministic(cid: str, bars: list[MarketPrice]) -> None:
    params = dc.PARAMS[cid]()
    first = dc.PROVIDERS[cid](bars, params)
    second = dc.PROVIDERS[cid](bars, dc.PARAMS[cid]())
    assert [s.signal for s in first] == [s.signal for s in second]


@pytest.mark.parametrize("cid", sorted(dc.PROVIDERS))
def test_signals_raise_only_frozen_reasons(cid: str, bars: list[MarketPrice]) -> None:
    for signal in dc.PROVIDERS[cid](bars, dc.PARAMS[cid]()):
        if signal.signal == Signal.HOLD:
            assert signal.reason in NO_TRADE_REASON_CODES, f"{cid}: unknown reason {signal.reason!r}"


@pytest.mark.parametrize("cid", sorted(dc.PROVIDERS))
def test_no_lookahead_decision_time_contract(cid: str, bars: list[MarketPrice]) -> None:
    params = dc.PARAMS[cid]()
    full = dc.PROVIDERS[cid](bars, params)
    # the signal for bar i must depend only on bars[:i+1] — the exact contract
    # the production engine enforces and the daily replay slices use.
    for i in range(0, len(bars), 9):
        prefix_signal = dc.PROVIDERS[cid](bars[: i + 1], params)[-1]
        assert prefix_signal.signal == full[i].signal, f"{cid}: look-ahead at index {i}"
        assert prefix_signal.timestamp == full[i].timestamp


@pytest.mark.parametrize("cid", sorted(dc.PROVIDERS))
def test_strategy_analyze_matches_provider_last_signal(cid: str, bars: list[MarketPrice]) -> None:
    params = dc.PARAMS[cid]()
    strategy = dc.STRATEGIES[cid](params)
    last = dc.PROVIDERS[cid](bars, params)[-1]
    via_oracle = strategy.analyze(bars)
    assert via_oracle.signal == last.signal
    assert via_oracle.reason == last.reason


@pytest.mark.parametrize("cid", sorted(dc.PROVIDERS))
def test_all_warmup_early_bars_hold(cid: str, bars: list[MarketPrice]) -> None:
    signals = dc.PROVIDERS[cid](bars, dc.PARAMS[cid]())
    # indicators (EMA/RSI/ATR/Donchian/Bollinger) are undefined at the very start.
    for i in range(2):
        assert signals[i].signal == Signal.HOLD, f"{cid}: bar {i} fired before warm-up"


@pytest.mark.parametrize("cid", sorted(dc.PROVIDERS))
def test_provider_timestamps_align_with_bars(cid: str, bars: list[MarketPrice]) -> None:
    signals = dc.PROVIDERS[cid](bars, dc.PARAMS[cid]())
    for signal, bar in zip(signals, bars):
        assert signal.timestamp == bar.timestamp


@pytest.mark.parametrize("cid", sorted(dc.PROVIDERS))
def test_unknown_knob_rejected(cid: str) -> None:
    params_cls = dc.PARAMS[cid]
    with pytest.raises(TypeError):
        params_cls(bogus_knob=1)


def test_providers_are_six_distinct_families() -> None:
    assert set(dc.PROVIDERS) == {
        "d1_trend_ema", "d2_momentum", "d3_breakout_vol",
        "d4_pullback", "d5_mean_reversion", "d6_structure",
    }
    # each family is a different provider function, not a parameter variant
    assert len({id(fn) for fn in dc.PROVIDERS.values()}) == 6


def test_signal_result_actionable_contract(bars: list[MarketPrice]) -> None:
    for cid in dc.PROVIDERS:
        for signal in dc.PROVIDERS[cid](bars, dc.PARAMS[cid]()):
            assert signal.actionable == (signal.signal in (Signal.BUY, Signal.SELL))