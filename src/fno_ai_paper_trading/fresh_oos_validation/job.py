"""Controlled single-use validation orchestration (the job).

``run_controlled_validation`` is the ONLY entry point that may consume the
fresh pool, and it refuses to do anything unless every gate condition holds and
the caller explicitly invoked it.  Sequence (mirrors the lineage's iteration006
discipline):

1. load + re-verify the fresh pool (hash, boundary, structure, order);
2. gate: explicit invocation, coverage, frozen-parameter pins, not-consumed;
3. evaluate algorithm + control over the pool (deterministic replay engines);
4. compute net-of-cost metrics under the single documented cost model;
5. atomically record the single-use consumption sidecar;
6. persist a byte-stable outcome; never edits a prior outcome.

The evaluation layer is a protocol: tests inject deterministic fakes so every
safety property is pinned before any real controlled run (data threshold is not
reached yet, so this job is exercised only through its tests).
"""
from __future__ import annotations

import json
import os
import tempfile
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Callable, Mapping, Sequence

from fno_ai_paper_trading.models.market import MarketPrice
from fno_ai_paper_trading.fresh_oos_validation.consume import (
    is_consumed,
    new_run_id,
    write_consumption,
)
from fno_ai_paper_trading.fresh_oos_validation.gate import evaluate_gate
from fno_ai_paper_trading.fresh_oos_validation.metrics import (
    compute_metrics,
    compare,
    form_trades,
    regime_label,
)
from fno_ai_paper_trading.fresh_oos_validation.pool import PoolReport, load_pool
from fno_ai_paper_trading.fresh_oos_validation.protocol import (
    ALGO_FINGERPRINT,
    ALGO_NAME,
    CONTROL_FINGERPRINT,
    CONTROL_NAME,
    EVIDENCE_LIMITATIONS,
    FROZEN_ALGO_PARAMS,
    FROZEN_CONTROL_PARAMS,
    MIN_TRADES,
    MetricsReport,
    STATUS_CONSUMED_ALREADY,
    STATUS_FAILED,
    STATUS_NOT_READY,
    STATUS_REFUSED,
    STATUS_SUCCESS,
    TradeLeg,
    ValidationOutcome,
    assert_frozen_params,
    cost_model_dict,
)
from fno_ai_paper_trading.fresh_oos_validation.signals import algo_004_signals, control_ma_signals

RESULTS_FILE_NAME = "single_use_validation.json"

Evaluator = Callable[[Sequence[MarketPrice]], Mapping[str, Sequence[TradeLeg]]]


def default_evaluator(bars: Sequence[MarketPrice]) -> Mapping[str, Sequence[TradeLeg]]:
    """Run the frozen OUR-ALGO-004 engine and the frozen MA(5,21) control."""
    assert_frozen_params(dict(FROZEN_ALGO_PARAMS), ALGO_NAME, ALGO_FINGERPRINT)
    assert_frozen_params(dict(FROZEN_CONTROL_PARAMS), CONTROL_NAME, CONTROL_FINGERPRINT)
    algo_targets = algo_004_signals(bars, **dict(FROZEN_ALGO_PARAMS))
    control_targets = control_ma_signals(bars, **dict(FROZEN_CONTROL_PARAMS))
    return {
        "ours": form_trades(bars, algo_targets),
        "control": form_trades(bars, control_targets),
    }


def run_controlled_validation(
    root: Path,
    *,
    explicit_invoke: bool,
    datasets_dir: Path | None = None,
    now_fn: Callable[[], datetime] | None = None,
    evaluator: Evaluator | None = None,
    min_days: int = 20,
    min_bars: int = 1500,
) -> ValidationOutcome:
    """Run the controlled single-use validation, or return a refusal outcome."""
    root = Path(root)
    run_id = new_run_id()
    now = now_fn() if now_fn else datetime.now()

    try:
        already_consumed = is_consumed(root)
    except Exception as exc:  # noqa: BLE001 -- surfaced machine-readably, never repaired
        return ValidationOutcome(
            run_id=run_id,
            status=STATUS_FAILED,
            consumed=False,
            consumed_at=None,
            dates=(),
            bars=0,
            per_day_hashes={},
            algorithm=ALGO_NAME,
            algorithm_fingerprint=ALGO_FINGERPRINT,
            control=CONTROL_NAME,
            control_fingerprint=CONTROL_FINGERPRINT,
            cost_model=cost_model_dict(),
            evidence_limitations=EVIDENCE_LIMITATIONS,
            reasons=(f"consumption record check failed: {exc}",),
            message="validation blocked: single-use bookkeeping could not be verified",
        )

    if already_consumed:
        return ValidationOutcome(
            run_id=run_id,
            status=STATUS_CONSUMED_ALREADY,
            consumed=True,
            consumed_at=None,
            dates=(),
            bars=0,
            per_day_hashes={},
            algorithm=ALGO_NAME,
            algorithm_fingerprint=ALGO_FINGERPRINT,
            control=CONTROL_NAME,
            control_fingerprint=CONTROL_FINGERPRINT,
            cost_model=cost_model_dict(),
            evidence_limitations=EVIDENCE_LIMITATIONS,
            reasons=("single-use validation already consumed; the fresh pool is NOT re-runnable",),
            message="the fresh pool has already been consumed by a previous single-use run",
        )

    try:
        pool = load_pool(root, datasets_dir=datasets_dir)
    except Exception as exc:  # noqa: BLE001 -- surfaced machine-readably, never repaired
        return ValidationOutcome(
            run_id=run_id,
            status=STATUS_FAILED,
            consumed=False,
            consumed_at=None,
            dates=(),
            bars=0,
            per_day_hashes={},
            algorithm=ALGO_NAME,
            algorithm_fingerprint=ALGO_FINGERPRINT,
            control=CONTROL_NAME,
            control_fingerprint=CONTROL_FINGERPRINT,
            cost_model=cost_model_dict(),
            evidence_limitations=EVIDENCE_LIMITATIONS,
            reasons=(f"pool verification failed: {exc}",),
            message="validation blocked: the fresh pool did not verify",
        )

    gate = evaluate_gate(
        pool,
        explicit_invoke=explicit_invoke,
        min_days=min_days,
        min_bars=min_bars,
        consumed=already_consumed,
    )
    if not gate.can_run:
        status = STATUS_REFUSED
        if not explicit_invoke:
            status = STATUS_REFUSED
        elif pool.num_days < min_days or pool.num_bars_count < min_bars:
            status = STATUS_NOT_READY
        return ValidationOutcome(
            run_id=run_id,
            status=status,
            consumed=False,
            consumed_at=None,
            dates=tuple(rec.day.isoformat() for rec in pool.days),
            bars=pool.num_bars_count,
            per_day_hashes=pool.per_day_hashes(),
            algorithm=ALGO_NAME,
            algorithm_fingerprint=ALGO_FINGERPRINT,
            control=CONTROL_NAME,
            control_fingerprint=CONTROL_FINGERPRINT,
            cost_model=cost_model_dict(),
            evidence_limitations=EVIDENCE_LIMITATIONS,
            reasons=gate.reasons,
            message="validation blocked by the pre-validation gate",
        )

    runner = evaluator if evaluator is not None else default_evaluator
    try:
        result = runner(pool.bars)
        ours_legs = list(result["ours"])
        control_legs = list(result["control"])
    except Exception as exc:  # noqa: BLE001 -- surfaced machine-readably, never repaired
        return ValidationOutcome(
            run_id=run_id,
            status=STATUS_FAILED,
            consumed=False,
            consumed_at=None,
            dates=tuple(rec.day.isoformat() for rec in pool.days),
            bars=pool.num_bars_count,
            per_day_hashes=pool.per_day_hashes(),
            algorithm=ALGO_NAME,
            algorithm_fingerprint=ALGO_FINGERPRINT,
            control=CONTROL_NAME,
            control_fingerprint=CONTROL_FINGERPRINT,
            cost_model=cost_model_dict(),
            evidence_limitations=EVIDENCE_LIMITATIONS,
            reasons=(f"evaluation failed: {exc}",),
            message="validation blocked: the replay engines did not complete",
        )

    if len(ours_legs) < MIN_TRADES:
        return ValidationOutcome(
            run_id=run_id,
            status=STATUS_NOT_READY,
            consumed=False,
            consumed_at=None,
            dates=tuple(rec.day.isoformat() for rec in pool.days),
            bars=pool.num_bars_count,
            per_day_hashes=pool.per_day_hashes(),
            algorithm=ALGO_NAME,
            algorithm_fingerprint=ALGO_FINGERPRINT,
            control=CONTROL_NAME,
            control_fingerprint=CONTROL_FINGERPRINT,
            cost_model=cost_model_dict(),
            evidence_limitations=EVIDENCE_LIMITATIONS,
            reasons=(f"algorithm produced {len(ours_legs)} trades < required {MIN_TRADES}",),
            message="validation blocked: trade count below protocol minimum",
        )

    ours_metrics = _build_metrics(pool, ours_legs, ALGO_NAME, ALGO_FINGERPRINT)
    control_metrics = _build_metrics(pool, control_legs, CONTROL_NAME, CONTROL_FINGERPRINT)
    comparison = compare(ours_metrics, control_metrics)
    outcome = ValidationOutcome(
        run_id=run_id,
        status=STATUS_SUCCESS,
        consumed=True,
        consumed_at=now.isoformat(timespec="seconds"),
        dates=tuple(rec.day.isoformat() for rec in pool.days),
        bars=pool.num_bars_count,
        per_day_hashes=pool.per_day_hashes(),
        algorithm=ALGO_NAME,
        algorithm_fingerprint=ALGO_FINGERPRINT,
        control=CONTROL_NAME,
        control_fingerprint=CONTROL_FINGERPRINT,
        cost_model=cost_model_dict(),
        algorithm_metrics=ours_metrics,
        control_metrics=control_metrics,
        comparison=comparison,
        evidence_limitations=EVIDENCE_LIMITATIONS,
        reasons=(),
        message=(
            f"single-use fresh-OOS validation consumed {len(pool.days)} days / "
            f"{pool.num_bars_count} bars; {len(ours_legs)} trades"
        ),
    )

    write_consumption(root, run_id=run_id, outcome=outcome.to_dict(), now_fn=now_fn)
    _write_results(root, outcome)
    return outcome


def _build_metrics(pool: PoolReport, legs: Sequence[TradeLeg], name: str, fingerprint: str) -> MetricsReport:
    raw = compute_metrics(legs, label=name)
    regimes = _entry_day_regimes(pool, legs)
    return MetricsReport(
        algorithm=name,
        params_fingerprint=fingerprint,
        trades=raw.trades,
        win_rate=raw.win_rate,
        net_pnl=raw.net_pnl,
        gross_pnl=raw.gross_pnl,
        costs=raw.costs,
        expectancy=raw.expectancy,
        profit_factor=raw.profit_factor,
        max_drawdown=raw.max_drawdown,
        tail_loss=raw.tail_loss,
        half1_net=raw.half1_net,
        half2_net=raw.half2_net,
        regime_breakdown=dict(regimes),
        insufficient_sample=raw.trades < MIN_TRADES,
    )


def _entry_day_regimes(pool: PoolReport, legs: Sequence[TradeLeg]) -> dict[str, int]:
    day_bars: dict[str, list[MarketPrice]] = {}
    for bar in pool.bars:
        day_bars.setdefault(bar.timestamp.date().isoformat(), []).append(bar)
    labels = [regime_label(day_bars.get(leg.entry_date.isoformat(), [])) for leg in legs]
    return dict(Counter(labels))


def _write_results(root: Path, outcome: ValidationOutcome) -> None:
    root = Path(root)
    path = root / RESULTS_FILE_NAME
    fd, scratch = tempfile.mkstemp(prefix=f".{RESULTS_FILE_NAME}.", dir=str(root), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(outcome.to_dict(), sort_keys=True, indent=2, default=str) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(scratch, path)
    except BaseException:
        try:
            os.unlink(scratch)
        except OSError:
            pass
        raise