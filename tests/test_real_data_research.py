"""Tests for the real-data research pipeline.

These tests run the pipeline against small, deterministic synthetic datasets so
no network access or broker credentials are required. They verify that the
pipeline is reproducible, validates datasets, separates in-sample from
out-of-sample data, and refuses to run without an Upstox token when asked for
real data.
"""
from __future__ import annotations

import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from fno_ai_paper_trading.data.dataset_store import save_dataset
from fno_ai_paper_trading.data.instrument_registry import get_research_instrument
from fno_ai_paper_trading.data.validation import validate_bars
from fno_ai_paper_trading.research.real_data import (
    DatasetStatistics,
    RealDataResearchResult,
    compute_dataset_stats,
    run_real_data_research,
)
from fno_ai_paper_trading.research.regimes import build_trend_reversal


def _smoke_dataset(tmp_path: Path):
    """Persist a small deterministic synthetic dataset into ``tmp_path``."""
    instrument = get_research_instrument("NIFTY 50")
    bars = build_trend_reversal(instrument, bars=240)
    return save_dataset(
        bars,
        instrument=instrument,
        provider="in_memory_smoke",
        interval="1d",
        directory=tmp_path,
        name="nifty50_smoke",
    )


class TestDatasetStatistics:
    def test_compute_dataset_stats(self, tmp_path: Path) -> None:
        dataset = _smoke_dataset(tmp_path)
        report = validate_bars(dataset.bars, allow_empty=False)
        stats = compute_dataset_stats(dataset.bars, report)

        assert isinstance(stats, DatasetStatistics)
        assert stats.observations == 240
        assert stats.duplicate_timestamps == 0
        assert stats.invalid_ohlc_bars == 0
        assert stats.close_min > Decimal("0")
        assert stats.close_max >= stats.close_min
        assert stats.avg_volume > Decimal("0")
        assert stats.avg_daily_return_pct is not None
        assert stats.daily_return_volatility_pct is not None
        assert stats.largest_daily_return_abs_pct is not None
        assert stats.largest_daily_return_abs_pct >= Decimal("0")

    def test_stats_detect_duplicates(self, tmp_path: Path) -> None:
        dataset = _smoke_dataset(tmp_path)
        bars = list(dataset.bars)
        # Replace the last bar's timestamp with the first to create a duplicate.
        bars[-1] = bars[0]
        report = validate_bars(bars, allow_empty=False)
        stats = compute_dataset_stats(bars, report)
        assert stats.duplicate_timestamps == 1


class TestRealDataResearchPipeline:
    def test_end_to_end_smoke(self, tmp_path: Path) -> None:
        dataset = _smoke_dataset(tmp_path)
        result = run_real_data_research(
            dataset,
            initial_capital=Decimal("250000"),
            quantity=5,
        )

        assert isinstance(result, RealDataResearchResult)
        assert result.dataset_hash == dataset.data_hash
        assert result.validation["ok"] is True
        assert result.strategy_params == {"fast": 5, "slow": 21}

        # Full, IS and OOS experiments exist and are reproducible through hashes.
        assert result.full_experiment.config.config_hash
        assert result.is_experiment.config.config_hash
        assert result.oos_experiment.config.config_hash

        # Benchmark is populated for the full period.
        assert result.full_experiment.benchmark is not None

        # Sensitivity grid evaluated all valid pairs.
        valid_pairs = {(r.fast, r.slow) for r in result.sensitivity.rows}
        assert valid_pairs == {(5, 21), (5, 50), (10, 21), (10, 50), (15, 21), (15, 50)}

        # Walk-forward produced at least one OOS window.
        assert result.walk_forward.steps
        assert len(result.walk_forward.per_step) == len(result.walk_forward.steps)

        # Regime segments: synthetic 2026 data falls outside the named date
        # windows, so the regime list should be empty for smoke data.
        assert result.regimes == []

    def test_repeatability(self, tmp_path: Path) -> None:
        dataset = _smoke_dataset(tmp_path)
        a = run_real_data_research(dataset)
        b = run_real_data_research(dataset)
        assert a.dataset_hash == b.dataset_hash
        assert a.full_experiment.config.config_hash == b.full_experiment.config.config_hash
        assert a.full_experiment.metrics.net_pnl == b.full_experiment.metrics.net_pnl
        assert a.oos_experiment.metrics.net_return_pct == b.oos_experiment.metrics.net_return_pct

    def test_oos_separation(self, tmp_path: Path) -> None:
        dataset = _smoke_dataset(tmp_path)
        result = run_real_data_research(dataset)
        is_end = result.is_experiment.config.end_date
        oos_start = result.oos_experiment.config.start_date
        assert is_end is not None
        assert oos_start is not None
        assert is_end < oos_start


class TestResearchRealDataCli:
    def test_cli_blocks_without_token(self, tmp_path: Path) -> None:
        env = dict(os.environ)
        env.pop("UPSTOX_ACCESS_TOKEN", None)
        cmd = [sys.executable, "scripts/research_real_data.py", "--outdir", str(tmp_path / "ds")]
        proc = subprocess.run(cmd, capture_output=True, text=True, env=env, cwd=str(Path(__file__).resolve().parents[1]))
        assert proc.returncode == 2
        assert "UPSTOX_ACCESS_TOKEN" in proc.stderr

    def test_cli_smoke_runs_and_writes_report(self, tmp_path: Path) -> None:
        outdir = tmp_path / "datasets"
        report_dir = tmp_path / "reports"
        cmd = [
            sys.executable,
            "scripts/research_real_data.py",
            "--smoke",
            "--outdir",
            str(outdir),
            "--report-dir",
            str(report_dir),
        ]
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=str(Path(__file__).resolve().parents[1]),
        )
        assert proc.returncode == 0, proc.stderr
        report = report_dir / "real_data_smoke_report.html"
        assert report.exists()
        assert "SMOKE RUN" in proc.stdout or "Offline Smoke Test" in report.read_text(encoding="utf-8")
