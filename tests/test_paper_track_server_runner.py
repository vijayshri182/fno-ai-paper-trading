"""Offline Phase-5 daily server-runner tests.

The Phase-5 ``server_runner`` is a *thin composition* of the already-proven
Phase-1..4 seams (``daily_session``) -- it never re-defines the NIFTY 50
instrument, the MA(5,21) strategy, the FNO risk manager, the risk-based
position sizer, the 2% stop-loss, the long-only policy, or the PaperBroker,
and it stays PaperBroker-only / offline / fail-closed.

Covered Phase-5 contract:

1.  ``live-daily`` command exists in ``scripts/run_paper_track.py``;
2.  the runner delegates to the exact Phase-4 interface
    (``run_warmup_sessions`` + ``daily_report``), never re-implementing
    strategy/risk/candle/broker logic;
3.  the warm-up token comes ONLY from the Phase-2 provider seam
    (``daily_session.get_access_token``, env set by ``token_provider``);
4.  live market data is delivered through the Phase-3 read-only Upstox V3
    client seam (``paper_track.upstox_feed``) at the injectable ``feed``
    parameter -- no socket / transport is re-implemented here;
5.  PaperBroker remains the execution backend (``is_live is False``);
6.  a completed trading day is rejected (once-per-day protection);
7-10. no live-execution dependency, no live gate, no ``UPSTOX_ACCESS_TOKEN``,
    no scheduler in the runner source;
11. no credential persistence / no token in any persisted artifact;
12. existing CLI commands remain intact.

Everything runs offline on a deterministic synthetic warm-up feed.
"""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys
from datetime import date, datetime

sys.path.insert(0, str(pathlib.Path("src").resolve()))
sys.path.insert(0, str(pathlib.Path("../src").resolve()))

import pytest

from fno_ai_paper_trading.broker.paper_broker import PaperBroker
from fno_ai_paper_trading.paper_track import daily_session as ds
from fno_ai_paper_trading.paper_track import server_runner as sr
from fno_ai_paper_trading.paper_track.engine import TrackEngine
from fno_ai_paper_trading.paper_track.errors import PaperTrackError
from fno_ai_paper_trading.paper_track.report import assert_report_clean
from fno_ai_paper_trading.paper_track.store import TrackStore

REPO = pathlib.Path(__file__).resolve().parents[1]
DAY0 = date(2026, 9, 21)
FAKE_TOKEN = "PAPER_TRACK_TEST_FAKE_TOKEN_phase5"


def _module_source() -> str:
    return pathlib.Path(sr.__file__).read_text(encoding="utf-8")


def _live_config(tmp_path, account="warm-up"):
    return sr.LiveDailyConfig(account=account, store_dir=pathlib.Path(tmp_path))


def _cmd_argv(tmp_path, account="warm-up", day="2026-09-21"):
    return ["--store-dir", str(tmp_path), "--account", account, "--day", day]


def _run_python(script: str) -> subprocess.CompletedProcess:
    import os

    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO / "src")
    return subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(REPO),
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )


# -- 1. command exists --------------------------------------------------------


def test_live_daily_command_exists():
    script = (REPO / "scripts" / "run_paper_track.py").read_text(encoding="utf-8")
    assert "live-daily" in script
    assert "cmd_live_daily" in script
    assert callable(sr.cmd_live_daily)


# -- 2. exact Phase-4 interface (composition-only) ---------------------------


def test_server_runner_delegates_never_redefines():
    src = _module_source()
    for seam in ("run_warmup_sessions", "daily_report", "build_warmup_config", "SyntheticFeed"):
        assert seam in src, seam
    # no re-implementation of the Phase-3 WebSocket transport
    for token in ("import socket", "import ssl", "asyncio", "websockets"):
        assert token not in src, token


def test_run_live_daily_returns_phase4_engine_offline(tmp_path):
    config = _live_config(tmp_path, account="phase5-if")
    engine = sr.run_live_daily(config=config, day=DAY0)
    assert isinstance(engine, TrackEngine)
    assert engine.broker.is_live is False
    assert engine.position_quantity == 0
    assert engine.report()["paper_only"] is True
    # the Phase-4 daily-report seam closes without violations
    report = ds.daily_report(engine, as_of=datetime.combine(DAY0, ds.CLOSE_TIME))
    assert report.position_flat is True
    assert report.violations == ()
    assert report.day == DAY0
    assert report.interval == ds.WARMUP_INTERVAL


# -- 3. token from the existing Phase-2 provider -----------------------------


def test_token_fail_closed_raises_when_env_absent(monkeypatch):
    monkeypatch.delenv(ds.TOKEN_ENV_NAME, raising=False)
    monkeypatch.delenv("UPSTOX_ACCESS_TOKEN", raising=False)
    from fno_ai_paper_trading.paper_track.token_provider import TokenProviderError

    with pytest.raises(TokenProviderError):
        sr.token_fail_closed()


def test_token_resolves_exactly_like_the_phase2_provider(monkeypatch):
    from fno_ai_paper_trading.paper_track.token_provider import get_access_token

    monkeypatch.setenv(ds.TOKEN_ENV_NAME, FAKE_TOKEN)
    assert sr.token_fail_closed() == FAKE_TOKEN
    assert sr.token_fail_closed() == get_access_token()


# -- 4. Upstox feed comes from the existing Phase-3 interface ----------------


def test_live_feed_seam_is_the_phase3_interface():
    src = _module_source()
    # the Phase-3 client module is the documented live-data source
    assert "upstox_feed" in src
    # the feed plugs in at the exact Phase-4 seam
    assert "run_warmup_sessions" in src
    import inspect

    assert "feed" in inspect.signature(sr.run_live_daily).parameters
    # no transport / socket / raw networking code is re-implemented here
    for token in ("import socket", "import ssl", "asyncio", "websockets", "Popen"):
        assert token not in src, token


def test_injected_feed_drives_the_session(tmp_path):
    from fno_ai_paper_trading.paper_track.feed import SyntheticFeed, TRACK_INSTRUMENT

    config = _live_config(tmp_path, account="phase5-feed")
    store = TrackStore(pathlib.Path(tmp_path), config.account)
    feed = SyntheticFeed.build([DAY0], seed=20260921, instrument=TRACK_INSTRUMENT())
    engine = sr.run_live_daily(config=config, store=store, feed=feed, day=DAY0)
    # every completed 5m bar of the day was consumed exactly once
    assert len(engine.consumed) == 75
    assert len(engine.history) == 75
    assert engine.position_quantity == 0


# -- 5. PaperBroker stays the execution backend ------------------------------


def test_paperbroker_remains_the_execution_backend(tmp_path, monkeypatch):
    monkeypatch.delenv(ds.TOKEN_ENV_NAME, raising=False)
    engine = sr.run_live_daily(
        config=_live_config(tmp_path, account="phase5-broker"), day=DAY0
    )
    assert isinstance(engine.broker, PaperBroker)
    assert engine.broker.is_live is False
    assert engine.report()["paper_only"] is True


# -- 6. duplicate completed day is rejected -----------------------------------


def test_duplicate_completed_day_is_rejected(tmp_path, capsys):
    argv = _cmd_argv(tmp_path, account="phase5-dup")
    assert sr.cmd_live_daily(argv) == 0
    store = TrackStore(pathlib.Path(tmp_path), "phase5-dup")
    assert store.load_report(DAY0) is not None

    rc = sr.cmd_live_daily(argv)
    captured = capsys.readouterr().out
    assert rc == 4
    assert "already completed" in captured

    # the lower-level seam refuses the repeat too (no silent double-trade)
    with pytest.raises(PaperTrackError):
        sr.run_live_daily(
            config=_live_config(tmp_path, account="phase5-dup"),
            store=store,
            day=DAY0,
        )


# -- 7-10. fail-closed source / no live exec / no live gate / no token var --


def test_server_runner_source_is_fail_closed():
    src = _module_source()
    forbidden = (
        "UPSTOX_ACCESS_TOKEN",
        "execution",
        "fresh_oos",
        "scheduler",
        "live_gate",
        "getpass",
        "Popen",
        "subprocess",
    )
    assert not any(tok in src for tok in forbidden), "live/exec/token tokens leaked into server runner"


def test_no_live_execution_dependency_subprocess():
    script = (
        "import sys, json; "
        "import fno_ai_paper_trading.paper_track.server_runner; "
        "bad=[m for m in sys.modules if m.startswith(('fno_ai_paper_trading.execution',"
        " 'fno_ai_paper_trading.fresh_oos', 'fno_ai_paper_trading.live_scheduler'))]; "
        "print(json.dumps(sorted(bad)))"
    )
    result = _run_python(script)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == []


# -- 11. no credential persistence -------------------------------------------


def test_no_credential_persistence(tmp_path, monkeypatch):
    monkeypatch.setenv(ds.TOKEN_ENV_NAME, FAKE_TOKEN)
    store_dir = pathlib.Path(tmp_path) / "cred_store"
    assert sr.cmd_live_daily(_cmd_argv(store_dir, account="phase5-cred")) == 0

    leaked: list[str] = []
    for path in store_dir.rglob("*"):
        if path.is_file():
            text = path.read_text(encoding="utf-8", errors="replace")
            if FAKE_TOKEN in text or "UPSTOX_ACCESS_TOKEN" in text:
                leaked.append(str(path.relative_to(store_dir)))
    assert leaked == []

    store = TrackStore(store_dir, "phase5-cred")
    report = store.load_report(DAY0)
    assert report is not None
    assert assert_report_clean(report) == []


# -- 12. existing CLI commands remain intact ----------------------------------


def test_existing_cli_commands_intact():
    script_text = (REPO / "scripts" / "run_paper_track.py").read_text(encoding="utf-8")
    runner_text = (REPO / "src" / "fno_ai_paper_trading" / "paper_track" / "runner.py").read_text(
        encoding="utf-8"
    )
    # the original commands live in runner.main's dispatch; live-daily is the
    # Phase-5 seam kept forward of it in the entry-point script.
    for command in ("smoke", "simulate", "list", "checkpt", "upstox"):
        assert command in runner_text, command
    assert "live-daily" in script_text
    assert "cmd_live_daily" in script_text

    result = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "run_paper_track.py"), "--help"],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    for command in ("smoke", "simulate", "list", "checkpt", "upstox"):
        assert command in result.stdout, command

    # ``live-daily --help`` renders the Phase-5 sub-command usage
    usage = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "run_paper_track.py"), "live-daily", "--help"],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert usage.returncode == 0, usage.stderr
    assert "live-daily" in usage.stdout


# -- registry-backed instrument resolution -----------------------------------


def test_research_instrument_is_the_registry_nifty_50():
    from fno_ai_paper_trading.data.instrument_registry import get_research_instrument

    assert sr.research_instrument() is get_research_instrument("NIFTY 50")
    assert sr.research_instrument().exchange_token == "NSE_INDEX|Nifty 50"