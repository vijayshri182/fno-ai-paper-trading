"""Tests for WS 7.12 controlled model promotion & rollback.

The registry is append-only and reconstructed from its log; the gate is a pure
decision over evidence views (no strategy/risk/broker/service imports), so a
promotion can never run code or enable live trading.
"""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from fno_ai_paper_trading.promotion.gate import (
    DeltaView,
    PromotionCriteria,
    PromotionGate,
    PromotionVerdict,
)
from fno_ai_paper_trading.promotion.registry import ModelVersion, VersionRegistry


def _view(
    *,
    name: str = "candidate",
    period: str | None = "out_of_sample",
    champion_pnl: str = "1000",
    challenger_pnl: str = "1500",
    champion_maxdd: str = "2.0",
    challenger_maxdd: str = "1.5",
    beats: bool = True,
) -> DeltaView:
    return DeltaView(
        challenger_name=name,
        period=period,
        champion_net_pnl=Decimal(champion_pnl),
        challenger_net_pnl=Decimal(challenger_pnl),
        champion_max_drawdown_pct=Decimal(champion_maxdd),
        challenger_max_drawdown_pct=Decimal(challenger_maxdd),
        beats_champion=beats,
    )


def _deltas(
    oos: DeltaView | None,
    validation: DeltaView | None = None,
) -> dict[str, dict[str, DeltaView]]:
    out: dict[str, dict[str, DeltaView]] = {}
    if oos is not None:
        out["out_of_sample"] = {oos.challenger_name: oos}
    if validation is not None:
        out["validation"] = {validation.challenger_name: validation}
    return out


# ---------------------------------------------------------------------------
# version registry
# ---------------------------------------------------------------------------

class TestVersionRegistry:
    def test_start_creates_baseline_active(self) -> None:
        registry = VersionRegistry()
        baseline = registry.start()
        assert baseline.version_id == "model_0"
        assert baseline.strategy_name == "moving_average_cross"
        assert registry.active is not None
        assert registry.active.status == "ACTIVE"
        assert registry.champion_chain == ("model_0",)

    def test_cannot_start_twice(self) -> None:
        registry = VersionRegistry()
        registry.start()
        with pytest.raises(ValueError):
            registry.start()

    def test_promote_retires_previous_active(self) -> None:
        registry = VersionRegistry()
        registry.start()
        promoted = registry.promote(
            strategy_name="regime_filtered_ma_cross",
            strategy_params={"allowed_trends": ["UP"]},
            feature_version="features-1",
            description="regime-filtered candidate",
            evidence={"oos_pnl": "1500"},
        )
        assert promoted.version_id == "model_1"
        assert promoted.status == "ACTIVE"
        assert registry.active is not None and registry.active.version_id == "model_1"
        baseline = registry.get("model_0")
        assert baseline is not None and baseline.status == "RETIRED"
        assert baseline.retired_at

    def test_promote_before_start_raises(self) -> None:
        registry = VersionRegistry()
        with pytest.raises(ValueError):
            registry.promote(
                strategy_name="x",
                strategy_params={},
                feature_version="f1",
                description="",
                evidence={},
            )

    def test_rollback_restores_previous_champion(self) -> None:
        registry = VersionRegistry()
        registry.start()
        registry.promote(
            strategy_name="regime_filtered_ma_cross",
            strategy_params={},
            feature_version="features-1",
            description="candidate",
            evidence={},
        )
        restored = registry.rollback(reason="worse out-of-sample")
        assert restored.version_id == "model_0"
        assert restored.status == "ACTIVE"
        rolled = registry.get("model_1")
        assert rolled is not None and rolled.status == "ROLLED_BACK"
        assert rolled.rollback_reason == "worse out-of-sample"
        assert registry.active is not None and registry.active.version_id == "model_0"

    def test_cannot_rollback_baseline(self) -> None:
        registry = VersionRegistry()
        registry.start()
        with pytest.raises(ValueError):
            registry.rollback(reason="nope")

    def test_rollback_to_specific_version(self) -> None:
        registry = VersionRegistry()
        registry.start()
        registry.promote(
            strategy_name="c1", strategy_params={}, feature_version="f", description="", evidence={}
        )
        registry.promote(
            strategy_name="c2", strategy_params={}, feature_version="f", description="", evidence={}
        )
        restored = registry.rollback_to("model_1", reason="c2 diverged")
        assert restored.version_id == "model_1"
        assert registry.get("model_1").status == "ACTIVE"
        assert registry.get("model_2").status == "ROLLED_BACK"

    def test_persistence_round_trip(self, tmp_path: Path) -> None:
        path = tmp_path / "model_registry.jsonl"
        registry = VersionRegistry(path)
        registry.start()
        registry.promote(
            strategy_name="regime_filtered_ma_cross",
            strategy_params={"allowed_trends": ["UP"]},
            feature_version="features-1",
            description="candidate",
            evidence={"oos_pnl": "1500"},
        )
        reloaded = VersionRegistry(path)
        assert reloaded.active is not None
        assert reloaded.active.version_id == "model_1"
        assert reloaded.active.status == "ACTIVE"
        assert reloaded.get("model_0").status == "RETIRED"
        assert reloaded.log == registry.log

    def test_persist_rollback_replays(self, tmp_path: Path) -> None:
        path = tmp_path / "model_registry.jsonl"
        registry = VersionRegistry(path)
        registry.start()
        registry.promote(
            strategy_name="c1", strategy_params={}, feature_version="f", description="", evidence={}
        )
        registry.rollback(reason="oos worse")
        reloaded = VersionRegistry(path)
        assert reloaded.active.version_id == "model_0"
        assert reloaded.get("model_1").status == "ROLLED_BACK"

    def test_corrupt_line_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "model_registry.jsonl"
        path.write_text('{"action":"start","version":{"version_id":"model_0","strategy_name":"m","strategy_params":{},"feature_version":"f","description":"","promoted_at":"2026-01-01T00:00:00","evidence":{}}}\nnot json\n', encoding="utf-8")
        with pytest.raises(ValueError):
            VersionRegistry(path)

    def test_unknown_action_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "bogus.jsonl"
        path.write_text('{"action":"explode"}\n', encoding="utf-8")
        with pytest.raises(ValueError):
            VersionRegistry(path)

    def test_no_file_means_empty_registry(self, tmp_path: Path) -> None:
        registry = VersionRegistry(tmp_path / "missing.jsonl")
        assert registry.active is None
        assert registry.versions == ()
        assert registry.champion_chain == ()


# ---------------------------------------------------------------------------
# promotion gate — pure evidence views (no evaluation imports needed)
# ---------------------------------------------------------------------------

class TestPromotionGate:
    def test_promotes_on_robust_oos_win(self) -> None:
        gate = PromotionGate()
        verdict = gate.evaluate(
            _deltas(_view(challenger_pnl="1500", champion_pnl="1000")),
            {"out_of_sample": 4, "validation": 2, "training": 6},
            "candidate",
        )
        assert verdict.decision == "PROMOTE"
        assert verdict.promoted is True
        assert any("never enables live trading" in r for r in verdict.reasons)

    def test_rejects_when_oos_lost(self) -> None:
        verdict = PromotionGate().evaluate(
            _deltas(_view(challenger_pnl="900", champion_pnl="1000", beats=False)),
            {"out_of_sample": 4},
            "candidate",
        )
        assert verdict.decision == "REJECT"
        assert any("does not beat the champion" in r for r in verdict.reasons)

    def test_rejects_when_no_oos_period(self) -> None:
        verdict = PromotionGate().evaluate({}, {"out_of_sample": 0}, "candidate")
        assert verdict.decision == "REJECT"
        assert any("no out-of-sample period" in r for r in verdict.reasons)

    def test_rejects_when_oos_insufficient_days(self) -> None:
        verdict = PromotionGate().evaluate(
            _deltas(_view()),
            {"out_of_sample": 1},
            "candidate",
            criteria=PromotionCriteria(oos_min_days=3),
        )
        assert verdict.decision == "REJECT"
        assert any("days 1 < minimum 3" in r for r in verdict.reasons)

    def test_accepts_when_oos_min_met(self) -> None:
        verdict = PromotionGate().evaluate(
            _deltas(_view()),
            {"out_of_sample": 3},
            "candidate",
            criteria=PromotionCriteria(oos_min_days=3),
        )
        assert verdict.decision == "PROMOTE"

    def test_rejects_when_validation_worse(self) -> None:
        verdict = PromotionGate().evaluate(
            _deltas(
                _view(period="out_of_sample"),
                _view(period="validation", challenger_pnl="800", champion_pnl="1000", beats=False),
            ),
            {"out_of_sample": 4, "validation": 2},
            "candidate",
        )
        assert verdict.decision == "REJECT"
        assert any("validation is worse" in r for r in verdict.reasons)

    def test_skips_validation_check_when_disabled(self) -> None:
        verdict = PromotionGate().evaluate(
            _deltas(
                _view(period="out_of_sample"),
                _view(period="validation", challenger_pnl="800", champion_pnl="1000", beats=False),
            ),
            {"out_of_sample": 4, "validation": 2},
            "candidate",
            criteria=PromotionCriteria(require_validation_not_worse=False),
        )
        assert verdict.decision == "PROMOTE"

    def test_rejects_when_positive_oos_required_and_flat(self) -> None:
        verdict = PromotionGate().evaluate(
            _deltas(_view(challenger_pnl="0", champion_pnl="-50")),
            {"out_of_sample": 3},
            "candidate",
            criteria=PromotionCriteria(require_positive_oos_pnl=True),
        )
        assert verdict.decision == "REJECT"
        assert any("not positive" in r for r in verdict.reasons)

    def test_verdict_evidence_serializable(self) -> None:
        verdict = PromotionGate().evaluate(
            _deltas(_view()),
            {"out_of_sample": 3},
            "candidate",
        )
        payload = json.loads(json.dumps(verdict.evidence, default=str))
        assert payload["out_of_sample"]["beats_champion"] is True

    def test_criteria_validation(self) -> None:
        with pytest.raises(ValueError):
            PromotionCriteria(oos_min_days=0)


# ---------------------------------------------------------------------------
# delta view adapters
# ---------------------------------------------------------------------------

class TestDeltaViewAdapters:
    def test_to_dict_and_from_dict_round_trip(self) -> None:
        view = _view()
        rebuilt = DeltaView.from_dict(view.to_dict())
        assert rebuilt == view

    def test_from_delta_uses_challenger_delta_fields(self) -> None:
        champion_pnl = Decimal("1000")
        challenger_pnl = Decimal("1500")
        delta = challenger_delta_obj(champion_pnl, challenger_pnl)
        view = DeltaView.from_delta(delta)
        assert view.net_pnl_delta == Decimal("500")
        assert view.challenger_name == "candidate"


def challenger_delta_obj(champion_pnl: Decimal, challenger_pnl: Decimal):
    """Build a WS 7.11 ChallengerDelta for adapter tests."""
    from fno_ai_paper_trading.evaluation.champion_challenger import ChallengerDelta

    return ChallengerDelta(
        challenger_name="candidate",
        period="out_of_sample",
        champion_net_pnl=champion_pnl,
        challenger_net_pnl=challenger_pnl,
        net_pnl_delta=challenger_pnl - champion_pnl,
        win_rate_delta=Decimal("0"),
        champion_win_rate=Decimal("50"),
        challenger_win_rate=Decimal("50"),
        champion_max_drawdown_pct=Decimal("2.0"),
        challenger_max_drawdown_pct=Decimal("1.5"),
        max_drawdown_pct_delta=Decimal("-0.5"),
        champion_profit_factor=Decimal("1.2"),
        challenger_profit_factor=Decimal("1.5"),
        beats_champion=True,
    )


# ---------------------------------------------------------------------------
# end-to-end: gate over a real WS 7.11 MultiPeriodComparison
# ---------------------------------------------------------------------------

class TestPromotionEndToEnd:
    def _comparison(self, candidate):
        from datetime import date, datetime, time, timedelta

        from fno_ai_paper_trading.evaluation.champion_challenger import ChampionChallenger
        from fno_ai_paper_trading.evaluation.five_year import DayBars, PeriodSplitConfig
        from fno_ai_paper_trading.models.enums import InstrumentType
        from fno_ai_paper_trading.models.instruments import Instrument
        from fno_ai_paper_trading.models.market import MarketPrice
        from fno_ai_paper_trading.strategies import MovingAverageCrossStrategy

        inst = Instrument(
            symbol="NIFTY1",
            instrument_type=InstrumentType.FUTURE,
            underlying_symbol="NIFTY",
        )

        def day_data(day: date, up: bool) -> DayBars:
            bars: list[MarketPrice] = []
            price = Decimal("25000")
            for i in range(60):
                if i >= 30:
                    price += Decimal("100") if up else Decimal("-100")
                bars.append(
                    MarketPrice(
                        instrument=inst,
                        timestamp=datetime.combine(day, time(9, 15)) + timedelta(minutes=i),
                        open=price - Decimal("2"),
                        high=price + Decimal("5"),
                        low=price - Decimal("5"),
                        close=price,
                        volume=1000,
                    )
                )
            return DayBars(day=day, bars=tuple(bars), source_hash=f"h-{day.isoformat()}")

        days = [
            day_data(date(2026, 9, 1), up=True),
            day_data(date(2026, 9, 2), up=False),
            day_data(date(2026, 9, 3), up=True),
            day_data(date(2026, 9, 4), up=False),
            day_data(date(2026, 9, 5), up=True),
        ]
        return ChampionChallenger(split=PeriodSplitConfig()).run_days(
            days, MovingAverageCrossStrategy(), [candidate]
        )

    def test_promotes_identical_twin_on_oos(self) -> None:
        from fno_ai_paper_trading.strategies import RegimeFilteredMovingAverageCross

        twin = RegimeFilteredMovingAverageCross(
            allowed_trends=("UP", "DOWN", "SIDEWAYS")
        )
        comparison = self._comparison(twin)
        verdict = PromotionGate().evaluate_from_comparison(
            comparison, twin.name, criteria=PromotionCriteria(oos_min_days=1)
        )
        assert verdict.decision == "PROMOTE"

    def test_rejects_suppressed_candidate_on_oos(self) -> None:
        from fno_ai_paper_trading.strategies import RegimeFilteredMovingAverageCross

        candidate = RegimeFilteredMovingAverageCross(allowed_trends=("DOWN",))
        comparison = self._comparison(candidate)
        verdict = PromotionGate().evaluate_from_comparison(
            comparison, candidate.name, criteria=PromotionCriteria(oos_min_days=1)
        )
        assert verdict.decision == "REJECT"
        assert any("does not beat the champion" in r for r in verdict.reasons)


class TestRegistryModelVersion:
    def test_model_version_round_trip(self) -> None:
        version = ModelVersion(
            version_id="model_1",
            strategy_name="regime_filtered_ma_cross",
            strategy_params={"allowed_trends": ["UP"]},
            feature_version="features-1",
            description="candidate",
            status="ACTIVE",
            promoted_at="2026-09-12T00:00:00",
            evidence={"oos_pnl": "1500"},
        )
        payload = version.to_dict()
        assert payload["strategy_params"] == {"allowed_trends": ["UP"]}
        assert payload["status"] == "ACTIVE"