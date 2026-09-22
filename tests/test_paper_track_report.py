"""Phase 12 — daily report: fields, redaction cleanliness, determinism.

The report is what any observer reads after a finalized day. It must be
complete, free of credentials, and reproducible: the same day re-run produces
the same economic fingerprint; tampered content changes it.
"""

from __future__ import annotations

import json

from fno_ai_paper_trading.paper_track.accounting import total_commission
from fno_ai_paper_trading.paper_track.report import assert_report_clean, build_report, report_fingerprint
from tests.paper_track_testkit import DAY0, day_ticks, drive, feed_for, make_engine

REQUIRED_TOP_LEVEL = {
    "paper_only",
    "trading_date",
    "account",
    "run_id",
    "strategy",
    "policy",
    "lifecycle",
    "signals",
    "orders",
    "fills",
    "accounting",
    "risk",
    "data_quality",
    "equity",
    "eod_status",
    "fingerprint",
    "report_created_at",
}


def _run(tmp_path, account="report"):
    engine, _, _ = make_engine(tmp_path, days=[DAY0], account=account)
    drive(engine, day_ticks(DAY0))
    return engine


def test_report_is_paper_only(tmp_path):
    report = _run(tmp_path).report()
    assert report is not None
    assert report["paper_only"] is True
    assert set(REQUIRED_TOP_LEVEL).issubset(report.keys())


def test_report_accounting_matches_engine(tmp_path):
    engine = _run(tmp_path)
    report = engine.report()
    assert report["accounting"]["cash"] == str(engine.portfolio.cash)
    assert report["accounting"]["net_pnl"] == str(
        engine.portfolio.realized_pnl - total_commission(engine.broker.fills)
    )


def test_report_clean_of_credentials(tmp_path):
    report = _run(tmp_path).report()
    assert assert_report_clean(report) == []
    blob = json.dumps(report, default=str).lower()
    for token in ("access_token", "client_secret", "password"):
        assert token not in blob


def test_report_carries_lifecycle_round_trip(tmp_path):
    report = _run(tmp_path).report()
    lifecycle = report["lifecycle"]
    assert lifecycle["entry"] is not None and lifecycle["exit"] is not None
    assert lifecycle["entry"]["quantity"] == lifecycle["exit"]["quantity"] > 0
    assert report["strategy"]["name"] == "moving_average_cross"
    assert report["accounting"]["final_position_quantity"] == 0


def test_fingerprint_stable_across_reruns(tmp_path):
    report_a = _run(tmp_path, account="fp-a").report()
    report_b = _run(tmp_path, account="fp-b").report()
    assert report_a["fingerprint"] == report_b["fingerprint"]
    assert report_fingerprint(_run(tmp_path, account="fp-c")) == report_a["fingerprint"]


def test_fingerprint_changes_with_economic_content(tmp_path):
    engine_a = _run(tmp_path, account="eco-a")
    engine, _, feed = make_engine(tmp_path, days=[DAY0], account="eco-b")
    engine.bars_source = feed_for([DAY0], seed=20269999)  # different closes
    drive(engine, day_ticks(DAY0))
    assert engine_a.report()["fingerprint"] != engine.report()["fingerprint"]


def test_persisted_report_matches_live_build(tmp_path):
    engine = _run(tmp_path, account="persist")
    engine.save_report_payload()
    stored = engine.store.load_report(DAY0)
    assert stored is not None
    live = build_report(engine)
    for key in live:
        if key in ("report_created_at",):
            continue
        assert live[key] == stored[key], key


def test_report_json_is_valid_and_serializable(tmp_path):
    engine = _run(tmp_path, account="jsonr")
    engine.save_report_payload()
    text = (engine.store.reports_dir / f"{engine.config.account}.{DAY0}.json").read_text(encoding="utf-8")
    parsed = json.loads(text)
    assert parsed["accounting"]["cash_consistent"] is True
    assert parsed["paper_only"] is True