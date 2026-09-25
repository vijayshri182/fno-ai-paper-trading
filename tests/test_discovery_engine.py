"""Tests for the discovery engine: catalog fidelity, replay determinism,
promotion honesty, OOS protection, and the written report contract."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pytest

from fno_ai_paper_trading.models.enums import Signal, InstrumentType
from fno_ai_paper_trading.models.instruments import Instrument
from fno_ai_paper_trading.models.market import MarketPrice
from fno_ai_paper_trading.walkforward.config import WalkForwardConfig

from fno_ai_paper_trading.discovery import backtest
from fno_ai_paper_trading.discovery.catalog import (
    build_control,
    build_deck,
    build_params,
    persist_definition,
    perturbations_for,
    variants_for,
)
from fno_ai_paper_trading.discovery.report import build_markdown


def _nifty() -> Instrument:
    return Instrument(
        symbol="Nifty 50",
        instrument_type=InstrumentType.INDEX,
        underlying_symbol="NIFTY",
        lot_size=1,
        multiplier=1,
    )


def _bars(n_days: int = 6, bars_per_day: int = 25) -> list[MarketPrice]:
    from datetime import time, timedelta

    start = date(2025, 6, 2)
    out: list[MarketPrice] = []
    for d in range(n_days):
        day = start + timedelta(days=d)
        for i in range(bars_per_day):
            ts = datetime.combine(day, time(9, 15)) + timedelta(minutes=5 * i)
            level = 100 + i * 0.01 + d
            out.append(MarketPrice(
                instrument=_nifty(),
                timestamp=ts,
                open=Decimal(str(level)),
                high=Decimal(str(level + 0.5)),
                low=Decimal(str(level - 0.5)),
                close=Decimal(str(level + 0.2)),
                volume=1000,
            ))
    return out


# ---------------------------------------------------------------------------
# catalog
# ---------------------------------------------------------------------------

class TestCatalog:
    def test_control_is_frozen_and_hashed(self) -> None:
        a, b = build_control(), build_control()
        assert a.candidate_id == "model_0.control"
        assert a.control is True
        assert a.definition_hash == b.definition_hash
        assert a.params == {"fast": 5, "slow": 21}

    def test_deck_contains_control_plus_six(self) -> None:
        deck = build_deck()
        assert len(deck) == 7
        assert deck[0].control is True
        assert {d.candidate_id for d in deck} == {
            "model_0.control", "d1_trend_ema", "d2_momentum", "d3_breakout_vol",
            "d4_pullback", "d5_mean_reversion", "d6_structure",
        }
        assert all(d.definition_hash for d in deck)
        assert all(d.params for d in deck if not d.control)

    def test_perturbations_are_recorded_variants(self) -> None:
        base = {"d1_trend_ema": None, "d2_momentum": None}
        for cid, _ in base.items():
            deck = build_deck()
            defn = next(d for d in deck if d.candidate_id == cid)
            variants = perturbations_for(defn)
            assert len(variants) == 8
            for v in variants:
                assert v.parent == cid
                assert v.version.startswith("1.0.p")
                assert v.params != defn.params
            # each variant has a distinct, stable hash
            assert len({v.definition_hash for v in variants}) == 8

    def test_control_has_no_perturbations(self) -> None:
        assert perturbations_for(build_control()) == []
        assert variants_for(build_control()) == [build_control()]

    def test_unknown_knob_rejected_by_build_params(self) -> None:
        defn = build_deck()[1]  # a discovery candidate
        with pytest.raises(TypeError):
            build_params(defn, overrides={"bogus_knob": 1})

    def test_persist_definition_write_once(self, tmp_path) -> None:
        defn = build_deck()[1]
        persist_definition(defn, tmp_path)
        persist_definition(defn, tmp_path)  # same hash → idempotent, no error
        tampered = build_control()  # different hash on the same path
        from copy import deepcopy
        import dataclasses
        other = deepcopy(defn)
        other = dataclasses.replace(other, params={**other.params, "rsi_period": 99})
        from fno_ai_paper_trading.discovery.catalog import _hash_of
        other = dataclasses.replace(other, definition_hash=_hash_of(
            other.candidate_id, other.family, other.provider_path,
            other.params, other.version, other.parent, other.generation,
        ))
        with pytest.raises(ValueError):
            persist_definition(other, tmp_path)


# ---------------------------------------------------------------------------
# replay + metrics
# ---------------------------------------------------------------------------

class TestReplay:
    def test_replay_is_deterministic_and_day_sliced(self) -> None:
        deck = build_deck()
        control = deck[0]
        d1 = deck[1]
        bars = _bars(n_days=6, bars_per_day=25)
        config = WalkForwardConfig()
        for defn in (control, d1):
            first = backtest.replay_candidate(defn, bars, config)
            second = backtest.replay_candidate(defn, bars, config)
            assert len(first) == len(second) == 6  # 6 consecutive calendar days
            assert [r.net_pnl for r in first] == [r.net_pnl for r in second]
            assert [r.day for r in first] == sorted(r.day for r in first)  # chronological
            assert len(first) == len({r.day for r in first})

    def test_replay_accepts_window_slice(self) -> None:
        d1 = build_deck()[1]
        bars = _bars(n_days=6, bars_per_day=25)
        config = WalkForwardConfig()
        window = backtest.replay_candidate(d1, bars, config, start=date(2025, 6, 3), end=date(2025, 6, 5))
        days = {r.day for r in window}
        assert days == {date(2025, 6, 3), date(2025, 6, 4), date(2025, 6, 5)}
        assert all(date(2025, 6, 6) > r.day >= date(2025, 6, 3) for r in window)

    def test_analyze_rolls_up_replays(self) -> None:
        control = build_control()
        bars = _bars(n_days=6, bars_per_day=25)
        replays = backtest.replay_candidate(control, bars, WalkForwardConfig())
        run = backtest.analyze(replays, control.candidate_id, control.version,
                               window_start=replays[0].day, window_end=replays[-1].day)
        assert run.trades == sum(r.trades for r in replays)
        assert run.net_pnl == sum((r.net_pnl for r in replays), Decimal("0"))
        assert run.costs == sum((r.costs for r in replays), Decimal("0"))
        assert 0.0 <= run.win_rate <= 100.0
        assert run.segments >= 1
        assert run.window_start == replays[0].day
        assert run.window_end == replays[-1].day

    def test_analyze_empty_window_is_honest(self) -> None:
        run = backtest.analyze([], "x", "1", window_start=date(2025, 1, 1), window_end=date(2025, 1, 2))
        assert run.trades == 0 and run.days == 0
        assert "no days replayed" in run.quality_issues[0]


# ---------------------------------------------------------------------------
# competition + promotion honesty
# ---------------------------------------------------------------------------

class TestCompetition:
    def test_score_is_bounded_and_oos_never_scored(self) -> None:
        from fno_ai_paper_trading.discovery.competition import score_candidate
        bars = _bars(n_days=6, bars_per_day=25)
        replays = backtest.replay_candidate(build_control(), bars, WalkForwardConfig())
        run = backtest.analyze(replays, "c", "1", window_start=date(2025, 6, 2), window_end=date(2025, 6, 5))
        sc = score_candidate(run, robustness_positive_fraction=1.0, cost_positive_fraction=1.0)
        assert sc.components.out_of_sample == 0.0
        assert 0.0 <= sc.components.total <= 100.0
        assert "out_of_sample_unproven" in sc.flags
        assert run.trades == 0 or sc.score < 100.0

    def test_promotion_is_reject_without_oos(self) -> None:
        from fno_ai_paper_trading.discovery.competition import promotion_verdict
        bars = _bars(n_days=6, bars_per_day=25)
        config = WalkForwardConfig()
        ctrl_runs = backtest.replay_candidate(build_control(), bars, config)
        chal_runs = backtest.replay_candidate(build_deck()[1], bars, config)
        win = date(2025, 6, 2)
        ctrl = backtest.analyze(ctrl_runs, "model_0.control", "1", window_start=win, window_end=date(2025, 6, 5))
        chal = backtest.analyze(chal_runs, "d1_trend_ema", "1", window_start=win, window_end=date(2025, 6, 5))
        verdict = promotion_verdict(chal, ctrl, challenger_name="d1_trend_ema", config=config)
        assert verdict.decision == "REJECT"


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------

class TestReport:
    def test_build_markdown_is_deterministic_and_has_20_sections(self) -> None:
        payload = {
            "run_id": "cycle_20260101_000000",
            "timestamp": "2026-01-01T00:00:00Z",
            "generation": "g0",
            "provenance": {
                "provider": "upstox_mock", "instrument": "Nifty 50",
                "instrument_key": "NSE_EQ|NIFTY", "interval": "5m",
                "requested_from": "2025-01-02", "requested_to": "2025-10-03",
                "actual_from": "2025-01-02", "actual_to": "2025-10-03",
                "candle_count": 10, "retrieved_at": "2026-01-01T00:00:00Z",
                "freshness": "FRESH", "data_hash": "abc",
                "schema_version": "1", "dataset_path": "x.csv",
                "blocked": "no (fresh Upstox data acquired)",
            },
            "market_environment": {
                "window_start": "2025-01-02", "window_end": "2025-10-03",
                "days": 3, "bars": 10, "avg_close": "100", "min_low": "90",
                "max_high": "120", "avg_day_range": "2", "median_atr": "1",
                "regime_estimate": "TRENDING", "note": "test",
            },
            "families_tested": [{"family": "TREND_FOLLOWING", "candidate_ids": ["d1_trend_ema"]}],
            "candidates": {
                "model_0.control": {"definition": {"name": "control", "family": "TREND_FOLLOWING"}},
            },
            "ranking": [],
            "scorecards": [],
            "promotion": {},
            "watch_window": {"protected_oos_start": "2025-10-06", "train_end": "2025-06-30",
                             "validation_start": "2025-07-01", "validation_end": "2025-10-03"},
            "current_best": None,
            "why_best": "none",
            "unproven": ["out-of-sample profitability"],
            "next_hypothesis": {"family": "TREND_FOLLOWING", "mechanism": "x", "read": "y", "trigger": "z"},
            "learning": {"bullet": "lb"},
            "algo_ready": "NO",
            "live_trading": False,
            "next_action": "continue the discovery loop",
            "links": {"paper trading dashboard": "../../docs/paper_trading_dashboard.html"},
        }
        a = build_markdown(payload)
        b = build_markdown(payload)
        assert a == b
        assert "## 20. LINKS" in a
        assert "paper_trading_dashboard.html" in a
        assert "## 19. ALGO READY / LIVE STATUS" in a
        assert a.count("\n## ") >= 19