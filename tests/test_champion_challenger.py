"""Tests for WS 7.11 champion vs challenger evaluation + regime-filtered challenger.

The challenger strategy is evidence only: it suppresses BUY entries outside the
allowed regime trend but never widens risk states (SELL/HOLD pass through) and
never uses bars after the decision bar. The comparison framework reuses the
five-year period split so no evaluation can influence day labels.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal

from fno_ai_paper_trading.evaluation.champion_challenger import (
    ChallengeEntry,
    ChampionChallenger,
    MultiPeriodComparison,
    challenger_delta,
    comparison_report_to_dict,
    comparison_report_to_html,
    multi_period_comparison_to_dict,
    multi_period_comparison_to_html,
)
from fno_ai_paper_trading.evaluation.five_year import DayBars, PeriodSplitConfig
from fno_ai_paper_trading.evaluation.records import EvaluationConfig
from fno_ai_paper_trading.models.enums import InstrumentType, Signal
from fno_ai_paper_trading.models.instruments import Instrument
from fno_ai_paper_trading.models.market import MarketPrice
from fno_ai_paper_trading.regime.detector import RegimeDetector, TrendState
from fno_ai_paper_trading.strategies import (
    MovingAverageCrossStrategy,
    RegimeFilteredMovingAverageCross,
)

FLAT_PRICE = Decimal("25000")
CROSSOVER = 31  # first non-flat bar (30 flats + 1 move) triggers the crossover


def _future() -> Instrument:
    return Instrument(
        symbol="NIFTY1",
        instrument_type=InstrumentType.FUTURE,
        underlying_symbol="NIFTY",
    )


def _bar(close: Decimal, ts: datetime) -> MarketPrice:
    return MarketPrice(
        instrument=_future(),
        timestamp=ts,
        open=close - Decimal("2"),
        high=close + Decimal("5"),
        low=close - Decimal("5"),
        close=close,
        volume=1000,
    )


def _series(
    steps: list[Decimal], length: int, start: datetime
) -> list[MarketPrice]:
    bars: list[MarketPrice] = []
    price = FLAT_PRICE
    for i in range(length):
        if i >= 30:
            price += steps[i - 30]
        bars.append(_bar(price, start + timedelta(minutes=1 * i)))
    return bars


def _rise(step: Decimal, length: int = 60, day: date = date(2026, 9, 1)) -> list[MarketPrice]:
    """30 flat bars then an ever-rising ramp; up-cross at bar index 30."""
    steps = [step] * max(0, length - 30)
    return _series(steps, length, datetime.combine(day, time(9, 15)))


def _fall(step: Decimal, length: int = 60, day: date = date(2026, 1, 1)) -> list[MarketPrice]:
    """30 flat bars then an ever-falling ramp; down-cross at bar index 30."""
    steps = [-step] * max(0, length - 30)
    return _series(steps, length, datetime.combine(day, time(9, 15)))


def _gentle_hill(length: int = 60) -> list[MarketPrice]:
    """Flat, gentle rise (SIDEWAYS up-cross), then a sharp fall (SELL later).

    Champion buys at the gentle up-cross and exits on the sharp fall, so it
    closes one round trip. A UP-filtered challenger suppresses that BUY (the
    regime at the up-cross is SIDEWAYS) and stays flat.
    """
    rise = [Decimal("10")] * 10
    fall = [-Decimal("100")] * max(0, length - 40)
    return _series(rise + fall, length, datetime(2026, 3, 1, 9, 15))


def _days(*day_seeds: tuple[date, int]) -> list[DayBars]:
    out: list[DayBars] = []
    for day, seed in day_seeds:
        bars = _rise(Decimal("100"), day=day) if seed % 2 == 0 else _fall(
            Decimal("100"), day=day
        )
        out.append(DayBars(day=day, bars=tuple(bars), source_hash=f"hash-{seed}"))
    return out


class TestRegimeFilteredMovingAverageCross:
    def test_name_is_distinct_from_baseline(self) -> None:
        candidate = RegimeFilteredMovingAverageCross()
        assert candidate.name == "regime_filtered_ma_cross"
        assert candidate.name != MovingAverageCrossStrategy().name

    def test_rejects_empty_allowed_trends(self) -> None:
        import pytest

        with pytest.raises(ValueError):
            RegimeFilteredMovingAverageCross(allowed_trends=[])

    def test_rejects_bad_trend_token(self) -> None:
        import pytest

        with pytest.raises(ValueError):
            RegimeFilteredMovingAverageCross(allowed_trends=("SOMETHING",))

    def test_rejects_fast_not_under_slow(self) -> None:
        import pytest

        with pytest.raises(ValueError):
            RegimeFilteredMovingAverageCross(fast=21, slow=5)

    def test_warmup_holds_like_baseline(self) -> None:
        candidate = RegimeFilteredMovingAverageCross()
        result = candidate.analyze(_rise(Decimal("100"))[:21])
        assert result.signal is Signal.HOLD

    def test_permits_buy_in_allowed_up_trend(self) -> None:
        # steep rise: the fast/slow gap at the up-cross exceeds 0.05% -> UP
        candidate = RegimeFilteredMovingAverageCross(allowed_trends=("UP",))
        result = candidate.analyze(_rise(Decimal("100"))[:CROSSOVER])
        assert result.signal is Signal.BUY
        assert result.meta["regime_trend"] == "UP"
        assert result.meta["regime_filtered"] is False

    def test_suppresses_buy_in_sideways_trend(self) -> None:
        # gentle rise: gap below the threshold -> SIDEWAYS at the up-cross
        candidate = RegimeFilteredMovingAverageCross(allowed_trends=("UP",))
        result = candidate.analyze(_rise(Decimal("10"))[:CROSSOVER])
        assert result.signal is Signal.HOLD
        assert result.meta["regime_trend"] == "SIDEWAYS"
        assert result.meta["regime_filtered"] is True
        assert "suppressed" in result.reason

    def test_buy_pass_through_when_trend_allowed(self) -> None:
        candidate = RegimeFilteredMovingAverageCross(
            allowed_trends=("UP", "SIDEWAYS")
        )
        result = candidate.analyze(_rise(Decimal("10"))[:CROSSOVER])
        assert result.signal is Signal.BUY
        assert result.meta["regime_filtered"] is False

    def test_down_cross_pass_through_unfiltered(self) -> None:
        # SELL exits are never suppressed by the regime filter.
        candidate = RegimeFilteredMovingAverageCross(allowed_trends=("UP",))
        result = candidate.analyze(_fall(Decimal("100"))[:CROSSOVER])
        assert result.signal is Signal.SELL
        assert result.meta["regime_filtered"] is False

    def test_deterministic(self) -> None:
        candidate = RegimeFilteredMovingAverageCross()
        bars = _rise(Decimal("10"))[:CROSSOVER]
        first = candidate.analyze(bars)
        second = candidate.analyze(bars)
        assert first.signal is second.signal
        assert first.reason == second.reason
        assert first.meta == second.meta

    def test_regime_uses_only_the_decision_prefix(self) -> None:
        # The strategy's regime label at the crossover must equal a fresh
        # detect_prefix over the same prefix -- never influenced by later bars.
        bars = _rise(Decimal("10"))
        candidate = RegimeFilteredMovingAverageCross(allowed_trends=("DOWN",))
        result = candidate.analyze(bars[:CROSSOVER])
        regime = RegimeDetector().detect_prefix(bars, CROSSOVER - 1)
        assert regime is not None
        assert result.meta["regime_label"] == regime.label

    def test_allowed_trends_normalized_to_enum(self) -> None:
        candidate = RegimeFilteredMovingAverageCross(allowed_trends=("down", "up"))
        assert TrendState.DOWN in candidate.allowed_trends
        assert TrendState.UP in candidate.allowed_trends


class TestChallengerDelta:
    def test_delta_summary_of_winner(self) -> None:
        delta = challenger_delta(
            ChampionshipVars.champion_run,
            ChampionshipVars.winner_run,
            period=None,
        )
        assert delta.challenger_name == "regime_filtered_ma_cross"
        assert delta.beats_champion is True
        assert delta.evidence_only is True
        assert delta.period is None

    def test_delta_summary_of_loser(self) -> None:
        delta = challenger_delta(
            ChampionshipVars.champion_run,
            ChampionshipVars.loser_run,
            period=None,
        )
        assert delta.beats_champion is False
        assert delta.net_pnl_delta < 0

    def test_delta_serializable(self) -> None:
        delta = challenger_delta(
            ChampionshipVars.champion_run,
            ChampionshipVars.winner_run,
            period="out_of_sample",
        )
        payload = delta.to_dict()
        assert payload["period"] == "out_of_sample"
        assert payload["beats_champion"] is True
        assert isinstance(payload["net_pnl_delta"], str)


class TestChampionChallengerBars:
    def test_runs_champion_and_challenger_with_deltas(self) -> None:
        champion = MovingAverageCrossStrategy()
        candidate = RegimeFilteredMovingAverageCross()
        report = ChampionChallenger().run_bars(_gentle_hill(), champion, [candidate])
        assert report.champion.baseline is True
        assert report.champion.strategy_name == "moving_average_cross"
        assert len(report.entries) == 1
        entry: ChallengeEntry = report.entries[0]
        assert entry.run.baseline is False
        assert entry.delta is not None
        assert entry.delta.beats_champion in (True, False)

    def test_challenger_without_filter_equals_champion(self) -> None:
        champion = MovingAverageCrossStrategy()
        twin = RegimeFilteredMovingAverageCross(
            allowed_trends=("UP", "DOWN", "SIDEWAYS")
        )
        report = ChampionChallenger().run_bars(_gentle_hill(), champion, [twin])
        delta = report.entries[0].delta
        assert delta is not None
        assert delta.net_pnl_delta == Decimal("0")
        assert delta.max_drawdown_pct_delta == Decimal("0")
        assert delta.beats_champion is True

    def test_suppression_leads_to_fewer_trades(self) -> None:
        champion = MovingAverageCrossStrategy()
        candidate = RegimeFilteredMovingAverageCross()
        report = ChampionChallenger().run_bars(_gentle_hill(), champion, [candidate])
        champion_trades = report.champion.aggregate.num_trades
        challenger_trades = report.entries[0].run.aggregate.num_trades
        assert champion_trades >= 1
        assert challenger_trades < champion_trades

    def test_config_provenance_recorded(self) -> None:
        config = EvaluationConfig(initial_capital=Decimal("50000"))
        report = ChampionChallenger(config=config).run_bars(
            _gentle_hill(), MovingAverageCrossStrategy(), []
        )
        assert report.champion.config.initial_capital == Decimal("50000")


class TestChampionChallengerDays:
    def _five_period_days(self) -> list[DayBars]:
        return _days(
            (date(2026, 9, 1), 10),
            (date(2026, 9, 2), 11),
            (date(2026, 9, 3), 12),
            (date(2026, 9, 4), 13),
            (date(2026, 9, 5), 14),
        )

    def test_multi_period_report_structure(self) -> None:
        champion = MovingAverageCrossStrategy()
        candidate = RegimeFilteredMovingAverageCross()
        runner = ChampionChallenger(split=PeriodSplitConfig())
        comparison = runner.run_days(self._five_period_days(), champion, [candidate])
        assert isinstance(comparison, MultiPeriodComparison)
        assert comparison.period_counts["training"] == 3
        assert comparison.period_counts["validation"] == 1
        assert comparison.period_counts["out_of_sample"] == 1
        assert "training" in comparison.by_period
        assert "out_of_sample" in comparison.by_period
        assert comparison.overall.champion.baseline is True
        assert comparison.overall.period is None

    def test_period_runs_only_cover_their_days(self) -> None:
        champion = MovingAverageCrossStrategy()
        candidate = RegimeFilteredMovingAverageCross()
        comparison = ChampionChallenger().run_days(
            self._five_period_days(), champion, [candidate]
        )
        oos = comparison.by_period["out_of_sample"]
        assert len(oos.champion.sessions) == 1
        assert oos.entries[0].run.sessions[0].dataset_name == "2026-09-05"

    def test_serialization_smoke(self) -> None:
        champion = MovingAverageCrossStrategy()
        candidate = RegimeFilteredMovingAverageCross()
        comparison = ChampionChallenger().run_days(
            self._five_period_days(), champion, [candidate]
        )
        payload = multi_period_comparison_to_dict(comparison)
        assert set(payload["by_period"].keys()) == {
            "training",
            "validation",
            "out_of_sample",
        }
        assert payload["overall"]["champion_run"]["strategy_name"] == "moving_average_cross"

    def test_comparison_report_dict_has_delta(self) -> None:
        champion = MovingAverageCrossStrategy()
        candidate = RegimeFilteredMovingAverageCross()
        report = ChampionChallenger().run_bars(_gentle_hill(), champion, [candidate])
        payload = comparison_report_to_dict(report)
        assert payload["champion_run"]["baseline"] is True
        assert payload["challengers"][0]["delta"]["evidence_only"] is True

    def test_html_render_smoke(self) -> None:
        champion = MovingAverageCrossStrategy()
        candidate = RegimeFilteredMovingAverageCross()
        runner = ChampionChallenger()
        bars_html = comparison_report_to_html(
            runner.run_bars(_gentle_hill(), champion, [candidate])
        )
        assert "Champion vs Challenger" in bars_html
        day_html = multi_period_comparison_to_html(
            runner.run_days(self._five_period_days(), champion, [candidate])
        )
        assert "out_of_sample" in day_html


class ChampionshipVars:
    champion_run = None
    winner_run = None
    loser_run = None


def _build_fixture_runs() -> None:
    # Steep, unbroken rise: the champion buys at the UP-regime up-cross and rides
    # the trend to a positive result. The unfiltered twin matches it exactly; a
    # DOWN-only challenger suppresses that BUY and stays flat, losing to the
    # champion on net P&L.
    champion = MovingAverageCrossStrategy()
    winner = RegimeFilteredMovingAverageCross(
        allowed_trends=("UP", "DOWN", "SIDEWAYS")
    )
    loser = RegimeFilteredMovingAverageCross(allowed_trends=("DOWN",))
    report = ChampionChallenger().run_bars(_rise(Decimal("100")), champion, [winner, loser])
    ChampionshipVars.champion_run = report.champion
    ChampionshipVars.winner_run = report.entries[0].run
    ChampionshipVars.loser_run = report.entries[1].run
    assert report.entries[1].run.aggregate.num_trades == 0
    assert report.entries[0].delta.beats_champion is True
    assert report.entries[1].delta.beats_champion is False


_build_fixture_runs()