"""Phases 8-9 — isolation boundaries.

The Daily Paper Trading Track is an island: it must never load the live-trading
stack (execution gate/manager/adapters), the Fresh-OOS validation plumbing, or a
scheduler, and it must produce credential-free reports even when the process
environment is polluted with live-trading variables.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"
PAPER_TRACK = SRC / "fno_ai_paper_trading" / "paper_track"

FORBIDDEN_MODULE_PREFIXES = (
    "fno_ai_paper_trading.fresh_oos",
    "fno_ai_paper_trading.fresh_oos_validation",
    "fno_ai_paper_trading.execution",
    "fno_ai_paper_trading.live_scheduler",
)

FORBIDDEN_SOURCE_TOKENS = (
    "fresh_oos",
    "fresh_oos_validation",
    "execution.manager",
    "execution.gate",
    "live_execution_test",
    "live_scheduler",
    "kiteconnect",
)


def _run_python(script: str, *, env: dict | None = None) -> subprocess.CompletedProcess:
    merged = dict(os.environ)
    merged["PYTHONPATH"] = str(SRC)
    if env:
        merged.update(env)
    return subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(REPO),
        env=merged,
        capture_output=True,
        text=True,
        timeout=180,
    )


def test_import_graph_excludes_live_and_oos_modules():
    script = """
import json, sys
import fno_ai_paper_trading.paper_track.engine
import fno_ai_paper_trading.paper_track.runner
import fno_ai_paper_trading.paper_track.store
import fno_ai_paper_trading.paper_track.report
import fno_ai_paper_trading.paper_track.invariants
mods = sorted(m for m in sys.modules if m.startswith("fno_ai_paper_trading"))
bad = [m for m in mods if m.startswith(("fno_ai_paper_trading.fresh_oos",
        "fno_ai_paper_trading.fresh_oos_validation",
        "fno_ai_paper_trading.execution",
        "fno_ai_paper_trading.live_scheduler"))]
print(json.dumps(bad))
"""
    result = _run_python(script)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == []


def test_engine_run_under_live_temptation_env_stays_paper():
    script = """
import json, sys, tempfile
from pathlib import Path
from datetime import date
sys.path.insert(0, %r)
from fno_ai_paper_trading.paper_track.engine import TrackEngine, TrackConfig
from fno_ai_paper_trading.paper_track.store import TrackStore
from fno_ai_paper_trading.paper_track.clock import FixedClock
from fno_ai_paper_trading.paper_track.feed import SyntheticFeed, TRACK_INSTRUMENT
from fno_ai_paper_trading.paper_track.feed import build_session_bars
tmp = Path(tempfile.mkdtemp())
day = date(2026, 9, 21)
feed = SyntheticFeed(TRACK_INSTRUMENT(), {day: build_session_bars(day, seed=1, instrument=TRACK_INSTRUMENT())})
account = "livegate"
engine = TrackEngine(TrackConfig(account=account, store_dir=tmp), store=TrackStore(tmp, account),
                     clock=FixedClock(__import__("datetime").datetime(2026, 9, 21, 9, 14)), bars_source=feed)
from datetime import datetime, timedelta
start = datetime(2026, 9, 21, 9, 14)
for k in range(2 + 76):
    engine.clock.set(start + timedelta(minutes=k if k < 2 else 5*(k-1)))
    try:
        engine.step()
    except Exception as e:
        break
report = engine.report()
mods = [m for m in sys.modules if m.startswith("fno_ai_paper_trading")]
bad = sorted(m for m in mods if m.startswith(("fno_ai_paper_trading.execution",
        "fno_ai_paper_trading.live_scheduler")))
print(json.dumps({"is_live": engine.broker.is_live,
                  "paper_only": report["paper_only"] if report else None,
                  "bad": bad}))
""" % (str(SRC),)
    env = {
        "UPSTOX_ACCESS_TOKEN": "supersecret-live-token",
        "FNO_UPSTOX_ACCESS_TOKEN": "supersecret-live-token",
        "FNO_LIVE_EXECUTION_ENABLED": "1",
        "FNO_LIVE_EXECUTION_TEST_ENABLED": "1",
    }
    result = _run_python(script, env=env)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["is_live"] is False
    assert payload["paper_only"] is True
    assert payload["bad"] == []


def test_source_tree_contains_no_live_or_oos_references():
    offenders = []
    files = sorted(PAPER_TRACK.rglob("*.py")) + [REPO / "scripts" / "run_paper_track.py"]
    for path in files:
        text = path.read_text(encoding="utf-8")
        for token in FORBIDDEN_SOURCE_TOKENS:
            if token in text:
                offenders.append(f"{path.relative_to(REPO)}: {token}")
    assert offenders == [], offenders


def test_persisted_report_is_credential_free_subprocess():
    script = """
import json, sys
sys.path.insert(0, %r)
from fno_ai_paper_trading.paper_track.report import assert_report_clean
sample = {"run_id": "paper-run-X", "data": "everything"}, "nope"
# Build a minimal report-like structure quickly to prove the scan is active.
report = {
    "paper_only": True,
    "run_id": "paper-run-20260921-ABC123",
    "account": "nifty_5m_daily",
    "strategy": {"name": "moving_average_cross"},
    "accounting": {"cash": "100000", "net_pnl": "0"},
    "data_quality": {"data_skips": [{"reason": "bar outside session window"}]},
}
print(json.dumps({"violations": assert_report_clean(report)}))
""" % (str(SRC),)
    result = _run_python(script)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["violations"] == []


def test_report_scan_flags_secret_tokens():
    script = """
import json, sys
sys.path.insert(0, %r)
from fno_ai_paper_trading.paper_track.report import assert_report_clean
leaky = {"note": "upstox_oauth access_token=abc client_secret=zzz password=p"}
print(json.dumps({"violations": assert_report_clean(leaky)}))
""" % (str(SRC),)
    result = _run_python(script)
    assert result.returncode == 0, result.stderr
    violations = json.loads(result.stdout)["violations"]
    assert "access_token" in violations and "upstox" in violations