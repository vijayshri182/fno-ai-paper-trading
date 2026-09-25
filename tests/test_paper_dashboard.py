"""Tests for the Paper Trading Transparency Dashboard (reporting/paper_dashboard.py).

The dashboard must be derived strictly from persisted artifacts: no number may
be invented, missing metrics render as NOT AVAILABLE, paper vs research P&L stay
separate, and generation is deterministic and traceable.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from fno_ai_paper_trading.evaluation.paper_trades import (
    AttributedPaperTrade,
    save_paper_trades,
)
from fno_ai_paper_trading.reporting import (
    NOT_AVAILABLE,
    OPTION_DATA_UNAVAILABLE,
    OPTION_PROFIT_NOT_PROVEN,
    build_dashboard,
    render_html,
    trade_stats,
    write_dashboard,
)

_GEN = datetime(2026, 9, 14, 8, 0, 0, tzinfo=timezone.utc)


def _trade(**overrides) -> AttributedPaperTrade:
    base = dict(
        strategy_id="moving_average_cross",
        strategy_family="TREND_FOLLOWING",
        strategy_version="1.0.0",
        configuration_version="abc",
        entry_time="2026-09-11T09:25:00",
        exit_time="2026-09-11T11:30:00",
        side="SELL",
        entry_price=Decimal("100"),
        exit_price=Decimal("99"),
        price_pnl=Decimal("1100.00"),
        commission=Decimal("100.00"),
        net_pnl=Decimal("1000.00"),
        signal="SELL",
        regime="sideways_normal",
        bucket="paper",
        confidence=0.8,
        exit_reason="stop_hit",
        metadata={"stop_pct": "0.02"},
    )
    base.update(overrides)
    return AttributedPaperTrade(**base)


def _session(**overrides) -> dict:
    base = {
        "generated_at": "2026-09-14T04:00:00Z",
        "summary": {
            "initial_cash": "100000",
            "cash": "101000",
            "equity": "101000",
            "realized_pnl": "1000.00",
            "realized_pnl_today": "500.00",
            "unrealized_pnl": "0",
            "open_quantity": 0,
            "orders_submitted": 1, "fills": 2, "trades": 1,
            "rejections": 0, "skips": 3, "consumed_bars": 75,
            "wins": 1, "losses": 0, "win_rate": 1.0,
        },
        "positions": [],
        "ledger": [],
        "trades": [],
    }
    base.update(overrides)
    return base


def _noop_sources(**overrides) -> dict:
    sources = {
        "paper_trades": [],
        "session_reports": [],
        "discovery_payload": None,
        "discovery_path": None,
        "heartbeat": None,
        "rook": {},
    }
    sources.update(overrides)
    return sources


# ---------------------------------------------------------------------------
# fundamentals (no data at all)
# ---------------------------------------------------------------------------

class TestNoData:
    def test_no_data_reconciliation_is_insufficient(self) -> None:
        dash = build_dashboard(_noop_sources(), generated_at=_GEN)
        assert dash.reconciliation.status == "INSUFFICIENT_DATA"
        assert dash.sections["1_capital_summary"]["starting_capital"] == NOT_AVAILABLE

    def test_no_paper_trades_is_stated_everywhere(self) -> None:
        dash = build_dashboard(_noop_sources(), generated_at=_GEN)
        assert dash.sections["4_trade_ledger"]["no_paper_trades"] is True
        assert dash.sections["4_trade_ledger"]["count"] == 0
        assert NOT_AVAILABLE in render_html(dash)

    def test_option_readiness_is_not_available(self) -> None:
        dash = build_dashboard(_noop_sources(), generated_at=_GEN)
        option = dash.sections["15_option_readiness"]
        assert option["option_data_status"] == OPTION_DATA_UNAVAILABLE
        assert option["option_profit_status"] == OPTION_PROFIT_NOT_PROVEN
        assert option["checks"]["Premium"] == "NOT AVAILABLE"

    def test_algo_ready_defaults_no_and_live_closed(self) -> None:
        dash = build_dashboard(_noop_sources(), generated_at=_GEN)
        health = dash.sections["17_system_health"]
        assert health["algo_ready"] == "NO"
        assert health["live_trading"] == "CLOSED"

    def test_generation_is_deterministic(self) -> None:
        a = build_dashboard(_noop_sources(), generated_at=_GEN)
        b = build_dashboard(_noop_sources(), generated_at=_GEN)
        assert a.sections == b.sections
        assert a.reconciliation.status == b.reconciliation.status


# ---------------------------------------------------------------------------
# reconciliation + capital math
# ---------------------------------------------------------------------------

class TestReconciliation:
    def test_reconciliation_ok_for_flat_consistent_account(self) -> None:
        trades = [_trade()]
        sources = _noop_sources(paper_trades=trades, session_reports=[_session()])
        dash = build_dashboard(sources, generated_at=_GEN)
        assert dash.reconciliation.status == "OK"
        assert "e1_starting_plus_pnl" in dash.reconciliation.checks
        assert dash.sections["1_capital_summary"]["net_pnl"] == "₹1,000.00"

    def test_reconciliation_failed_on_math_mismatch(self) -> None:
        trades = [_trade()]
        session = _session()
        session["summary"]["equity"] = "99000"
        session["summary"]["cash"] = "99000"
        sources = _noop_sources(paper_trades=trades, session_reports=[session])
        dash = build_dashboard(sources, generated_at=_GEN)
        assert dash.reconciliation.status == "FAILED"
        assert any("Equation 1 mismatch" in p for p in dash.reconciliation.problems)

    def test_capital_summary_math(self) -> None:
        trades = [_trade()]
        sources = _noop_sources(paper_trades=trades, session_reports=[_session()])
        cap = build_dashboard(sources, generated_at=_GEN).sections["1_capital_summary"]
        assert cap["starting_capital"] == "₹100,000.00"
        assert cap["available_cash"] == "₹101,000.00"
        assert cap["realized_pnl"] == "₹1,000.00"
        assert cap["unrealized_pnl"] == "₹0.00"
        assert cap["return_pct"] == "1.00%"

    def test_ledger_completeness(self) -> None:
        trades = [_trade(entry_time="2026-09-11T09:25:00", exit_time="2026-09-11T11:30:00"),
                       _trade(price_pnl=Decimal("-100.00"), net_pnl=Decimal("-200.00"), side="BUY",
                              entry_time="2026-09-12T09:25:00", exit_time="2026-09-12T11:30:00")]
        dash = build_dashboard(_noop_sources(paper_trades=trades, session_reports=[]), generated_at=_GEN)
        ledger = dash.sections["4_trade_ledger"]
        assert ledger["count"] == 2
        rows = ledger["rows"]
        assert {r["exit_reason"] for r in rows} == {"stop_hit"}
        stats = trade_stats(trades)
        assert stats["wins"] + stats["losses"] + stats["breakeven"] == stats["trades"] == 2
        assert stats["gross_pnl"] - stats["costs"] == stats["net_pnl"]

    def test_lifecycle_persistence_only_recorded_fields(self) -> None:
        trades = [_trade(metadata={}, regime="sideways_normal")]
        lifecycle = build_dashboard(
            _noop_sources(paper_trades=trades, session_reports=[]), generated_at=_GEN
        ).sections["6_lifecycle"]
        row = lifecycle["rows"][0]
        assert "entry_time" in row["timestamps"] and "exit_time" in row["timestamps"]
        assert "P&L calculated" in row["recorded_steps"]
        assert set(row["recorded_steps"]).isdisjoint(row["missing_steps"])


# ---------------------------------------------------------------------------
# risk / no-trade / research separation / provenance
# ---------------------------------------------------------------------------

class TestRiskAndAttribution:
    def test_risk_utilization_reads_session_and_heartbeat(self) -> None:
        heartbeat = {"skips": 7, "rejections": 2, "cycle_at": "2026-09-14T04:00:00Z",
                     "safety_decision": "HOLD_ON_EDGE"}
        sources = _noop_sources(session_reports=[_session()], heartbeat=heartbeat)
        risk = build_dashboard(sources, generated_at=_GEN).sections["12_risk_dashboard"]
        assert risk["daily_loss_used"] == "₹500.00"
        assert risk["risk_limit_blocks"] == 2
        assert risk["no_trade_due_to_risk"] == 7
        no_trade = build_dashboard(sources, generated_at=_GEN).sections["13_no_trade"]
        assert no_trade["no_trade_count"] == 7

    def test_no_trade_reason_codes_all_frozen_and_zero(self) -> None:
        no_trade = build_dashboard(_noop_sources(), generated_at=_GEN).sections["13_no_trade"]
        assert set(no_trade["reason_codes"]) == {
            "SIDEWAYS_MARKET", "WEAK_TREND", "LOW_VOLATILITY", "EXTREME_VOLATILITY",
            "NO_PULLBACK", "NO_MOMENTUM", "NO_BREAKOUT", "POOR_RISK_REWARD",
            "INSUFFICIENT_EXPECTED_EDGE", "HIGH_COST", "TIME_CUTOFF",
            "RISK_LIMIT", "DAILY_LOSS_LIMIT",
        }
        assert all(v == 0 for v in no_trade["reason_codes"].values())

    def test_research_and_paper_are_separate(self) -> None:
        discovery = {
            "config": {"first_date": "2025-01-02", "last_date": "2025-10-03"},
            "candidates_generated": 7,
            "current_best": {"candidate_id": "d1_trend_ema", "version": "1.0"},
            "why_best": "highest bounded score on validation",
            "algo_ready": "NO",
            "provenance": {"blocked": "PARTIAL FRESH FETCH - feb-mar windows recovered via bisect",
                           "freshness": "STALE (partial)", "provider": "upstox_5m"},
            "watch_window": {"protected_oos_start": "2025-10-06"},
        }
        sources = _noop_sources(discovery_payload=discovery)
        block = build_dashboard(sources, generated_at=_GEN).sections["14_research_vs_paper"]
        assert block["research"]["window"] == "2025-01-02 .. 2025-10-03"
        assert block["paper"]["paper_trades"] == 0
        assert "never combined" in block["separation_note"]

    def test_stale_partial_provenance_preserved(self) -> None:
        discovery = {"provenance": {
            "provider": "upstox_5m",
            "blocked": "PARTIAL FRESH FETCH - 2 sub-windows recovered via bisection",
            "freshness": "STALE (partial)",
        }}
        sources = _noop_sources(discovery_payload=discovery)
        prov = build_dashboard(sources, generated_at=_GEN).sections["16_provenance"]
        assert prov["blocked"].startswith("PARTIAL FRESH FETCH")
        assert prov["freshness"] == "STALE (partial)"

    def test_decision_explanation_is_ledger_only(self) -> None:
        trades = [_trade(metadata={"stop_pct": "0.02"})]
        rows = build_dashboard(
            _noop_sources(paper_trades=trades, session_reports=[]), generated_at=_GEN
        ).sections["5_decision_explanation"]["rows"]
        assert len(rows) == 1
        assert "no generated fiction" in build_dashboard(
            _noop_sources(paper_trades=trades), generated_at=_GEN
        ).sections["5_decision_explanation"]["note"]


# ---------------------------------------------------------------------------
# determinism / traceability / rendering
# ---------------------------------------------------------------------------

class TestTraceability:
    def test_section_hashes_deterministic_and_traces_present(self) -> None:
        trades = [_trade()]
        sources = _noop_sources(paper_trades=trades, session_reports=[_session()])
        a = build_dashboard(sources, generated_at=_GEN)
        b = build_dashboard(sources, generated_at=_GEN)
        audit_a = a.sections["18_auditability"]
        assert audit_a["section_hashes"] == b.sections["18_auditability"]["section_hashes"]
        for name, section in a.sections.items():
            assert section.get("trace"), f"{name}: missing trace"

    def test_render_html_is_standalone_and_complete(self) -> None:
        trades = [_trade()]
        sources = _noop_sources(paper_trades=trades, session_reports=[_session()])
        html = render_html(build_dashboard(sources, generated_at=_GEN))
        assert html.startswith("<!doctype html>")
        for marker in ("AT A GLANCE", "MONEY RECONCILIATION", "NO TRADE TRANSPARENCY",
                       "OPTION READINESS", "ALGO READY", "1. CAPITAL SUMMARY"):
            assert marker in html

    def test_write_dashboard_emits_html_and_json(self, tmp_path) -> None:
        trades = [_trade()]
        sources = _noop_sources(paper_trades=trades, session_reports=[_session()])
        dash = build_dashboard(sources, generated_at=_GEN)
        html_path = tmp_path / "dash.html"
        json_path = tmp_path / "dash.json"
        written = write_dashboard(dash, html_path, json_path)
        assert written["html"].exists()
        assert written["json"].exists()
        import json
        payload = json.loads(json_path.read_text(encoding="utf-8"))
        assert payload["sections"]["14_research_vs_paper"]["paper"]["paper_trades"] == 1

    def test_rendered_html_does_not_fabricate_option_profit(self) -> None:
        html = render_html(build_dashboard(_noop_sources(), generated_at=_GEN))
        assert OPTION_PROFIT_NOT_PROVEN in html