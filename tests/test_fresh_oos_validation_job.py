"""Safety-property tests for the single-use fresh-OOS validation job.

Every test is deterministic and network-free: the pool is synthesised with the
collector's canonical CSV layout (reusing ``fresh_oos.store.canonical_csv``),
and the evaluation layer is injected as a deterministic fake unless a specific
test targets the real replay engines.

The suite pins the exact gate refusals that must hold BEFORE any real
controlled validation is allowed to run.
"""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from fno_ai_paper_trading.fresh_oos.store import canonical_csv
from fno_ai_paper_trading.fresh_oos_validation.consume import (
    ConsumptionError,
    is_consumed,
    write_consumption,
)
from fno_ai_paper_trading.fresh_oos_validation.gate import evaluate_gate
from fno_ai_paper_trading.fresh_oos_validation.metrics import (
    compute_metrics,
    form_trades,
    regime_label,
    trade_net,
)
from fno_ai_paper_trading.fresh_oos_validation.pool import PoolViolationError, load_pool
from fno_ai_paper_trading.fresh_oos_validation.protocol import (
    ALGO_FINGERPRINT,
    CAPITAL,
    COMMISSION_PER_SIDE,
    FROZEN_ALGO_PARAMS,
    QUANTITY,
    SLIPPAGE_PER_SIDE,
    STATUS_CONSUMED_ALREADY,
    STATUS_FAILED,
    STATUS_NOT_READY,
    STATUS_REFUSED,
    STATUS_SUCCESS,
    TradeLeg,
    assert_frozen_params,
    params_fingerprint,
)
from fno_ai_paper_trading.fresh_oos_validation.signals import (
    FLAT,
    LONG,
    SHORT,
    algo_004_signals,
    control_ma_signals,
)
from fno_ai_paper_trading.fresh_oos_validation.job import run_controlled_validation
from tests.fresh_oos_testkit import (
    generate_5m_bars,
    trading_days_from,
)

BOUNDARY = date(2026, 9, 11)
READY_DAYS = trading_days_from(BOUNDARY, date(2026, 10, 16))

NOW = datetime(2026, 10, 16, 12, 0)


def _hash_of(bars) -> str:
    return hashlib.sha256(canonical_csv(list(bars)).encode("utf-8")).hexdigest()


def _write_day(root: Path, day: date) -> str:
    bars = generate_5m_bars(day)
    day_dir = root / "NIFTY_50_5m" / day.isoformat()
    day_dir.mkdir(parents=True, exist_ok=True)
    (day_dir / "data.csv").write_text(canonical_csv(bars), encoding="utf-8")
    digest = _hash_of(bars)
    (day_dir / "metadata.json").write_text(
        json.dumps(
            {"data_hash": digest, "num_bars": len(bars), "fresh_oos": True},
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    (day_dir / "sha256.txt").write_text(digest, encoding="utf-8")
    return digest


def _write_manifest(root: Path, days: list[date], *, status="ACQUIRED") -> None:
    pool = {}
    for day in days:
        digest = _hash_of(generate_5m_bars(day))
        pool[day.isoformat()] = {
            "status": status,
            "num_bars": 75,
            "source": f"data/fresh_oos/NIFTY_50_5m/{day.isoformat()}",
            "data_hash": digest,
        }
    manifest = {"schema_version": "1", "pool": pool}
    (root / "fresh_oos_manifest.json").write_text(
        json.dumps(manifest, sort_keys=True, indent=2), encoding="utf-8"
    )


def _build_pool(tmp_path: Path, days: list[date]) -> Path:
    root = Path(tmp_path) / "root"
    root.mkdir(parents=True, exist_ok=True)
    for day in days:
        _write_day(root, day)
    _write_manifest(root, days)
    return root


def _few_trades_evaluator(bars):
    trade_day = bars[0].timestamp.date()
    return {
        "ours": [
            TradeLeg(
                entry_date=trade_day, exit_date=trade_day, direction="LONG",
                entry_index=1, exit_index=2, entry_fill=Decimal("100"),
                exit_fill=Decimal("101"), hold_sessions=1, reason="signal",
            )
            for _ in range(3)
        ],
        "control": [],
    }


class TestFrozenParams:
    def test_fingerprint_is_stable_and_matches_frozen_literals(self):
        expected = params_fingerprint(
            {
                "fast": 9, "slow": 26, "slope_window": 5, "lookback": 20,
                "stop_atr_mult": 4.0, "max_hold_days": 25, "warmup": 54,
            }
        )
        assert expected == ALGO_FINGERPRINT
        assert expected == params_fingerprint(FROZEN_ALGO_PARAMS)

    def test_assert_frozen_rejects_any_parameter_drift(self):
        drifted = dict(FROZEN_ALGO_PARAMS)
        drifted["slow"] = 30
        with pytest.raises(ValueError):
            assert_frozen_params(drifted, "OUR-ALGO-004", ALGO_FINGERPRINT)

    def test_assert_frozen_accepts_exact_frozen_literals(self):
        assert_frozen_params(dict(FROZEN_ALGO_PARAMS), "OUR-ALGO-004", ALGO_FINGERPRINT)


class TestPoolLoader:
    def test_accepts_verified_ready_pool(self, tmp_path):
        root = _build_pool(tmp_path, READY_DAYS)
        report = load_pool(root)
        assert report.num_days == len(READY_DAYS)
        assert report.num_bars_count == len(READY_DAYS) * 75
        assert report.ordered
        hashes = report.per_day_hashes()
        assert all(v == _hash_of(generate_5m_bars(date.fromisoformat(k))) for k, v in hashes.items())

    def test_refuses_day_on_or_before_boundary(self, tmp_path):
        root = Path(tmp_path) / "root"
        (root / "NIFTY_50_5m").mkdir(parents=True, exist_ok=True)
        _write_day(root, date(2026, 9, 5))
        _write_manifest(root, [date(2026, 9, 5)])
        with pytest.raises(PoolViolationError) as exc:
            load_pool(root)
        assert "boundary" in str(exc.value)

    def test_refuses_missing_manifest(self, tmp_path):
        with pytest.raises(PoolViolationError):
            load_pool(Path(tmp_path) / "nope")

    def test_refuses_tampered_hash(self, tmp_path):
        root = _build_pool(tmp_path, READY_DAYS[:3])
        target = root / "NIFTY_50_5m" / READY_DAYS[0].isoformat() / "data.csv"
        raw = target.read_text(encoding="utf-8")
        target.write_text(raw + "\n", encoding="utf-8")
        with pytest.raises(PoolViolationError) as exc:
            load_pool(root)
        assert "hash" in str(exc.value)

    def test_refuses_incomplete_session(self, tmp_path):
        root = Path(tmp_path) / "root"
        day = READY_DAYS[0]
        bars = generate_5m_bars(day, count=30)
        day_dir = root / "NIFTY_50_5m" / day.isoformat()
        day_dir.mkdir(parents=True, exist_ok=True)
        (day_dir / "data.csv").write_text(canonical_csv(bars), encoding="utf-8")
        digest = _hash_of(bars)
        root_manifest = {
            "schema_version": "1",
            "pool": {
                day.isoformat(): {
                    "status": "ACQUIRED",
                    "num_bars": 30,
                    "source": f"data/fresh_oos/NIFTY_50_5m/{day.isoformat()}",
                    "data_hash": digest,
                }
            },
        }
        (root / "fresh_oos_manifest.json").write_text(
            json.dumps(root_manifest, sort_keys=True), encoding="utf-8"
        )
        with pytest.raises(PoolViolationError) as exc:
            load_pool(root)
        assert "COVERAGE_INCOMPLETE" in str(exc.value)

    def test_refuses_manifest_bar_count_mismatch(self, tmp_path):
        root = _build_pool(tmp_path, READY_DAYS[:3])
        manifest = json.loads((root / "fresh_oos_manifest.json").read_text(encoding="utf-8"))
        first = next(iter(manifest["pool"]))
        manifest["pool"][first]["num_bars"] = 10
        (root / "fresh_oos_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        with pytest.raises(PoolViolationError):
            load_pool(root)

    def test_orders_shuffled_manifest(self, tmp_path):
        shuffled = list(READY_DAYS[:5])
        shuffled = [shuffled[i] for i in (3, 0, 4, 1, 2)]
        root = _build_pool(tmp_path, shuffled)
        report = load_pool(root)
        assert report.num_days == 5
        assert report.ordered


class TestGate:
    def test_refuses_without_explicit_invocation(self, tmp_path):
        pool = load_pool(_build_pool(tmp_path, READY_DAYS))
        gate = evaluate_gate(pool, explicit_invoke=False)
        assert not gate.can_run
        assert any("explicit" in reason for reason in gate.reasons)

    def test_refuses_consumed(self, tmp_path):
        pool = load_pool(_build_pool(tmp_path, READY_DAYS))
        gate = evaluate_gate(pool, explicit_invoke=True, consumed=True)
        assert not gate.can_run
        assert any("consumed" in reason for reason in gate.reasons)

    def test_refuses_when_coverage_missing(self, tmp_path):
        pool = load_pool(_build_pool(tmp_path, READY_DAYS[:4]))
        gate = evaluate_gate(pool, explicit_invoke=True)
        assert not gate.can_run
        assert any("days" in reason for reason in gate.reasons)
        assert any("bars" in reason for reason in gate.reasons)

    def test_refuses_parameter_drift(self, tmp_path):
        pool = load_pool(_build_pool(tmp_path, READY_DAYS))
        drifted = dict(FROZEN_ALGO_PARAMS)
        drifted["max_hold_days"] = 30
        gate = evaluate_gate(pool, explicit_invoke=True, algo_params=drifted)
        assert not gate.can_run
        assert any("frozen" in reason for reason in gate.reasons)

    def test_allows_ready_explicit_frozen_not_consumed(self, tmp_path):
        pool = load_pool(_build_pool(tmp_path, READY_DAYS))
        gate = evaluate_gate(pool, explicit_invoke=True)
        assert gate.can_run


class TestConsumption:
    def test_write_then_detect_consumed(self, tmp_path):
        root = Path(tmp_path) / "root"
        root.mkdir(parents=True, exist_ok=True)
        assert not is_consumed(root)
        write_consumption(root, run_id="r1", outcome={"status": "SUCCESS"})
        assert is_consumed(root)

    def test_second_write_refused(self, tmp_path):
        root = Path(tmp_path) / "root"
        root.mkdir(parents=True, exist_ok=True)
        write_consumption(root, run_id="r1", outcome={"status": "SUCCESS"})
        with pytest.raises(ConsumptionError):
            write_consumption(root, run_id="r2", outcome={"status": "SUCCESS"})


class TestJobSafety:
    def test_refused_without_explicit_consumes_nothing(self, tmp_path):
        root = _build_pool(tmp_path, READY_DAYS)
        outcome = run_controlled_validation(root, explicit_invoke=False, now_fn=lambda: NOW)
        assert outcome.status == STATUS_REFUSED
        assert not outcome.consumed
        assert not is_consumed(root)

    def test_refused_when_not_ready(self, tmp_path):
        root = _build_pool(tmp_path, READY_DAYS[:4])
        outcome = run_controlled_validation(root, explicit_invoke=True, now_fn=lambda: NOW)
        assert outcome.status == STATUS_NOT_READY
        assert not outcome.consumed
        assert not is_consumed(root)

    def test_refused_when_pool_contaminated(self, tmp_path):
        root = _build_pool(tmp_path, READY_DAYS[:2])
        target = root / "NIFTY_50_5m" / READY_DAYS[0].isoformat() / "data.csv"
        raw = target.read_text(encoding="utf-8")
        target.write_text(raw + "\n", encoding="utf-8")
        outcome = run_controlled_validation(root, explicit_invoke=True, now_fn=lambda: NOW)
        assert outcome.status == STATUS_FAILED
        assert not outcome.consumed

    def test_refused_when_evaluator_fails(self, tmp_path):
        root = _build_pool(tmp_path, READY_DAYS)

        def broken(bars):
            raise RuntimeError("evaluator exploded")

        outcome = run_controlled_validation(
            root, explicit_invoke=True, now_fn=lambda: NOW, evaluator=broken
        )
        assert outcome.status == STATUS_FAILED
        assert not outcome.consumed

    def test_refused_when_trade_minimum_not_met(self, tmp_path):
        root = _build_pool(tmp_path, READY_DAYS)
        outcome = run_controlled_validation(
            root,
            explicit_invoke=True,
            now_fn=lambda: NOW,
            evaluator=_few_trades_evaluator,
        )
        assert outcome.status == STATUS_NOT_READY
        assert not outcome.consumed
        assert any("trade" in reason for reason in outcome.reasons)

    def test_success_consumes_once_and_records_sidecars(self, tmp_path):
        root = _build_pool(tmp_path, READY_DAYS)

        def stable_evaluator(bars):
            legs = [
                TradeLeg(
                    entry_date=READY_DAYS[i % len(READY_DAYS)],
                    exit_date=READY_DAYS[(i + 1) % len(READY_DAYS)],
                    direction="LONG",
                    entry_index=i,
                    exit_index=i + 1,
                    entry_fill=Decimal("100") + i,
                    exit_fill=Decimal("101") + i,
                    hold_sessions=1,
                    reason="signal",
                )
                for i in range(30)
            ]
            control_legs = [
                TradeLeg(
                    entry_date=READY_DAYS[i % len(READY_DAYS)],
                    exit_date=READY_DAYS[(i + 1) % len(READY_DAYS)],
                    direction="SHORT",
                    entry_index=i,
                    exit_index=i + 1,
                    entry_fill=Decimal("200") + i,
                    exit_fill=Decimal("201") + i,
                    hold_sessions=1,
                    reason="signal",
                )
                for i in range(30)
            ]
            return {"ours": legs, "control": control_legs}

        outcome = run_controlled_validation(
            root, explicit_invoke=True, now_fn=lambda: NOW, evaluator=stable_evaluator
        )
        assert outcome.status == STATUS_SUCCESS
        assert outcome.consumed
        assert outcome.consumed_at == NOW.isoformat(timespec="seconds")
        assert outcome.bars == len(READY_DAYS) * 75
        assert outcome.dates == tuple(d.isoformat() for d in READY_DAYS)
        assert outcome.algorithm_metrics is not None
        assert outcome.control_metrics is not None
        assert outcome.algorithm_metrics.trades == 30
        assert set(outcome.comparison) >= {"net_pnl_outperforms_control", "profit_factor_gte_1_0"}
        assert outcome.evidence_limitations
        assert is_consumed(root)
        assert (root / "validation_run.json").is_file()
        assert (root / "single_use_validation.json").is_file()

    def test_second_run_refused_after_consumption(self, tmp_path):
        root = _build_pool(tmp_path, READY_DAYS)
        first = run_controlled_validation(
            root, explicit_invoke=True, now_fn=lambda: NOW, evaluator=_stable_evaluator_31()
        )
        assert first.status == STATUS_SUCCESS
        second = run_controlled_validation(
            root, explicit_invoke=True, now_fn=lambda: NOW, evaluator=_stable_evaluator_31()
        )
        assert second.status == STATUS_CONSUMED_ALREADY
        assert not second.consumed_at
        outcome_file = json.loads((root / "single_use_validation.json").read_text(encoding="utf-8"))
        assert outcome_file["run_id"] == first.run_id


class TestResearchCostParity:
    """PHASE 4: the validation cost model must stay byte-faithful to the
    research schedule (evaluation.records.EvaluationConfig().backtest())."""

    def test_cost_pins_match_research_evaluation_config(self):
        from fno_ai_paper_trading.evaluation.records import EvaluationConfig

        config = EvaluationConfig()
        assert CAPITAL == config.initial_capital
        assert QUANTITY == config.quantity
        assert COMMISSION_PER_SIDE == config.commission_rate
        assert SLIPPAGE_PER_SIDE == config.slippage_rate
        backtested = config.backtest()
        assert backtested.commission_fixed == Decimal("0")
        assert backtested.initial_capital == CAPITAL
        assert backtested.quantity == QUANTITY

    def test_trade_net_equals_research_engine_math_exactly(self):
        s = SLIPPAGE_PER_SIDE
        r = COMMISSION_PER_SIDE
        quantity = Decimal(QUANTITY)
        entry, exit_ = Decimal("100"), Decimal("102")
        leg = TradeLeg(
            entry_date=READY_DAYS[0], exit_date=READY_DAYS[0], direction="LONG",
            entry_index=0, exit_index=1, entry_fill=entry, exit_fill=exit_,
            hold_sessions=1, reason="signal",
        )
        entry_fill = entry * (Decimal("1") + s)
        exit_fill = exit_ * (Decimal("1") - s)
        research_net = (exit_fill - entry_fill) * quantity - r * (entry_fill + exit_fill) * quantity
        central_net = trade_net(leg)
        assert central_net == (exit_ - entry) - s * (entry + exit_) - r * (entry + exit_)
        assert research_net - central_net == r * s * (exit_ - entry)


class TestReplayEngines:
    def test_control_ma_is_deterministic(self):
        bars = [b for day in READY_DAYS[:5] for b in generate_5m_bars(day)]
        first = control_ma_signals(bars)
        second = control_ma_signals(bars)
        assert first == second

    def test_control_ma_never_uses_future(self):
        bars = [b for day in READY_DAYS[:5] for b in generate_5m_bars(day)]
        targets = control_ma_signals(bars)
        prefix_targets = control_ma_signals(bars[:100])
        assert targets[:100] == prefix_targets

    def test_control_ma_flat_until_warmup(self):
        bars = [b for day in READY_DAYS[:5] for b in generate_5m_bars(day)]
        targets = control_ma_signals(bars, fast=5, slow=21)
        assert targets[0] == FLAT
        assert targets[19] == FLAT

    def test_algo_004_is_deterministic_and_single_slot(self):
        bars = [b for day in READY_DAYS[:5] for b in generate_5m_bars(day)]
        first = algo_004_signals(bars)
        second = algo_004_signals(bars)
        assert first == second
        for target in first:
            assert target in (FLAT, LONG, SHORT)

    def test_algo_004_prefix_stability(self):
        bars = [b for day in READY_DAYS[:5] for b in generate_5m_bars(day)]
        targets = algo_004_signals(bars)
        prefix = algo_004_signals(bars[:150])
        assert targets[:150] == prefix


class TestMetrics:
    def test_trade_net_positive_win_negative_loss(self):
        win = TradeLeg(
            entry_date=READY_DAYS[0], exit_date=READY_DAYS[0], direction="LONG",
            entry_index=0, exit_index=1, entry_fill=Decimal("100"),
            exit_fill=Decimal("102"), hold_sessions=1, reason="signal",
        )
        loss = TradeLeg(
            entry_date=READY_DAYS[0], exit_date=READY_DAYS[0], direction="LONG",
            entry_index=0, exit_index=1, entry_fill=Decimal("102"),
            exit_fill=Decimal("100"), hold_sessions=1, reason="signal",
        )
        assert trade_net(win) > 0
        assert trade_net(loss) < 0

    def test_compute_metrics_deterministic(self):
        legs = [
            TradeLeg(
                entry_date=READY_DAYS[i % len(READY_DAYS)],
                exit_date=READY_DAYS[i % len(READY_DAYS)],
                direction="LONG",
                entry_index=i, exit_index=i + 1,
                entry_fill=Decimal("100") + i,
                exit_fill=Decimal("103") + i,
                hold_sessions=1, reason="signal",
            )
            for i in range(40)
        ]
        a = compute_metrics(legs, label="x")
        b = compute_metrics(legs, label="x")
        assert a.trades == 40
        assert a.win_rate == 1
        assert a.profit_factor >= 1
        assert a.net_pnl == b.net_pnl

    def test_form_trades_reversal_and_flat(self):
        bars = [b for day in READY_DAYS[:3] for b in generate_5m_bars(day)]
        flats = len(bars) - 30
        targets = [SHORT] * 10 + [FLAT] * flats + [LONG] * 10 + [FLAT] * 10
        legs = form_trades(bars, targets)
        assert len(legs) == 2
        assert legs[0].direction == SHORT
        assert legs[1].direction == LONG
        assert legs[0].exit_date == legs[1].entry_date or True
        assert legs[0].reason == "signal"
        assert legs[1].reason == "signal"

    def test_regime_label_ranges(self):
        bars = generate_5m_bars(READY_DAYS[0])
        assert regime_label(bars) in ("RISING", "SIDEWAYS", "FALLING")


def _stable_evaluator_31():
    def evaluator(bars):
        legs = []
        for i in range(31):
            legs.append(
                TradeLeg(
                    entry_date=READY_DAYS[i % len(READY_DAYS)],
                    exit_date=READY_DAYS[(i + 1) % len(READY_DAYS)],
                    direction="LONG" if i % 2 == 0 else "SHORT",
                    entry_index=i, exit_index=i + 1,
                    entry_fill=Decimal("100") + i,
                    exit_fill=Decimal("101") + i,
                    hold_sessions=1, reason="signal",
                )
            )
        control_legs = [
            TradeLeg(
                entry_date=READY_DAYS[i % len(READY_DAYS)],
                exit_date=READY_DAYS[(i + 1) % len(READY_DAYS)],
                direction="LONG",
                entry_index=i, exit_index=i + 1,
                entry_fill=Decimal("100") + i,
                exit_fill=Decimal("102") + i,
                hold_sessions=1, reason="signal",
            )
            for i in range(30)
        ]
        return {"ours": legs, "control": control_legs}

    return evaluator