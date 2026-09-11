"""Tests for WS 7.10 adaptive learning — outcome analysis and candidate generation.

Covers the evidence-only outcome summary, hard thresholded candidate generation,
and the structural no-execution boundary (the learning package must not import
strategy, risk, sizing, stop-loss, broker, or portfolio modules).
"""
from __future__ import annotations

import ast
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from fno_ai_paper_trading.ai.decision import AIDecision
from fno_ai_paper_trading.experience import builders
from fno_ai_paper_trading.experience.enums import (
    AdvisoryUsage,
    DataQualityStatus,
    DecisionStatus,
    ExperienceSourceType,
)
from fno_ai_paper_trading.experience.records import (
    AdvisoryEvidence,
    DecisionContext,
    ExperienceRecord,
    TradeOutcome,
)
from fno_ai_paper_trading.learning.candidates import (
    CandidateConfig,
    CandidateGenerator,
)
from fno_ai_paper_trading.learning.outcome import analyze_outcomes
from fno_ai_paper_trading.models.enums import InstrumentType, OrderSide, Signal
from fno_ai_paper_trading.models.instruments import Instrument

_SC = DecisionStatus.EXECUTED


def _future() -> Instrument:
    return Instrument(symbol="NIFTY1", instrument_type=InstrumentType.FUTURE, underlying_symbol="NIFTY")


def _ts(hour: int = 10, minute: int = 0, day: int = 2) -> datetime:
    return datetime(2026, 9, day, hour, minute, tzinfo=timezone.utc)


def _decision(
    *,
    signal: Signal = Signal.BUY,
    regime_label: str = "up_normal",
    strategy_name: str = "ma_cross",
    strategy_version: str = "1.0.0",
    decision_status: DecisionStatus = _SC,
    decision_timestamp: datetime | None = None,
) -> DecisionContext:
    return builders.build_decision(
        instrument=_future(),
        decision_timestamp=decision_timestamp or _ts(),
        timeframe="5m",
        signal=signal,
        confidence="0.80",
        strategy_name=strategy_name,
        strategy_version=strategy_version,
        data_reference="ref",
        features={"ma_fast": Decimal("24100"), "ma_slow": Decimal("24050")},
        decision_status=decision_status,
        regime_label=regime_label,
        data_quality=DataQualityStatus.VALIDATED,
    )


def _outcome(*, realized_pnl: str = "1000", trade_id: str = "t-1") -> TradeOutcome:
    return builders.build_outcome(
        trade_id=trade_id,
        side=OrderSide.BUY,
        entry_price="24100",
        entry_timestamp=_ts(9, 30),
        exit_price="24150",
        exit_timestamp=_ts(10, 30),
        quantity=75,
        realized_pnl=realized_pnl,
        total_costs="40",
    )


def _advisory(usage: AdvisoryUsage = AdvisoryUsage.ACCEPTED) -> AdvisoryEvidence:
    return builders.build_advisory(
        AIDecision(
            action=Signal.BUY,
            confidence=Decimal("0.82"),
            rationale="signal aligned",
            features_considered={"ma_fast": Decimal("24100")},
            market_regime="up_normal",
            model_name="test",
            model_version="1.0",
            timestamp=_ts(),
            data_reference="ref",
        ),
        usage=usage,
    )


def _complete_record(*, realized_pnl: str = "1000", regime: str = "up_normal", trade_id: str = "t-1") -> ExperienceRecord:
    return builders.build_record(
        decision=_decision(regime_label=regime),
        source=ExperienceSourceType.PAPER_SESSION,
        recorded_at=_ts(11, 0),
        outcome=_outcome(realized_pnl=realized_pnl, trade_id=trade_id),
        advisory=_advisory(),
    )


def _pending_record(*, regime: str = "up_normal") -> ExperienceRecord:
    return builders.build_record(
        decision=_decision(regime_label=regime),
        source=ExperienceSourceType.PAPER_SESSION,
        recorded_at=_ts(11, 0),
        outcome=None,
        advisory=None,
    )


def _no_trade_record(*, regime: str = "up_normal", decision_status: DecisionStatus = DecisionStatus.SKIPPED) -> ExperienceRecord:
    return builders.build_record(
        decision=_decision(regime_label=regime, decision_status=decision_status),
        source=ExperienceSourceType.PAPER_SESSION,
        recorded_at=_ts(11, 0),
        outcome=None,
        advisory=None,
    )


class TestOutcomeAnalysis:
    def test_basic_metrics(self):
        r1 = _complete_record(realized_pnl="2000", trade_id="a")
        r2 = _complete_record(realized_pnl="-1000", trade_id="b")
        r3 = _complete_record(realized_pnl="0", trade_id="c")
        r4 = _pending_record()
        r5 = _no_trade_record()

        analysis = analyze_outcomes([r1, r2, r3, r4, r5])

        assert analysis.completed == 3
        assert analysis.wins == 1
        assert analysis.losses == 1
        assert analysis.breakeven == 1
        assert analysis.pending_outcome == 1
        assert analysis.no_trade == 1
        assert analysis.net_pnl == Decimal("1000")
        assert analysis.gross_win == Decimal("2000")
        assert analysis.gross_loss == Decimal("1000")
        assert analysis.win_rate == Decimal("1") / Decimal("3")
        assert analysis.expectancy == Decimal("1000") / Decimal("3")
        assert analysis.avg_win == Decimal("2000")
        assert analysis.avg_loss == Decimal("-1000")
        assert analysis.profit_factor == Decimal("2")
        assert analysis.total_costs == Decimal("120")

    def test_all_pending_gives_zeros(self):
        analysis = analyze_outcomes([_pending_record(), _pending_record()])
        assert analysis.completed == 0
        assert analysis.wins == 0
        assert analysis.win_rate is None
        assert analysis.expectancy is None
        assert analysis.avg_win is None
        assert analysis.avg_loss is None
        assert analysis.profit_factor is None
        assert analysis.pending_outcome == 2

    def test_profit_factor_none_when_no_losses(self):
        analysis = analyze_outcomes([_complete_record(realized_pnl="100"), _complete_record(realized_pnl="200")])
        assert analysis.profit_factor is None
        assert analysis.losses == 0
        assert analysis.avg_loss is None

    def test_to_dict_deterministic(self):
        records = [_complete_record(realized_pnl="500", trade_id="a"), _complete_record(realized_pnl="-500", trade_id="b")]
        assert analyze_outcomes(records).to_dict() == analyze_outcomes(records).to_dict()

    def test_per_regime_breakdown(self):
        r1 = _complete_record(regime="up_normal", realized_pnl="100", trade_id="a")
        r2 = _complete_record(regime="up_normal", realized_pnl="200", trade_id="b")
        r3 = _complete_record(regime="down", realized_pnl="-100", trade_id="c")
        analysis = analyze_outcomes([r1, r2, r3])
        regimes = {s.regime_label: s for s in analysis.per_regime}
        assert regimes["up_normal"].completed == 2
        assert regimes["up_normal"].wins == 2
        assert regimes["up_normal"].win_rate == Decimal("1")
        assert regimes["down"].completed == 1
        assert regimes["down"].wins == 0
        assert regimes["down"].win_rate == Decimal("0")

    def test_per_signal_breakdown(self):
        r1 = builders.build_record(
            decision=_decision(signal=Signal.BUY, regime_label="up_normal"),
            source=ExperienceSourceType.PAPER_SESSION, recorded_at=_ts(),
            outcome=_outcome(realized_pnl="100", trade_id="a"),
        )
        r2 = builders.build_record(
            decision=_decision(signal=Signal.SELL, regime_label="up_normal"),
            source=ExperienceSourceType.PAPER_SESSION, recorded_at=_ts(11),
            outcome=_outcome(realized_pnl="-50", trade_id="b"),
        )
        analysis = analyze_outcomes([r1, r2])
        by_sig = {s.signal: s for s in analysis.per_signal}
        assert by_sig[Signal.BUY].completed == 1
        assert by_sig[Signal.SELL].completed == 1

    def test_per_advisory_counts_only_present(self):
        with_advisory = _complete_record(realized_pnl="1000", trade_id="a")
        without_advisory = builders.build_record(
            decision=_decision(regime_label="up_normal"),
            source=ExperienceSourceType.PAPER_SESSION, recorded_at=_ts(11),
            outcome=_outcome(realized_pnl="500", trade_id="b"),
            advisory=None,
        )
        rejected = builders.build_record(
            decision=_decision(regime_label="up_normal", decision_timestamp=_ts(10, 15)),
            source=ExperienceSourceType.PAPER_SESSION, recorded_at=_ts(11, 15),
            outcome=_outcome(realized_pnl="-200", trade_id="c"),
            advisory=_advisory(AdvisoryUsage.REJECTED),
        )
        analysis = analyze_outcomes([with_advisory, without_advisory, rejected])
        by_usage = {s.usage: s for s in analysis.per_advisory}
        assert by_usage[AdvisoryUsage.ACCEPTED].completed == 1
        assert by_usage[AdvisoryUsage.REJECTED].completed == 1
        assert len(analysis.per_advisory) == 2


class TestCandidateConfig:
    def test_defaults(self):
        config = CandidateConfig()
        assert config.min_completed == 20
        assert config.min_completed_per_regime == 10
        assert config.win_rate_delta == Decimal("0.05")

    def test_immutable(self):
        config = CandidateConfig(min_completed=30)
        with pytest.raises(Exception):
            config.min_completed = 25


def _records(n_win: int, n_loss: int, *, regime: str = "up_normal") -> list[ExperienceRecord]:
    records: list[ExperienceRecord] = []
    for i in range(n_win + n_loss):
        pnl = "100" if i < n_win else "-100"
        records.append(_complete_record(realized_pnl=pnl, trade_id=f"t-{i}"))
    return records


class TestCandidateGenerator:
    def test_insufficient_when_too_few(self):
        result = CandidateGenerator().generate(_records(1, 1))
        assert not result.emitted
        assert result.candidates == ()
        assert any("min_completed" in r for r in result.insufficient_reasons)

    def test_single_loss_does_not_generate(self):
        result = CandidateGenerator().generate([_complete_record(realized_pnl="-100")])
        assert not result.emitted
        assert len(result.candidates) == 0

    def test_single_win_does_not_generate(self):
        result = CandidateGenerator().generate([_complete_record(realized_pnl="500")])
        assert not result.emitted

    def test_regime_outperform_emitted_when_threshold_met(self):
        up_records = [_complete_record(realized_pnl="100", regime="up_normal", trade_id=f"up-{i}") for i in range(12)]
        down_records = [_complete_record(realized_pnl="-100", regime="down", trade_id=f"dn-{i}") for i in range(10)]
        result = CandidateGenerator().generate(up_records + down_records)
        assert result.emitted
        kinds = [c.kind for c in result.candidates]
        assert "regime_focus" in kinds
        assert result.summary.completed == 22

    def test_no_regime_candidate_when_regime_too_small(self):
        up_records = [_complete_record(realized_pnl="100", regime="up_normal", trade_id=f"u-{i}") for i in range(15)]
        down_records = [_complete_record(realized_pnl="-100", regime="down", trade_id=f"d-{i}") for i in range(5)]
        result = CandidateGenerator().generate(up_records + down_records)
        regimes_in_candidates = {c.evidence["regime_label"] for c in result.candidates if c.kind == "regime_focus"}
        assert "down" not in regimes_in_candidates
        assert any("down" in r and "min_completed_per_regime" in r for r in result.insufficient_reasons)
        assert "up_normal" in regimes_in_candidates

    def test_candidate_to_dict_deterministic(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        generator = CandidateGenerator(now=lambda: now)
        records = [_complete_record(realized_pnl="100", regime="up_normal", trade_id=f"t-{i}") for i in range(25)]
        result = generator.generate(records)
        if result.candidates:
            candidate_dict = result.candidates[0].to_dict()
            assert isinstance(candidate_dict, dict)
            assert "kind" in candidate_dict
            assert candidate_dict["created_at"] == now.isoformat()

    def test_advisory_alignment_emitted_when_split_clear(self):
        accepted = [
            builders.build_record(
                decision=_decision(regime_label="up_normal", decision_timestamp=_ts(10, i % 60)),
                source=ExperienceSourceType.PAPER_SESSION, recorded_at=_ts(11, i % 60),
                outcome=_outcome(realized_pnl="100", trade_id=f"acc-{i}"),
                advisory=_advisory(AdvisoryUsage.ACCEPTED),
            )
            for i in range(15)
        ]
        rejected = [
            builders.build_record(
                decision=_decision(regime_label="up_normal", decision_timestamp=_ts(10, (i + 30) % 60)),
                source=ExperienceSourceType.PAPER_SESSION, recorded_at=_ts(11, (i + 30) % 60),
                outcome=_outcome(realized_pnl="-100", trade_id=f"rej-{i}"),
                advisory=_advisory(AdvisoryUsage.REJECTED),
            )
            for i in range(15)
        ]
        generator = CandidateGenerator(config=CandidateConfig(min_completed_per_advisory_bucket=10))
        result = generator.generate(accepted + rejected)
        assert result.emitted
        assert any(c.kind == "advisory_alignment" for c in result.candidates)

    def test_advisory_candidate_skipped_when_only_one_bucket(self):
        accepted = [
            builders.build_record(
                decision=_decision(regime_label="up_normal", decision_timestamp=_ts(10, i % 60)),
                source=ExperienceSourceType.PAPER_SESSION, recorded_at=_ts(11, i % 60),
                outcome=_outcome(realized_pnl="100", trade_id=f"acc-{i}"),
                advisory=_advisory(AdvisoryUsage.ACCEPTED),
            )
            for i in range(25)
        ]
        result = CandidateGenerator().generate(accepted)
        assert not any(c.kind == "advisory_alignment" for c in result.candidates)

    def test_generation_result_immutable(self):
        result = CandidateGenerator().generate(_records(0, 0))
        assert isinstance(result.candidates, tuple)
        assert isinstance(result.insufficient_reasons, tuple)

    def test_fixed_now_injected(self):
        now = datetime(2026, 2, 1, tzinfo=timezone.utc)
        up_records = [_complete_record(realized_pnl="100", regime="up_normal", trade_id=f"t-{i}") for i in range(25)]
        generator = CandidateGenerator(now=lambda: now)
        result = generator.generate(up_records)
        for candidate in result.candidates:
            assert candidate.created_at == now


class TestLearningSafetyBoundary:
    def _forbidden_imports(self) -> set[str]:
        forbidden_prefixes = [
            "fno_ai_paper_trading.strategies",
            "fno_ai_paper_trading.risk",
            "fno_ai_paper_trading.sizer",
            "fno_ai_paper_trading.stop_loss",
            "fno_ai_paper_trading.broker",
            "fno_ai_paper_trading.portfolio",
            "fno_ai_paper_trading.services.trading_service",
            "fno_ai_paper_trading.services.paper_session",
            "fno_ai_paper_trading.services.strategy_service",
        ]
        return set(forbidden_prefixes)

    def test_learning_package_has_no_strategy_risk_broker_imports(self):
        learning_dir = Path(__file__).resolve().parent.parent / "fno_ai_paper_trading" / "learning"
        forbidden = self._forbidden_imports()
        for py_file in learning_dir.glob("*.py"):
            if py_file.name == "__pycache__":
                continue
            tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    module = getattr(node, "module", None) or ""
                    if module in forbidden or any(module.startswith(f) for f in forbidden):
                        pytest.fail(f"{py_file.name} imports forbidden module {module!r}")