"""CLI smoke tests for ``status`` and ``collect`` (subprocess-free).

Run the module entry points directly; verify read-only status output, JSON
summary shape, and exit codes (0 clean/no-op, 1 failure, 2 usage).
"""
from __future__ import annotations

import json
from datetime import date, datetime

from fresh_oos_testkit import BOUNDARY, FakeHistoricalDataClient, make_context
from fno_ai_paper_trading.fresh_oos import collect as collect_module

NOW = datetime(2026, 10, 15, 12, 0)
D1 = date(2026, 9, 17)


def _run_collect(ctx, *argv, fake=False):
    """Invoke the collect CLI against the temp store (optionally a fake client)."""
    original = collect_module.factory.build_collector

    def _wired(root=None, datasets_dir=None, max_days=None, client=None):
        fresh = type(ctx["collector"]).__new__(type(ctx["collector"]))
        fresh.__dict__.update(ctx["collector"].__dict__)
        return fresh

    if fake:
        collect_module.factory.build_collector = _wired
    try:
        return collect_module.main(list(argv))
    finally:
        collect_module.factory.build_collector = original


def test_collect_cli_service_args_and_json_summary(tmp_path, capsys):
    ctx = make_context(tmp_path, now=NOW)
    code = _run_collect(ctx, "--once", "--date", D1.isoformat(), "--json", fake=True)
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "SUCCESS"
    assert payload["accepted"] == [D1.isoformat()]
    assert ctx["store"].find(D1) is not None


def test_collect_cli_boundary_date_exits_failure(tmp_path, capsys):
    ctx = make_context(tmp_path, now=NOW)
    code = _run_collect(ctx, "--date", BOUNDARY.isoformat(), "--json", fake=True)
    assert code == 1  # PROTOCOL_VIOLATION
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "PROTOCOL_VIOLATION"
    assert ctx["store"].find(BOUNDARY) is None


def test_collect_cli_invalid_date_exits_usage(tmp_path):
    ctx = make_context(tmp_path, now=NOW)
    assert _run_collect(ctx, "--date", "not-a-date") == 2


def test_collect_cli_repeated_run_is_idempotent(tmp_path, capsys):
    ctx = make_context(tmp_path, now=NOW)
    assert _run_collect(ctx, "--date", D1.isoformat(), "--json", fake=True) == 0
    capsys.readouterr()
    assert _run_collect(ctx, "--date", D1.isoformat(), "--json", fake=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "NO_NEW_DATA"


def test_status_cli_prints_readiness_block(tmp_path, monkeypatch, capsys):
    from fno_ai_paper_trading.fresh_oos import factory
    from fno_ai_paper_trading.fresh_oos import status as status_module

    ctx = make_context(tmp_path, now=NOW)
    ctx["collector"].collect_once(force_date=D1)
    collector2 = factory.build_collector(root=str(ctx["root"]), datasets_dir=str(ctx["datasets"]))
    assert len(collector2.manifest.accepted_dates) == 1  # restart-safe: pool is loaded
    original = status_module.factory.build_collector
    status_module.factory.build_collector = lambda **kw: collector2
    try:
        code = status_module.main([])
    finally:
        status_module.factory.build_collector = original
    assert code == 0
    out = capsys.readouterr().out
    assert "Boundary (exclusive)" in out
    assert "NOT_READY" in out or "DATA_READY" in out
    assert "Validation" in out
    assert "NOT RUN" in out