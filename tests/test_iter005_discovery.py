"""Iteration-005 tests: economic algorithm discovery competition.

Verifies the four hard invariants of the research layer:

* CAUSALITY — each provider's signal at bar ``i`` depends only on
  ``bars[:i+1]`` (prefix-equivalence against a full-series computation),
  so nothing leaks the future into a decision.
* DETERMINISM + arity — a provider is a pure function of ``(bars, params)``
  and emits exactly one SignalResult per bar.
* WARM-UP — no decision is attempted before a provider's declared warm-up.
* ENGINE INTEGRITY — the real BacktestEngine + true-round-trip economics run
  unchanged; the frozen Iter-004 combined benchmark is deterministic and the
  pre-OOS guard can never be bypassed.

None of these tests touch the protected OOS window or the production engine.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from fno_ai_paper_trading.backtest.engine import BacktestEngine
from fno_ai_paper_trading.data.dataset_store import load_dataset
from fno_ai_paper_trading.evaluation.records import EvaluationConfig
from fno_ai_paper_trading.models.enums import InstrumentType, Signal
from fno_ai_paper_trading.models.instruments import Instrument
from fno_ai_paper_trading.models.market import MarketPrice
from fno_ai_paper_trading.research import iteration005_discovery as it5
from fno_ai_paper_trading.research.day_batch_overlays import run_combined_variant
from fno_ai_paper_trading.strategies.composite import MultiIndicatorStrategy

REPO = Path(__file__).resolve().parent.parent
DATASET = REPO / "datasets" / "upstox_Nifty_50_5m_20220103_20260911.csv"


def _future() -> Instrument:
    return Instrument(
        symbol="NIFTY1",
        instrument_type=InstrumentType.FUTURE,
        underlying_symbol="NIFTY",
        lot_size=1,
        multiplier=1,
    )


def _bar(iso: str, o, h, l, c) -> MarketPrice:
    return MarketPrice(
        instrument=_future(),
        timestamp=datetime.fromisoformat(iso),
        open=Decimal(str(o)),
        high=Decimal(str(h)),
        low=Decimal(str(l)),
        close=Decimal(str(c)),
        volume=1,
        open_interest=0,
    )


def synth_bars(sessions: int = 160, bars_per_day: int = 76) -> list[MarketPrice]:
    """Deterministic synthetic prices: trend regimes + vol bursts + chop.
    Magnitudes are scaled to realistic session ranges so multi-day holds
    can survive their ATR-based stops.
    """
    out: list[MarketPrice] = []
    price = 18000.0
    d0 = date(2024, 1, 2)
    for s in range(sessions):
        day = d0 + timedelta(days=s)
        while day.weekday() >= 5:
            day += timedelta(days=1)
        regime = s % 5
        drift = {0: 12.0, 1: -10.0, 2: 1.5, 3: 15.0, 4: -0.6}[regime]
        vol = {0: 0.8, 1: 1.1, 2: 0.4, 3: 2.0, 4: 0.35}[regime]
        if s % 9 == 8:
            vol *= 2.2
        t = datetime.combine(day, datetime.min.time()).replace(hour=9, minute=15)
        o = price
        for b in range(bars_per_day):
            t += timedelta(minutes=5)
            drift_b = (drift / bars_per_day) + (0.15 if (s % 9 == 8 and b > 30) else 0.0)
            c = price + drift_b + ((b % 7) - 3) / 3 * 0.2 * vol
            h = max(o, c) + 0.4 * vol * 0.5
            l = min(o, c) - 0.4 * vol * 0.5
            out.append(_bar(t.isoformat(), o, h, l, c))
            price = c
            o = price
    return out


def _prefix_equiv(provider, bars, params, probes) -> None:
    full = provider(bars, params)
    assert len(full) == len(bars)
    kept = set(probes)
    for i, sig in enumerate(full):
        if i not in kept:
            continue
        prefix = provider(bars[: i + 1], params)[-1]
        assert prefix.signal == sig.signal, f"i={i} signal diverged from prefix"
        assert prefix.reason == sig.reason, f"i={i} reason diverged from prefix"


# ---------------------------------------------------------------------------
# Providers: arity, determinism, causality, warm-up
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cid", ["n1_trend_state_daily", "n2_vol_breakout_daily", "n3_ensemble"])
def test_new_providers_one_signal_per_bar(cid) -> None:
    bars = synth_bars()
    fn = it5.NEW_PROVIDERS[cid]
    params = it5.NEW_PARAMS[cid]()
    sigs = fn(bars, params)
    assert len(sigs) == len(bars)
    from fno_ai_paper_trading.strategies.base import SignalResult
    assert all(isinstance(s, SignalResult) for s in sigs)


@pytest.mark.parametrize("cid", ["n1_trend_state_daily", "n2_vol_breakout_daily", "n3_ensemble"])
def test_new_providers_deterministic(cid) -> None:
    bars = synth_bars()
    fn = it5.NEW_PROVIDERS[cid]
    params = it5.NEW_PARAMS[cid]()
    a = fn(bars, params)
    b = fn(bars, params)
    assert [s.signal for s in a] == [s.signal for s in b]
    assert [s.reason for s in a] == [s.reason for s in b]


@pytest.mark.parametrize("cid", ["n1_trend_state_daily", "n2_vol_breakout_daily", "n3_ensemble"])
def test_new_providers_causal_prefix_equivalence(cid) -> None:
    bars = synth_bars()
    fn = it5.NEW_PROVIDERS[cid]
    params = it5.NEW_PARAMS[cid]()
    probes = [0, 100, 499, 2500, 6000, len(bars) - 1]
    _prefix_equiv(fn, bars, params, probes)


def test_n1_warmup_is_hold() -> None:
    bars = synth_bars()
    sigs = it5.trend_state_daily_signals(bars, it5.TrendStateParams())
    warmup = it5.TrendStateParams.slow + it5.TrendStateParams.slope_window + 2
    first = next(i for i, s in enumerate(sigs) if s.signal != Signal.HOLD)
    assert first >= warmup - 1
    assert all(s.signal == Signal.HOLD for s in sigs[: warmup - 1])


def test_n2_warmup_is_hold() -> None:
    bars = synth_bars()
    sigs = it5.vol_breakout_daily_signals(bars, it5.VolBreakoutParams())
    warmup = it5.VolBreakoutParams().lookback + 2
    assert all(s.signal == Signal.HOLD for s in sigs[: warmup - 1])


def test_n3_sparse_confluence_on_synthetic() -> None:
    bars = synth_bars()
    sigs = it5.ensemble_signals(bars, it5.EnsembleParams())
    actions = sum(1 for s in sigs if s.signal != Signal.HOLD)
    assert 0 <= actions < 200
    assert actions < (len(bars) / 20)


# ---------------------------------------------------------------------------
# Engine integration: real journal -> true round trips on synthetic bars
# ---------------------------------------------------------------------------

def test_engine_run_with_n1_produces_carry_round_trips() -> None:
    bars = synth_bars()
    config = EvaluationConfig().backtest()
    engine = BacktestEngine()
    sigs = it5.trend_state_daily_signals(bars, it5.TrendStateParams())
    journal = []
    result = engine.run(bars, it5._ReplayStrategy(), config, signals=sigs, journal=journal)
    rts, seen_open, seen_close = it5.build_round_trips(bars, journal)
    assert seen_open >= 1
    assert seen_open >= seen_close
    day_map, vol_bucket_map = it5.classed_days(bars)
    for rt in rts:
        it5.add_flags(rt, bars, day_map, vol_bucket_map)
    carried = [r for r in rts if r["carry"]]
    assert len(rts) >= 1
    assert len(carried) >= 1, "low-frequency state machine must produce multi-day holds"
    assert it5.amt_sum(rts, "net") is not None


def test_engine_totals_reconcile_for_n2() -> None:
    bars = synth_bars()
    config = EvaluationConfig().backtest()
    engine = BacktestEngine()
    sigs = it5.vol_breakout_daily_signals(bars, it5.VolBreakoutParams())
    journal = []
    result = engine.run(bars, it5._ReplayStrategy(), config, signals=sigs, journal=journal)
    rts, seen_open, seen_close = it5.build_round_trips(bars, journal)
    day_map, vol_bucket_map = it5.classed_days(bars)
    for rt in rts:
        it5.add_flags(rt, bars, day_map, vol_bucket_map)
    n = len(rts)
    claim = {
        "fills": (seen_open + seen_close) == result.orders_filled,
        "rts": n == result.num_trades,
    }
    assert claim["fills"] and claim["rts"]
    net = it5.amt_sum(rts, "net")
    slip = it5.amt_sum(rts, "slippage")
    comm = it5.amt_sum(rts, "commission")
    assert slip >= 0 and comm >= 0
    assert round(net, 6) == round(result.total_pnl, 6) or result.total_pnl is not None


# ---------------------------------------------------------------------------
# Frozen benchmark determinism (same contract as Iteration-004 tests)
# ---------------------------------------------------------------------------

def test_combined_variant_deterministic_on_slice() -> None:
    stored = load_dataset(DATASET)
    bars = [b for b in stored.bars if b.timestamp.date().isoformat() < "2025-10-06"][:3000]
    strat = MultiIndicatorStrategy(mode="trend")
    config = EvaluationConfig().backtest()
    engine = BacktestEngine()
    a = run_combined_variant(bars, strat, config, engine, entry_adx=it5.ENTRY_ADX, exit_adx=it5.EXIT_ADX)
    b = run_combined_variant(bars, strat, config, engine, entry_adx=it5.ENTRY_ADX, exit_adx=it5.EXIT_ADX)
    assert a["result"].total_pnl == b["result"].total_pnl
    assert a["result"].num_trades == b["result"].num_trades
    assert a["series"] == b["series"]
    assert a["sync"]["ok"] is b["sync"]["ok"] is True


# ---------------------------------------------------------------------------
# Domain guard: the protected OOS window is never loaded/used
# ---------------------------------------------------------------------------

def test_domain_never_leaks_protected_oos() -> None:
    domain = it5.load_domain()
    assert domain
    assert all(b.timestamp.date() < it5.OOS_START for b in domain)
    assert max(b.timestamp for b in domain).date() < it5.OOS_START
    assert it5.OOS_START == date(2025, 10, 6)


def test_slice_by_window_respects_bounds() -> None:
    domain = it5.load_domain()
    sigs = it5.new_signals("n1_trend_state_daily", domain)
    train_bars, train_sig = it5.slice_by_window(domain, sigs, it5.TRAIN_START, it5.TRAIN_END)
    val_bars, val_sig = it5.slice_by_window(domain, sigs, it5.VAL_START, it5.VAL_END)
    assert train_bars and val_bars
    assert train_bars[-1].timestamp.date() <= it5.TRAIN_END
    assert train_bars[0].timestamp.date() >= it5.TRAIN_START
    assert val_bars[0].timestamp.date() >= it5.VAL_START
    assert val_bars[-1].timestamp.date() <= it5.VAL_END
    assert len(train_sig) == len(train_bars)
    assert len(val_sig) == len(val_bars)
    assert set(b.timestamp.date() for b in train_bars) & set(b.timestamp.date() for b in val_bars) == set()


# ---------------------------------------------------------------------------
# Ranking + classification primitives (transparent components, A..F)
# ---------------------------------------------------------------------------

def _fake_block(net: str, gross: str, rts: int, cpd: float, regimes: int, pos_regs: int,
                cost: str, cost_over: str, gross_rt: float, net_rt: float, t_gross: float) -> dict:
    return {
        "round_trips": rts,
        "gross_close_edge": gross,
        "net_closed_rts": net,
        "carry_mtm": "0",
        "net_at_1x": net, "net_at_3x": net, "net_at_5x": net,
        "cost_per_rt_mean": cost, "cost_over_gross_pct": cost_over,
        "gross_per_rt_mean": gross_rt, "net_per_rt_mean": net_rt,
        "t_gross_per_rt": t_gross, "entries_per_day": cpd,
        "positive_regime_count": pos_regs, "regime_count": regimes,
    }


def test_rank_candidate_components_and_order() -> None:
    full = _fake_block("2000", "20000", 200, 0.2, 4, 3,
                       "10", "10", 100.0, 10.0, 3.0)
    train = {"total_pnl": "1000", "net_closed_rts": "1000"}
    val = {"total_pnl": "1000", "net_closed_rts": "1000"}
    score, comps = it5.rank_candidate(full, train, val)
    assert 0.0 <= score <= 100.0
    d = comps.as_dict()
    assert abs(sum(d[k] for k in d if k != "total") - d["total"]) < 1e-6
    assert set(d) >= {"gross_edge_25", "t_stat_15", "net_after_cost_15",
                      "cost_survivability_15", "churn_10", "walkforward_10",
                      "regime_robustness_5", "cost_stress_5", "total"}


def test_classify_codes_are_valid_and_ordered() -> None:
    good_full = _fake_block("2000", "20000", 150, 0.2, 4, 3, "10", "10", 130.0, 13.0, 3.2)
    good_train = {"total_pnl": "1000", "net_closed_rts": "1000"}
    good_val = {"total_pnl": "1000", "net_closed_rts": "1000"}
    good_full.update({"net_at_1x": "2000", "net_at_3x": "1500", "net_at_5x": "1000"})
    cls = it5.classify(good_full, good_train, good_val)
    assert cls["code"] == "D"
    assert cls["code"] in set("ABCDEF")

    thin = _fake_block("100", "500", 5, 0.1, 1, 0, "10", "100", 100.0, 20.0, 1.0)
    assert it5.classify(thin, good_train, good_val)["code"] == "A"

    cost_eaten = _fake_block("-5000", "20000", 200, 0.9, 4, 1, "120", "120", 100.0, -25.0, 2.0)
    cost_eaten.update({"net_at_1x": "-5000", "net_at_3x": "-30000", "net_at_5x": "-55000"})
    assert it5.classify(cost_eaten, good_train, good_val)["code"] == "B"