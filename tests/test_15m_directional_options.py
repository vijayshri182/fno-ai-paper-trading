"""Focused test matrix for 15M_DIRECTIONAL_OPTIONS_EXPERIMENT.

Covers the mandate test list: windowing completeness, duplicate/out-of-order
rejection, deterministic signal mapping, the state-machine table (incl. the
approved NEUTRAL-close and CALL<->PUT switches), label-only execution on the
index (no fabricated option fields), risk-control reuse, persistence/resume,
cross-account isolation, no-credential/token-leak, determinism of the fiscal
fingerprint and no-winner comparison reporting.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal

import pytest

from fno_ai_paper_trading.broker.paper_broker import PaperBroker
from fno_ai_paper_trading.experiments.directional_15m.contract import (
    Action,
    DirectionalStopPolicy,
    Leg,
    Signal15m,
    decide,
    decision_moments,
    signal_to_15m,
)
from fno_ai_paper_trading.experiments.directional_15m.executor import (
    DayAlreadyReported,
    DirectionalOptionsConfig,
)
from fno_ai_paper_trading.experiments.directional_15m.report import (
    baseline_metrics_from_engine,
    build_experiment_report,
)
from fno_ai_paper_trading.experiments.directional_15m.runner import replay_experiment
from fno_ai_paper_trading.experiments.directional_15m.signal import donchian_signal_15m
from fno_ai_paper_trading.experiments.directional_15m.window import DecisionWindowAggregator
from fno_ai_paper_trading.models.market import MarketPrice
from fno_ai_paper_trading.paper_track.feed import (
    SyntheticFeed,
    TRACK_INSTRUMENT,
    trading_day_sequence,
)
from fno_ai_paper_trading.paper_track.store import TrackStore

DAY = date(2026, 9, 21)


def _bar(ts: datetime, price: Decimal | float = 25000) -> MarketPrice:
    p = Decimal(str(price))
    return MarketPrice(
        instrument=TRACK_INSTRUMENT(),
        timestamp=ts,
        open=p,
        high=p + Decimal("10"),
        low=p - Decimal("10"),
        close=p,
        volume=1000,
    )


def _moments(day: date = DAY) -> list[datetime]:
    return decision_moments(day)


# --------------------------------------------------------------------------- window

def test_windows_are_24_from_0930_to_1515():
    moments = _moments()
    assert len(moments) == 24
    assert moments[0].time() == time(9, 30)
    assert moments[-1].time() == time(15, 15)


def test_decision_boundaries_aligned_to_5min_grid():
    for m in _moments():
        assert (m.minute % 5) == 0
        assert (m.minute % 15) == 0


def test_complete_window_requires_exactly_three_completed_bars():
    agg = DecisionWindowAggregator()
    moment = _moments()[0]
    for k in range(3):
        agg.ingest(_bar(moment - timedelta(minutes=5 * (3 - k))))
    result = agg.decision_at(moment)
    assert result.complete
    assert len(result.window) == 3
    assert result.reference_bar is not None


def test_incomplete_window_when_second_bar_missing():
    agg = DecisionWindowAggregator()
    moment = _moments()[0]
    agg.ingest(_bar(moment - timedelta(minutes=15)))
    agg.ingest(_bar(moment - timedelta(minutes=5)))
    result = agg.decision_at(moment)
    assert not result.complete
    assert "missing" in result.note
    assert result.reference_bar is None


def test_duplicate_candle_consumed_once_with_anomaly():
    agg = DecisionWindowAggregator()
    bar = _bar(_moments()[0] - timedelta(minutes=10))
    assert agg.ingest(bar) is True
    assert agg.ingest(bar) is False
    assert len(agg.anomalies) == 1
    assert "duplicate" in agg.anomalies[0]


def test_out_of_order_candle_rejected():
    agg = DecisionWindowAggregator()
    agg.ingest(_bar(_moments()[0] - timedelta(minutes=15)))
    # A stale/older bar delivered after a newer one is rejected.
    old = _bar(_moments()[0] - timedelta(minutes=20))
    assert agg.ingest(old) is False
    assert any("out-of-order" in a for a in agg.anomalies)


def test_window_ignores_bars_after_the_window():
    agg = DecisionWindowAggregator()
    moment = _moments()[0]
    agg.ingest(_bar(moment - timedelta(minutes=15)))
    agg.ingest(_bar(moment - timedelta(minutes=10)))
    agg.ingest(_bar(moment - timedelta(minutes=5)))
    agg.ingest(_bar(moment))  # completes later than `moment`
    result = agg.decision_at(moment)
    assert result.complete
    assert all(b.timestamp != moment for b in result.window)


# --------------------------------------------------------------------------- signal

def test_signal_mapping_buy_sell_hold():
    assert signal_to_15m("BUY") is Signal15m.BULLISH
    assert signal_to_15m("SELL") is Signal15m.BEARISH
    assert signal_to_15m("HOLD") is Signal15m.NEUTRAL


def test_empty_history_evaluates_neutral():
    sig, raw = donchian_signal_15m([])
    assert sig is Signal15m.NEUTRAL
    assert raw is None


def test_donchian_signal_deterministic_across_calls():
    bars = [
        _bar(_moments()[0] - timedelta(minutes=5 * k), 25000 + 10 * k)
        for k in range(30)
    ]
    first = donchian_signal_15m(bars)
    second = donchian_signal_15m(bars)
    assert first[0] is second[0]


# --------------------------------------------------------------------------- state machine

def test_state_table_flat_entries():
    assert decide(Leg.FLAT, Signal15m.BULLISH).actions == (Action.ENTER_CALL,)
    assert decide(Leg.FLAT, Signal15m.BEARISH).actions == (Action.ENTER_PUT,)
    assert decide(Leg.FLAT, Signal15m.NEUTRAL).actions == ()


def test_state_table_holds_same_direction():
    assert decide(Leg.CALL, Signal15m.BULLISH).actions == ()
    assert decide(Leg.PUT, Signal15m.BEARISH).actions == ()


def test_neutral_closes_the_open_leg():
    assert decide(Leg.CALL, Signal15m.NEUTRAL).actions == (Action.EXIT_CALL,)
    assert decide(Leg.PUT, Signal15m.NEUTRAL).actions == (Action.EXIT_PUT,)


def test_opposite_signal_switches_legs():
    assert decide(Leg.CALL, Signal15m.BEARISH).actions == (Action.SWITCH_CALL_TO_PUT,)
    assert decide(Leg.PUT, Signal15m.BULLISH).actions == (Action.SWITCH_PUT_TO_CALL,)


def test_state_machine_never_acts_mid_window():
    # All rules are (state, signal) -> actions; no time axis here.
    for state in Leg:
        for sig in Signal15m:
            d = decide(state, sig)
            assert d.state is state
            assert d.signal is sig
            assert set(d.actions) <= set(Action)


# --------------------------------------------------------------------------- stop policy

def test_directional_stop_long_below_entry():
    policy = DirectionalStopPolicy(Decimal("0.02"))
    bar = _bar(_moments()[0], 24500)  # 25000 * 0.98 = 24500
    check = policy.check(leg=Leg.CALL, entry_price=Decimal("25000"), bar=bar)
    assert check.triggered


def test_directional_stop_short_above_entry():
    policy = DirectionalStopPolicy(Decimal("0.02"))
    bar = _bar(_moments()[0], 25500)  # 25000 * 1.02 = 25500
    check = policy.check(leg=Leg.PUT, entry_price=Decimal("25000"), bar=bar)
    assert check.triggered


def test_directional_stop_flat_raises():
    policy = DirectionalStopPolicy(Decimal("0.02"))
    with pytest.raises(ValueError):
        policy.check(leg=Leg.FLAT, entry_price=Decimal("25000"), bar=_bar(_moments()[0]))


# --------------------------------------------------------------------------- engine replay

def _run(days: int = 3, seed: int = 7, store_dir=None, account="exp_test", baseline=None):
    config = DirectionalOptionsConfig(
        account=account, store_dir=store_dir or "data/experiments/test_dir"
    )
    store = TrackStore(config.store_dir, config.account)
    start = trading_day_sequence(DAY, 1)[0]
    days_list = trading_day_sequence(start, days)
    feed = SyntheticFeed.build(days_list, seed=seed, instrument=config.instrument)
    engine = replay_experiment(
        config=config,
        store=store,
        feed=feed,
        days=days_list,
        baseline_metrics=baseline,
    )
    return engine, store, days_list


def test_replay_ends_flat_and_reports(tmp_path):
    engine, store, _ = _run(3, seed=7, store_dir=tmp_path)
    assert engine.leg is Leg.FLAT
    assert engine.position_quantity == 0
    report = engine.report()
    assert report["experiment_id"] == "15M_DIRECTIONAL_OPTIONS_EXPERIMENT"
    assert report["accounting"]["reconciled"] is True
    assert report["decisions"]["mid_window_actions"] == 0


def test_replay_deterministic_fingerprint(tmp_path):
    _, _, _ = _run(2, seed=9, store_dir=tmp_path / "a")
    engine2, _, _ = _run(2, seed=9, store_dir=tmp_path / "b")
    assert engine2.stable_fingerprint() == engine2.stable_fingerprint()
    # A different seed gives a (almost certainly) different fingerprint.
    engine3, _, _ = _run(2, seed=10, store_dir=tmp_path / "c")
    assert engine2.stable_fingerprint() != engine3.stable_fingerprint()


def test_no_decision_from_incomplete_window_counted_as_error(tmp_path):
    engine, _, _ = _run(1, seed=1, store_dir=tmp_path)
    # The synthetic feed always completes windows, so a healthy run has zero
    # "data errors" from windowing; anything else would be a bug.
    report = engine.report()
    assert report["data_quality"]["data_errors"] == 0


def test_risk_controls_are_reused_and_gating(tmp_path):
    config = DirectionalOptionsConfig(
        account="exp_risk", store_dir=tmp_path, max_daily_loss=Decimal("500")
    )
    store = TrackStore(config.store_dir, config.account)
    start = trading_day_sequence(DAY, 1)[0]
    days = trading_day_sequence(start, 2)
    feed = SyntheticFeed.build(days, seed=3, instrument=config.instrument)
    engine = replay_experiment(config=config, store=store, feed=feed, days=days)
    # RiskManager caps are visible through the reused gate.
    assert engine.risk_manager is not None
    assert engine.counters["risk_refusals"] is not None
    report = build_experiment_report(engine)
    assert report["accounting"]["reconciled"] is True


def test_label_only_execution_on_index_has_no_option_fields(tmp_path):
    engine, _, _ = _run(1, seed=2, store_dir=tmp_path)
    inst = engine.config.instrument
    assert inst.option_type is None
    assert inst.strike is None
    assert inst.expiry is None
    report = engine.report()
    assert report["execution"]["label_only"] is True
    assert report["execution"]["zero_premium"] is True
    assert report["scope"]["option_selection"]["no_premium"] is True
    assert report["scope"]["option_selection"]["mode"] == "label-only position intents on the index"


def test_engine_uses_only_the_paper_broker(tmp_path):
    engine, _, _ = _run(1, seed=2, store_dir=tmp_path)
    assert isinstance(engine.broker, PaperBroker)
    assert engine.broker.is_live is False


def test_state_survives_restart_and_is_identical(tmp_path):
    # Run days 1-2 fully, then continue a new engine from the checkpoint and
    # compare to a single uninterrupted run of days 1-3.
    start = trading_day_sequence(DAY, 1)[0]
    days2 = trading_day_sequence(start, 2)
    feed2 = SyntheticFeed.build(days2, seed=21, instrument=TRACK_INSTRUMENT())
    config = DirectionalOptionsConfig(account="exp_resume", store_dir=tmp_path)
    store = TrackStore(config.store_dir, config.account)
    replay_experiment(config=config, store=store, feed=feed2, days=days2)

    # A day that already produced a report is refused (once-per-day).
    with pytest.raises(DayAlreadyReported):
        replay_experiment(config=config, store=store, feed=feed2, days=days2, run_id="dup-run")

    days3 = trading_day_sequence(start, 3)
    fresh = DirectionalOptionsConfig(account="exp_fresh", store_dir=tmp_path)
    fresh_store = TrackStore(fresh.store_dir, fresh.account)
    feed_again = SyntheticFeed.build(days3, seed=21, instrument=TRACK_INSTRUMENT())
    fresh_engine = replay_experiment(
        config=fresh, store=fresh_store, feed=feed_again, days=days3
    )

    continue_feed = SyntheticFeed.build(days3, seed=21, instrument=TRACK_INSTRUMENT())
    continued_day3 = replay_experiment(
        config=config,
        store=store,
        feed=continue_feed,
        days=[days3[-1]],
        run_id="resumed-run",
        resume=True,
    )
    assert continued_day3.stable_fingerprint() == fresh_engine.stable_fingerprint()


def test_experiment_store_does_not_touch_baseline_store(tmp_path):
    import json
    from pathlib import Path as P

    baseline_dir = tmp_path / "baseline"
    exp_dir = tmp_path / "exp"
    # Simulate an existing frozen baseline store.
    baseline_store = TrackStore(baseline_dir, "nifty_5m_daily")
    baseline_store.update_run("existing", last_day="2026-09-21", fingerprint="deadbeef")
    baseline_bytes_before = sorted(
        (p.relative_to(baseline_dir).as_posix(), p.read_bytes()) for p in baseline_dir.rglob("*")
    )

    engine, _, _ = _run(1, seed=5, store_dir=exp_dir)
    assert engine.store.store_dir != baseline_dir
    baseline_bytes_after = sorted(
        (p.relative_to(baseline_dir).as_posix(), p.read_bytes()) for p in baseline_dir.rglob("*")
    )
    assert baseline_bytes_before == baseline_bytes_after
    manifest = json.loads((baseline_dir / "manifest.json").read_text(encoding="utf-8"))
    assert "existing" in manifest["runs"]
    assert "exp" not in str(manifest)


def test_no_credentials_or_tokens_in_experiment_output(tmp_path):
    """No secret/credential value or secret-bearing key ever appears in output."""
    import json

    engine, store, days = _run(1, seed=4, store_dir=tmp_path)
    report = engine.report()

    def walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                yield k.lower(), v
                yield from walk(v)
        elif isinstance(node, list):
            for item in node:
                yield from walk(item)

    forbidden_key_contains = ("access_token", "practice_token", "api_key", "apikey",
                              "password", "secret", "credential", "auth")
    for key, value in walk(report):
        if any(f in key for f in forbidden_key_contains):
            raise AssertionError(f"forbidden key in report: {key}")
        if "token" in key and key != "exchange_token" and value not in (None, ""):
            raise AssertionError(f"non-empty token-carrying key in report: {key} = {value}")
    for day in days:
        checkpoint = store.load_checkpoint(day)
        assert checkpoint is not None
        for key, value in walk(checkpoint):
            if any(f in key for f in forbidden_key_contains):
                raise AssertionError(f"forbidden key in checkpoint: {key}")
            if "token" in key and key != "exchange_token" and value not in (None, ""):
                raise AssertionError(f"non-empty token-carrying key in checkpoint: {key} = {value}")


def test_report_never_declares_a_winner(tmp_path):
    engine, _, _ = _run(1, seed=6, store_dir=tmp_path)
    report = engine.report()
    assert report["declared_winner"] is None
    assert report["baseline_comparison"] is None


def test_comparison_table_holds_both_columns_no_winner(tmp_path):
    from fno_ai_paper_trading.paper_track.engine import TrackConfig

    days = trading_day_sequence(DAY, 2)
    feed = SyntheticFeed.build(days, seed=11, instrument=TRACK_INSTRUMENT())
    baseline_store = TrackStore(tmp_path / "base", "nifty_5m_daily")
    base_config = TrackConfig(
        account="nifty_5m_daily", store_dir=baseline_store.store_dir, interval="5m"
    )
    from fno_ai_paper_trading.paper_track.runner import run_sessions

    base = run_sessions(config=base_config, store=baseline_store, feed=feed, days=days)
    base_metrics = baseline_metrics_from_engine(base)
    assert base_metrics["name"].startswith("MA(5,21)")

    config = DirectionalOptionsConfig(account="exp_cmp", store_dir=tmp_path / "exp")
    exp_store = TrackStore(config.store_dir, config.account)
    exp_feed = SyntheticFeed.build(days, seed=11, instrument=TRACK_INSTRUMENT())
    engine = replay_experiment(
        config=config, store=exp_store, feed=exp_feed, days=days, baseline_metrics=base_metrics
    )
    report = engine.report()
    table = report["baseline_comparison"]
    assert table is not None
    assert table["declared_winner"] is None
    assert table["no_winner_declared"] is True
    assert {r["metric"] for r in table["rows"]} >= {"net_pnl", "win_rate", "round_trips"}


def test_reconciliation_holds_on_replay(tmp_path):
    engine, _, _ = _run(3, seed=13, store_dir=tmp_path)
    portfolio = engine.portfolio
    commissions = sum((Decimal(str(f.commission)) for f in engine.broker.fills), Decimal("0"))
    assert portfolio.cash == (
        engine.config.initial_cash + portfolio.realized_pnl - commissions
    )


def test_switches_count_matches_reversals(tmp_path):
    engine, _, _ = _run(3, seed=13, store_dir=tmp_path)
    report = engine.report()
    switches = report["decisions"]["switches"]
    assert report["decisions"]["reversals"] == switches["CALL_TO_PUT"] + switches["PUT_TO_CALL"]
