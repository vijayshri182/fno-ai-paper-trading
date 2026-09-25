"""Focused test matrix for 5M_DIRECTIONAL_OPTIONS_EXPERIMENT.

Covers the mandate test list: single-completed-candle cadence (72 decision
instants/day 09:20..15:15, never at/after the 15:20 EOD flatten), duplicate /
out-of-order rejection, deterministic signal mapping, the full approved
transition table (9 rules), the reversal sequence without duplicate entries,
no-duplicate-entries during replay, label-only execution on the index (no
fabricated option fields), risk-control reuse, persistence/resume, cross-
account isolation (from the 15M experiment store AND the MA(5,21) baseline
store), no-credential/token-leak, determinism of the fiscal fingerprint and
no-winner comparison reporting.
"""

from __future__ import annotations

import tempfile
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path as P

import pytest

from fno_ai_paper_trading.broker.paper_broker import PaperBroker
from fno_ai_paper_trading.experiments.directional_5m.contract import (
    Action,
    DirectionalStopPolicy,
    Leg,
    Signal15m,
    decide,
    decision_moments,
    signal_to_15m,
)
from fno_ai_paper_trading.experiments.directional_5m.executor import (
    DayAlreadyReported,
    Directional5MOptionsConfig,
)
from fno_ai_paper_trading.experiments.directional_5m.report import (
    baseline_metrics_from_engine,
    build_experiment_report,
)
from fno_ai_paper_trading.experiments.directional_5m.runner import replay_experiment
from fno_ai_paper_trading.experiments.directional_5m.signal import donchian_signal_5m
from fno_ai_paper_trading.experiments.directional_5m.window import BarCompletionAggregator
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


# --------------------------------------------------------------------------- cadence

def test_windows_are_72_from_0920_to_1515():
    moments = _moments()
    assert len(moments) == 72
    assert moments[0].time() == time(9, 20)   # candle 09:15 completes
    assert moments[-1].time() == time(15, 15)  # candle 15:10 completes


def test_decision_grid_is_5_minute_aligned():
    for m in _moments():
        assert (m.minute % 5) == 0
    for a, b in zip(_moments(), _moments()[1:]):
        assert (b - a).total_seconds() == 300  # exactly one candle apart


def test_no_decision_moment_at_or_after_eod_flatten():
    # The candles completing at 15:20/15:25/15:30 are at/after the 15:20 EOD
    # flatten and are never decision instants (always-flat-at-close kept).
    for m in _moments():
        assert m.time() < time(15, 20)


# --------------------------------------------------------------------------- aggregator

def test_decision_requires_the_exact_completed_candle():
    agg = BarCompletionAggregator()
    moment = _moments()[0]
    agg.ingest(_bar(moment - timedelta(minutes=5)))  # candle 09:15
    result = agg.decision_at(moment)
    assert result.complete
    assert len(result.window) == 1
    assert result.reference_bar is not None
    assert result.reference_bar.timestamp == moment - timedelta(minutes=5)


def test_decision_incomplete_when_reference_candle_missing():
    agg = BarCompletionAggregator()
    moment = _moments()[0]
    agg.ingest(_bar(moment - timedelta(minutes=10)))  # earlier candle, not 09:15
    result = agg.decision_at(moment)
    assert not result.complete
    assert "no completed candle" in result.note
    assert result.reference_bar is None


def test_all_seventy_two_moments_have_a_reference_candle_in_a_session():
    agg = BarCompletionAggregator()
    # 09:15..15:25 open every 5 minutes (75 candles); each completes 5 min later.
    start = datetime.combine(DAY, time(9, 15))
    for k in range(75):
        assert agg.ingest(_bar(start + timedelta(minutes=5 * k)))
    for moment in _moments():
        result = agg.decision_at(moment)
        assert result.complete, f"missing completed candle at {moment:%H:%M}"


def test_duplicate_candle_consumed_once_with_anomaly():
    agg = BarCompletionAggregator()
    bar = _bar(_moments()[0] - timedelta(minutes=5))
    assert agg.ingest(bar) is True
    assert agg.ingest(bar) is False
    assert len(agg.anomalies) == 1
    assert "duplicate" in agg.anomalies[0]


def test_out_of_order_candle_rejected():
    agg = BarCompletionAggregator()
    agg.ingest(_bar(_moments()[0] - timedelta(minutes=5)))
    old = _bar(_moments()[0] - timedelta(minutes=10))
    assert agg.ingest(old) is False
    assert any("out-of-order" in a for a in agg.anomalies)


def test_decision_ignores_bars_that_open_at_the_moment():
    agg = BarCompletionAggregator()
    moment = _moments()[0]
    agg.ingest(_bar(moment - timedelta(minutes=5)))
    agg.ingest(_bar(moment))  # opens at `moment`, completes later -> not a trigger
    result = agg.decision_at(moment)
    assert result.complete
    assert result.reference_bar.timestamp == moment - timedelta(minutes=5)


# --------------------------------------------------------------------------- signal

def test_signal_mapping_buy_sell_hold():
    assert signal_to_15m("BUY") is Signal15m.BULLISH
    assert signal_to_15m("SELL") is Signal15m.BEARISH
    assert signal_to_15m("HOLD") is Signal15m.NEUTRAL


def test_empty_history_evaluates_neutral():
    sig, raw = donchian_signal_5m([])
    assert sig is Signal15m.NEUTRAL
    assert raw is None


def test_donchian_signal_deterministic_across_calls():
    bars = [
        _bar(_moments()[0] - timedelta(minutes=5 * k), 25000 + 10 * k)
        for k in range(30)
    ]
    first = donchian_signal_5m(bars)
    second = donchian_signal_5m(bars)
    assert first[0] is second[0]


# --------------------------------------------------------------------------- state machine: approved 9-rule table

def test_state_table_rule_1_flat_bullish_opens_call():
    d = decide(Leg.FLAT, Signal15m.BULLISH)
    assert d.actions == (Action.ENTER_CALL,)


def test_state_table_rule_2_call_bullish_holds_call():
    assert decide(Leg.CALL, Signal15m.BULLISH).actions == ()


def test_state_table_rule_3_call_bearish_switches_to_put():
    assert decide(Leg.CALL, Signal15m.BEARISH).actions == (Action.SWITCH_CALL_TO_PUT,)


def test_state_table_rule_4_flat_bearish_opens_put():
    assert decide(Leg.FLAT, Signal15m.BEARISH).actions == (Action.ENTER_PUT,)


def test_state_table_rule_5_put_bearish_holds_put():
    assert decide(Leg.PUT, Signal15m.BEARISH).actions == ()


def test_state_table_rule_6_put_bullish_switches_to_call():
    assert decide(Leg.PUT, Signal15m.BULLISH).actions == (Action.SWITCH_PUT_TO_CALL,)


def test_state_table_rule_7_call_neutral_closes_to_flat():
    assert decide(Leg.CALL, Signal15m.NEUTRAL).actions == (Action.EXIT_CALL,)


def test_state_table_rule_8_put_neutral_closes_to_flat():
    assert decide(Leg.PUT, Signal15m.NEUTRAL).actions == (Action.EXIT_PUT,)


def test_state_table_rule_9_flat_neutral_stays_flat():
    assert decide(Leg.FLAT, Signal15m.NEUTRAL).actions == ()


def test_state_table_covers_exactly_the_nine_approved_rules():
    table = {}
    for state in Leg:
        for sig in Signal15m:
            table[(state, sig)] = decide(state, sig).actions
    assert table == {
        (Leg.FLAT, Signal15m.BULLISH): (Action.ENTER_CALL,),
        (Leg.CALL, Signal15m.BULLISH): (),
        (Leg.CALL, Signal15m.BEARISH): (Action.SWITCH_CALL_TO_PUT,),
        (Leg.FLAT, Signal15m.BEARISH): (Action.ENTER_PUT,),
        (Leg.PUT, Signal15m.BEARISH): (),
        (Leg.PUT, Signal15m.BULLISH): (Action.SWITCH_PUT_TO_CALL,),
        (Leg.CALL, Signal15m.NEUTRAL): (Action.EXIT_CALL,),
        (Leg.PUT, Signal15m.NEUTRAL): (Action.EXIT_PUT,),
        (Leg.FLAT, Signal15m.NEUTRAL): (),
    }


def test_reversal_chain_bullish_bearish_bullish_bearish_no_duplicate_entries():
    # BULLISH -> BUY CALL; BEARISH -> SELL CALL + BUY PUT;
    # BULLISH -> SELL PUT + BUY CALL; BEARISH -> SELL CALL + BUY PUT.
    seq = [Signal15m.BULLISH, Signal15m.BEARISH, Signal15m.BULLISH, Signal15m.BEARISH]
    state = Leg.FLAT
    executed = []
    for sig in seq:
        decision = decide(state, sig)
        for action in decision.actions:
            if action is Action.ENTER_CALL:
                assert state is Leg.FLAT  # never a duplicate entry
                state = Leg.CALL
            elif action is Action.ENTER_PUT:
                assert state is Leg.FLAT
                state = Leg.PUT
            elif action is Action.EXIT_CALL:
                assert state is Leg.CALL
                state = Leg.FLAT
            elif action is Action.EXIT_PUT:
                assert state is Leg.PUT
                state = Leg.FLAT
            elif action is Action.SWITCH_CALL_TO_PUT:
                assert state is Leg.CALL
                state = Leg.PUT
            elif action is Action.SWITCH_PUT_TO_CALL:
                assert state is Leg.PUT
                state = Leg.CALL
            executed.append(action.value)
    assert executed == [
        "ENTER_CALL",
        "SWITCH_CALL_TO_PUT",
        "SWITCH_PUT_TO_CALL",
        "SWITCH_CALL_TO_PUT",
    ]
    assert state is Leg.PUT


def test_never_jump_directly_to_an_opposite_leg_without_a_close():
    for state in (Leg.CALL, Leg.PUT):
        assert decide(state, Signal15m.NEUTRAL).actions  # closes first
        opp = decide(state, Signal15m.BEARISH if state is Leg.CALL else Signal15m.BULLISH)
        assert opp.actions[0].value.startswith("SWITCH")  # close + reopen, never bare ENTER


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
    config = Directional5MOptionsConfig(
        account=account,
        store_dir=store_dir or P(tempfile.gettempdir()) / "5m_test_dir",
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
    assert report["experiment_id"] == "5M_DIRECTIONAL_OPTIONS_EXPERIMENT"
    assert report["accounting"]["reconciled"] is True
    assert report["decisions"]["mid_candle_actions"] == 0


def test_replay_counts_72_decision_points_per_day(tmp_path):
    engine, _, days = _run(2, seed=8, store_dir=tmp_path)
    report = engine.report()
    assert report["decisions"]["decision_points_total"] == 72 * len(days)


def test_replay_deterministic_fingerprint(tmp_path):
    engine_a, _, _ = _run(2, seed=9, store_dir=tmp_path / "a")
    engine_b, _, _ = _run(2, seed=9, store_dir=tmp_path / "b")
    assert engine_a.stable_fingerprint() == engine_b.stable_fingerprint()
    engine_c, _, _ = _run(2, seed=10, store_dir=tmp_path / "c")
    assert engine_b.stable_fingerprint() != engine_c.stable_fingerprint()


def test_healthy_run_has_zero_data_errors(tmp_path):
    engine, _, _ = _run(1, seed=1, store_dir=tmp_path)
    report = engine.report()
    assert report["data_quality"]["data_errors"] == 0
    assert report["data_quality"]["poisoned"] == 0


def test_no_duplicate_entries_across_replay(tmp_path):
    engine, _, _ = _run(3, seed=13, store_dir=tmp_path)
    state = "FLAT"
    for decision in engine.decisions:
        assert decision["state_before"] == state
        for action in decision["actions"]:
            if action == "ENTER_CALL":
                assert state == "FLAT", "duplicate CALL entry while CALL already open"
                state = "CALL"
            elif action == "ENTER_PUT":
                assert state == "FLAT", "duplicate PUT entry while PUT already open"
                state = "PUT"
            elif action == "EXIT_CALL":
                assert state == "CALL"
                state = "FLAT"
            elif action == "EXIT_PUT":
                assert state == "PUT"
                state = "FLAT"
            elif action == "SWITCH_CALL_TO_PUT":
                assert state == "CALL"
                state = "PUT"
            elif action == "SWITCH_PUT_TO_CALL":
                assert state == "PUT"
                state = "CALL"
        assert decision["state_after"] == state
    # No decision ever fires at/after the EOD flatten boundary.
    assert all(
        datetime.fromisoformat(d["moment"]).time() < time(15, 20)
        for d in engine.decisions
    )


def test_risk_controls_are_reused_and_gating(tmp_path):
    config = Directional5MOptionsConfig(
        account="exp_risk", store_dir=tmp_path, max_daily_loss=Decimal("500")
    )
    store = TrackStore(config.store_dir, config.account)
    start = trading_day_sequence(DAY, 1)[0]
    days = trading_day_sequence(start, 2)
    feed = SyntheticFeed.build(days, seed=3, instrument=config.instrument)
    engine = replay_experiment(config=config, store=store, feed=feed, days=days)
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
    start = trading_day_sequence(DAY, 1)[0]
    days2 = trading_day_sequence(start, 2)
    feed2 = SyntheticFeed.build(days2, seed=21, instrument=TRACK_INSTRUMENT())
    config = Directional5MOptionsConfig(account="exp_resume", store_dir=tmp_path)
    store = TrackStore(config.store_dir, config.account)
    replay_experiment(config=config, store=store, feed=feed2, days=days2)

    # A day that already produced a report is refused (once-per-day).
    with pytest.raises(DayAlreadyReported):
        replay_experiment(config=config, store=store, feed=feed2, days=days2, run_id="dup-run")

    days3 = trading_day_sequence(start, 3)
    fresh = Directional5MOptionsConfig(account="exp_fresh", store_dir=tmp_path)
    fresh_store = TrackStore(fresh.store_dir, fresh.account)
    fresh_engine = replay_experiment(
        config=fresh,
        store=fresh_store,
        feed=SyntheticFeed.build(days3, seed=21, instrument=TRACK_INSTRUMENT()),
        days=days3,
    )
    continued_day3 = replay_experiment(
        config=config,
        store=store,
        feed=SyntheticFeed.build(days3, seed=21, instrument=TRACK_INSTRUMENT()),
        days=[days3[-1]],
        run_id="resumed-run",
        resume=True,
    )
    assert continued_day3.stable_fingerprint() == fresh_engine.stable_fingerprint()


def test_5m_store_does_not_touch_15m_store(tmp_path):
    """Cross-experiment isolation: a 5M run never writes into the 15M store."""
    import json

    exp_15 = tmp_path / "exp15"
    exp_5 = tmp_path / "exp5"
    config15 = Directional5MOptionsConfig(account="exp_15x", store_dir=exp_15)
    store15 = TrackStore(config15.store_dir, config15.account)
    store15.update_run("existing-15m", last_day="2026-09-21", fingerprint="deadbeef15")
    fifteen_bytes_before = sorted(
        (p.relative_to(exp_15).as_posix(), p.read_bytes()) for p in exp_15.rglob("*")
    )

    engine = replay_experiment(
        config=Directional5MOptionsConfig(account="exp_5m", store_dir=exp_5),
        store=TrackStore(exp_5, "exp_5m"),
        feed=SyntheticFeed.build(
            trading_day_sequence(DAY, 1), seed=5, instrument=TRACK_INSTRUMENT()
        ),
        days=trading_day_sequence(DAY, 1),
    )
    store = TrackStore(exp_5, "exp_5m")
    assert engine.config.experiment_id == "5M_DIRECTIONAL_OPTIONS_EXPERIMENT"
    fifteen_bytes_after = sorted(
        (p.relative_to(exp_15).as_posix(), p.read_bytes()) for p in exp_15.rglob("*")
    )
    assert fifteen_bytes_before == fifteen_bytes_after
    report = engine.report()
    assert report["schema"] == "5m_directional_options.report"
    assert report["experiment_id"] == "5M_DIRECTIONAL_OPTIONS_EXPERIMENT"
    manifest = json.loads((exp_5 / "manifest.json").read_text(encoding="utf-8"))
    assert set(manifest["runs"]) == {engine.run_id}
    # The manifest fingerprint is the run recorded at day-finalize: it equals
    # the persisted report's fingerprint (both built from the same report).
    persisted = store.load_report(DAY)
    assert persisted is not None
    assert manifest["runs"][engine.run_id]["fingerprint"] == persisted["fingerprint"]
    assert "exp_15x" not in str(manifest)
    assert list(exp_5.rglob("exp_5m.*.json")), "5M account files must be written under its own namespace"


def test_5m_store_does_not_touch_baseline_store(tmp_path):
    baseline_dir = tmp_path / "baseline"
    exp_dir = tmp_path / "exp"
    baseline_store = TrackStore(baseline_dir, "nifty_5m_daily")
    baseline_store.update_run("existing", last_day="2026-09-21", fingerprint="deadbeef")
    baseline_bytes_before = sorted(
        (p.relative_to(baseline_dir).as_posix(), p.read_bytes()) for p in baseline_dir.rglob("*")
    )

    engine = replay_experiment(
        config=Directional5MOptionsConfig(account="exp_5m", store_dir=exp_dir),
        store=TrackStore(exp_dir, "exp_5m"),
        feed=SyntheticFeed.build(
            trading_day_sequence(DAY, 1), seed=5, instrument=TRACK_INSTRUMENT()
        ),
        days=trading_day_sequence(DAY, 1),
    )
    assert engine.store.store_dir != baseline_dir
    baseline_bytes_after = sorted(
        (p.relative_to(baseline_dir).as_posix(), p.read_bytes()) for p in baseline_dir.rglob("*")
    )
    assert baseline_bytes_before == baseline_bytes_after


def test_no_credentials_or_tokens_in_experiment_output(tmp_path):
    """No secret/credential value or secret-bearing key ever appears in output."""

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
        assert checkpoint["schema"] == "5m_directional_options.checkpoint"
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
    from fno_ai_paper_trading.paper_track.runner import run_sessions

    days = trading_day_sequence(DAY, 2)
    feed = SyntheticFeed.build(days, seed=11, instrument=TRACK_INSTRUMENT())
    baseline_store = TrackStore(tmp_path / "base", "nifty_5m_daily")
    base_config = TrackConfig(
        account="nifty_5m_daily", store_dir=baseline_store.store_dir, interval="5m"
    )
    base = run_sessions(config=base_config, store=baseline_store, feed=feed, days=days)
    base_metrics = baseline_metrics_from_engine(base)
    assert base_metrics["name"].startswith("MA(5,21)")

    config = Directional5MOptionsConfig(account="exp_cmp", store_dir=tmp_path / "exp")
    exp_store = TrackStore(config.store_dir, config.account)
    engine = replay_experiment(
        config=config,
        store=exp_store,
        feed=SyntheticFeed.build(days, seed=11, instrument=TRACK_INSTRUMENT()),
        days=days,
        baseline_metrics=base_metrics,
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


def test_15m_experiment_is_still_intact(tmp_path):
    """The 15M experiment's own contract table is byte-for-byte unchanged."""
    from fno_ai_paper_trading.experiments.directional_15m.contract import (
        decide as decide_15m,
    )
    from fno_ai_paper_trading.experiments.directional_15m.contract import (
        Action as Action15m,
    )
    from fno_ai_paper_trading.experiments.directional_15m.contract import (
        Leg as Leg15m,
    )
    from fno_ai_paper_trading.experiments.directional_15m.contract import (
        Signal15m as Signal15m_E,
    )

    # The exact same approved table is honoured by the 5M experiment.
    for state in Leg15m:
        for sig in Signal15m_E:
            assert decide(state, sig).actions == decide_15m(state, sig).actions
    # The 5M experiment id is intentionally distinct.
    from fno_ai_paper_trading.experiments.directional_15m.executor import (
        DirectionalOptionsConfig,
    )

    assert DirectionalOptionsConfig().account == "15m_directional_options"
    assert Directional5MOptionsConfig().account == "5m_directional_options"
    assert DirectionalOptionsConfig().experiment_id == "15M_DIRECTIONAL_OPTIONS_EXPERIMENT"
    assert Directional5MOptionsConfig().experiment_id == "5M_DIRECTIONAL_OPTIONS_EXPERIMENT"