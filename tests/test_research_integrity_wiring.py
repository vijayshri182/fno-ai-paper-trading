"""G7 — canonical research-integrity wiring.

Tests that the options_research integrity helpers (windows.py) are actually
enforced at the production ingestion seams:

* ``validate_split`` + canonical ``PROTECTED_OOS_START`` binding in the
  discovery-cycle research driver;
* ``collect_protected_days`` refusal in the real-data research ingestion;
* ``check_no_lookahead`` in the walk-forward day decision seam.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from dataclasses import replace

import pytest

import scripts.discovery_cycle as discovery_cycle
import scripts.research_real_data as research_cli
from fno_ai_paper_trading.data.dataset_store import save_dataset
from fno_ai_paper_trading.data.instrument_registry import get_research_instrument
from fno_ai_paper_trading.evaluation.five_year import DayBars
from fno_ai_paper_trading.models.enums import Signal
from fno_ai_paper_trading.models.market import MarketPrice
from fno_ai_paper_trading.options_research.windows import PROTECTED_OOS_START
from fno_ai_paper_trading.research.regimes import build_trend_reversal
from fno_ai_paper_trading.strategies.base import SignalResult, Strategy
from fno_ai_paper_trading.walkforward.engine import WalkForwardEngine


# ---------------------------------------------------------------------------
# discovery_cycle: canonical split + protected-boundary binding
# ---------------------------------------------------------------------------
def test_discovery_refuses_non_canonical_protected_boundary(tmp_path):
    with pytest.raises(SystemExit) as exc:
        discovery_cycle.main(
            [
                "--outdir", str(tmp_path / "ds"),
                "--reports-dir", str(tmp_path / "rep"),
                "--protected-oos-start", "2025-10-07",
            ]
        )
    assert exc.value.code == 2


def test_discovery_refuses_inverted_split(tmp_path):
    with pytest.raises(SystemExit) as exc:
        discovery_cycle.main(
            [
                "--outdir", str(tmp_path / "ds"),
                "--reports-dir", str(tmp_path / "rep"),
                "--train-start", "2025-07-01",
                "--train-end", "2025-06-30",
            ]
        )
    assert exc.value.code == 2


def test_discovery_refuses_overlapping_split(tmp_path):
    with pytest.raises(SystemExit) as exc:
        discovery_cycle.main(
            [
                "--outdir", str(tmp_path / "ds"),
                "--reports-dir", str(tmp_path / "rep"),
                "--train-end", "2025-06-30",
                "--val-start", "2025-06-30",
            ]
        )
    assert exc.value.code == 2


def test_discovery_valid_plan_passes_guards_and_reports_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("FNO_UPSTOX_ACCESS_TOKEN", "")
    args = [
        "--outdir", str(tmp_path / "ds"),
        "--reports-dir", str(tmp_path / "rep"),
    ]
    code = discovery_cycle.main(args)
    # No credential, no cached dataset -> minimal report-only run (exit 3),
    # which proves the canonical guards let the default plan through.
    assert code == 3
    assert (tmp_path / "rep").is_dir()


# ---------------------------------------------------------------------------
# research_real_data: protected-window ingestion refusal
# ---------------------------------------------------------------------------
def _rebase(days: list[date], bars: list[MarketPrice]) -> list[MarketPrice]:
    assert len(days) == len(bars)
    return [
        replace(b, timestamp=datetime.combine(days[i], b.timestamp.time()))
        for i, b in enumerate(bars)
    ]


def _business_days(start: date, n: int) -> list[date]:
    out: list[date] = []
    day = start
    while len(out) < n:
        if day.weekday() < 5:
            out.append(day)
        day += timedelta(days=1)
    return out


def _dataset(tmp_path, days: list[date]):
    instrument = get_research_instrument("NIFTY 50")
    base = build_trend_reversal(instrument, bars=len(days))
    rebased = _rebase(days, base)
    return save_dataset(
        rebased,
        instrument=instrument,
        provider="guard_test",
        interval="1d",
        directory=tmp_path,
        name="guard_dataset",
    )


def test_real_data_refuses_protected_days(tmp_path, monkeypatch, capsys):
    protected_days = _business_days(date(2025, 11, 3), 40)
    assert any(PROTECTED_OOS_START <= d <= date(2026, 9, 11) for d in protected_days)
    dataset = _dataset(tmp_path, protected_days)
    monkeypatch.setenv("FNO_UPSTOX_ACCESS_TOKEN", "fake-token")
    monkeypatch.setattr(
        research_cli,
        "_fetch_real_dataset",
        lambda token, start, end, directory, name: dataset,
    )
    code = research_cli.main(
        [
            "--outdir", str(tmp_path / "ds"),
            "--report-dir", str(tmp_path / "rep"),
            "--start", "2025-11-01",
            "--end", "2025-12-31",
        ]
    )
    assert code == 2
    assert "protected-OOS" in capsys.readouterr().err


def test_real_data_accepts_clean_window(tmp_path, monkeypatch):
    clean_days = _business_days(date(2025, 2, 3), 80)
    assert max(clean_days) < PROTECTED_OOS_START
    dataset = _dataset(tmp_path, clean_days)
    monkeypatch.setenv("FNO_UPSTOX_ACCESS_TOKEN", "fake-token")
    monkeypatch.setattr(
        research_cli,
        "_fetch_real_dataset",
        lambda token, start, end, directory, name: dataset,
    )
    code = research_cli.main(
        [
            "--outdir", str(tmp_path / "ds"),
            "--report-dir", str(tmp_path / "rep"),
            "--start", "2025-02-03",
            "--end", "2025-05-30",
        ]
    )
    assert code == 0
    assert (tmp_path / "rep" / "real_data_research_report.html").exists()


# ---------------------------------------------------------------------------
# walk-forward engine: canonical no-lookahead at the decision seam
# ---------------------------------------------------------------------------
class _FutureStampingStrategy(Strategy):
    """A strategy that stamps signals one minute after the latest input bar."""

    name = "future_stamper"

    def analyze(self, bars: list[MarketPrice]) -> SignalResult:
        last = bars[-1]
        return SignalResult(
            signal=Signal.HOLD,
            instrument=last.instrument,
            timestamp=last.timestamp + timedelta(minutes=1),
            reason="future-stamped mock",
        )


def test_walkforward_raises_on_future_stamped_signal(monkeypatch):
    import fno_ai_paper_trading.walkforward.engine as wf_engine

    monkeypatch.setattr(
        wf_engine, "_build_version_strategy", lambda *a, **k: _FutureStampingStrategy()
    )
    engine = WalkForwardEngine()
    days = []
    start = date(2025, 6, 2)
    for i in range(3):
        ts = datetime.combine(start + timedelta(days=i), time(9, 15))
        bars = [
            MarketPrice(
                instrument=get_research_instrument("NIFTY 50"),
                timestamp=ts + timedelta(minutes=5 * k),
                open=100,
                high=101,
                low=99,
                close=100,
                volume=1000,
            )
            for k in range(10)
        ]
        days.append(DayBars(day=ts.date(), bars=tuple(bars), source_hash="g7-test"))
    with pytest.raises(ValueError, match="no-lookahead violation"):
        engine.run(days)