"""WS 7.18 walk-forward engine tests.

Proves the temporal-discipline guarantees: the algorithm used on day D is frozen
before D is evaluated, evidence from D affects only later days, challengers
validate on future days only, promotions take effect the *next* trading day, the
protected out-of-sample period is never loaded, history is append-only, nothing
is tuned, and no live-trading code is reachable from the walk-forward package.
"""
from __future__ import annotations

import ast
import json
from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest import mock

import pytest

from fno_ai_paper_trading.evaluation.five_year import DayBars
from fno_ai_paper_trading.models.enums import InstrumentType
from fno_ai_paper_trading.models.instruments import Instrument
from fno_ai_paper_trading.models.market import MarketPrice

from fno_ai_paper_trading.walkforward.catalog import CATALOG_BY_KEY
from fno_ai_paper_trading.walkforward.config import WalkForwardConfig
from fno_ai_paper_trading.walkforward.engine import WalkForwardEngine
from fno_ai_paper_trading.walkforward.gate import (
    WalkForwardGate,
    WalkForwardGateCriteria,
    agg_window_metrics,
)
from fno_ai_paper_trading.walkforward.learning import (
    ChampionTradeSlice,
    EvidenceAggregator,
)
from fno_ai_paper_trading.walkforward.records import (
    DailyEvolutionRecord,
    EvolutionLedger,
)
from fno_ai_paper_trading.walkforward.reports import write_reports

REPO_ROOT = Path(__file__).resolve().parents[1]
WALKWARD_DIR = REPO_ROOT / "src" / "fno_ai_paper_trading" / "walkforward"

INST = Instrument(
    symbol="NIFTY1", instrument_type=InstrumentType.FUTURE, underlying_symbol="NIFTY"
)

FORBIDDEN_IMPORT_ROOTS = {
    "broker",
    "portfolio",
    "risk",
    "services",
    "paper_session",
    "sizing",
    "stop_loss",
    "execution",
    "live",
}


# ----------------------------------------------------------------- helpers --


def _bars(day: date, base: Decimal = Decimal("100")) -> list[MarketPrice]:
    import math

    out: list[MarketPrice] = []
    for i in range(78):
        wave = Decimal(str(round(math.sin(i / 6.0) * 4.0, 4)))
        price = base + wave + Decimal(i) * Decimal("0.05")
        ts = (
            datetime.combine(day, datetime.min.time()).replace(hour=9, minute=15)
            + timedelta(minutes=5 * i)
        )
        out.append(
            MarketPrice(
                instrument=INST,
                timestamp=ts,
                open=price + Decimal("0.1"),
                high=price + Decimal("1"),
                low=price - Decimal("1"),
                close=price,
                volume=1000,
            )
        )
    return out


def _domain(n: int = 42, start: date | None = None) -> list[DayBars]:
    start = start or date(2026, 1, 2)
    days: list[DayBars] = []
    base = Decimal("100")
    cursor = start
    while len(days) < n:
        if cursor.weekday() < 5:
            base += Decimal("0.2")
            days.append(
                DayBars(day=cursor, bars=tuple(_bars(cursor, base)), source_hash="wf-test")
            )
        cursor += timedelta(days=1)
    return days


def _config(**overrides) -> WalkForwardConfig:
    base = WalkForwardConfig(
        initial_capital=Decimal("100000"),
        quantity=1,
        commission_rate=Decimal("0.0003"),
        slippage_rate=Decimal("0.001"),
        enable_risk_manager=True,
        max_position_quantity=75,
        max_daily_loss=Decimal("10000"),
        enable_stop_loss=True,
        stop_loss_pct=Decimal("0.02"),
        min_evidence_days=10,
        research_cadence_days=10,
        research_window_days=20,
        nightly_review_window_days=15,
        min_hypothesis_regime_trades=6,
        max_challengers_per_round=1,
        max_challengers_total=4,
        validation_window_days=12,
        promotion_cadence_days=6,
        min_validation_trades=6,
        min_profit_factor=Decimal("1.0"),
        max_trades_per_day=10,
        min_regime_trades=3,
        regime_degradation_tolerance=Decimal("0.75"),
    )
    return replace(base, **overrides)


def _win_day(net: int = 50) -> dict[str, object]:
    return {
        "net_pnl": str(net),
        "costs": "1",
        "slippage": "0",
        "trades": 1,
        "wins": 1,
        "losses": 0,
        "gross_profit": str(net + 1),
        "gross_loss": "0",
        "max_drawdown_pct": "0",
        "worst_loss": None,
        "exposure_pct": "100",
        "per_regime": {"up_normal": {"trades": 1, "wins": 1, "realized_pnl": str(net)}},
    }


def _lose_day(net: int = -50) -> dict[str, object]:
    return {
        "net_pnl": str(net),
        "costs": "1",
        "slippage": "0",
        "trades": 1,
        "wins": 0,
        "losses": 1,
        "gross_profit": "0",
        "gross_loss": str(abs(net)),
        "max_drawdown_pct": "0",
        "worst_loss": str(abs(net)),
        "exposure_pct": "100",
        "per_regime": {"up_normal": {"trades": 1, "wins": 0, "realized_pnl": str(net)}},
    }


def _metrics(
    day_count: int, day_builder, start: date | None = None
) -> dict[str, object]:
    start = start or date(2026, 6, 1)
    return {
        (start + timedelta(days=i)).isoformat(): day_builder()
        for i in range(day_count)
    }


# ------------------------------------------------------------ engine basics --


def test_ledger_has_all_21_fields_every_day():
    result = WalkForwardEngine(_config()).run(_domain())
    records = result.ledger_records
    assert len(records) == result.days_processed
    assert [r["1.day"] for r in records] == sorted(r["1.day"] for r in records)
    required = DailyEvolutionRecord.required_field_keys()
    for row in records:
        assert required.issubset(set(row.keys())), sorted(required - set(row.keys()))


def test_algorithm_for_day_frozen_before_day():
    result = WalkForwardEngine(_config()).run(_domain())
    records = result.ledger_records
    for index in range(1, len(records)):
        prev_next = records[index - 1]["20.next_day_algorithm"]
        assert records[index]["2.algorithm_used"] == prev_next, index
    assert records[0]["2.algorithm_used"] == "model_0"
    assert records[0]["3.parent_algorithm"] is None


def test_protected_oos_period_never_loaded():
    days = _domain(20)
    oos_start = days[10].day
    cfg = _config(protected_oos_start=oos_start)
    result = WalkForwardEngine(cfg).run(days)
    # The full leading part of the domain is truncated at the boundary; OOS
    # days are never evaluated and never appear in any ledger row.
    assert result.days_processed == 10
    assert result.days_available == 10
    assert result.last_day < oos_start
    assert all(date.fromisoformat(str(r["1.day"])) < oos_start for r in result.ledger_records)
    # A domain consisting only of protected days leaves nothing to walk.
    oos_day = DayBars(day=oos_start, bars=tuple(_bars(oos_start)), source_hash="wf-test")
    with pytest.raises(ValueError, match="empty after applying bounds"):
        WalkForwardEngine(cfg).run([oos_day])


def test_run_deterministic_and_resume_byte_identical(tmp_path):
    days = _domain()
    cfg = _config()
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    full = WalkForwardEngine(cfg).run(days, out_dir=a_dir, run_name="wf")
    interrupted = WalkForwardEngine(cfg).run(
        days, out_dir=b_dir, run_name="wf", stop_after_days=8
    )
    assert interrupted.days_processed == 8
    resumed = WalkForwardEngine(cfg).run(days, out_dir=b_dir, run_name="wf", resume=True)
    assert resumed.days_processed == len(days)
    assert (a_dir / "wf.ledger.jsonl").read_bytes() == (b_dir / "wf.ledger.jsonl").read_bytes()
    assert full.champion_totals == resumed.champion_totals
    assert full.days_processed == resumed.days_processed


def test_resume_rejects_config_hash_change(tmp_path):
    days = _domain(12)
    cfg = _config()
    out = tmp_path / "run"
    WalkForwardEngine(cfg).run(days, out_dir=out, run_name="wf", stop_after_days=6)
    tampered = _config(research_window_days=17)
    with pytest.raises(ValueError, match="config_hash"):
        WalkForwardEngine(tampered).run(days, out_dir=out, run_name="wf", resume=True)


def test_ledger_is_append_only_never_rewritten(tmp_path):
    ledger_path = tmp_path / "ledger.jsonl"
    record = DailyEvolutionRecord(
        day="2026-01-02",
        algorithm_used="model_0",
        parent_algorithm=None,
        regime="up_normal",
        regime_features={"regime": "up_normal"},
        signal="CALL",
        signal_occurrences=(),
        trades=(),
        pnl="1.5",
        transaction_costs="0.3",
        slippage="0.1",
        outcome="WIN",
        win_count=1,
        loss_count=0,
        open_position_count=0,
        evidence={},
        problem_identified={"pattern": "none", "description": "ok"},
        hypothesis_generated=[],
        challenger_generated=[],
        modification_proposed=[],
        evidence_supporting={},
        validation_period_assigned=[],
        validation_status=[],
        promotion_decision=[],
        next_day_algorithm="model_0",
        why_next_day="no gate today",
    )
    second = replace(record, day="2026-01-03", pnl="2.0")
    EvolutionLedger(ledger_path).append(record)
    EvolutionLedger(ledger_path).append(second)
    reloaded = EvolutionLedger(ledger_path)
    assert reloaded.count == 2
    assert [r["1.day"] for r in reloaded.records()] == ["2026-01-02", "2026-01-03"]


# ------------------------------------------------------------ no-live policy --


def test_walkforward_imports_no_live_trading_modules():
    forbidden_lines: list[str] = []
    for py in sorted(WALKWARD_DIR.rglob("*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    leaf = alias.name.split(".")[0]
                    if leaf == "fno_ai_paper_trading":
                        second = alias.name.split(".")[1] if "." in alias.name else ""
                        if second in FORBIDDEN_IMPORT_ROOTS:
                            forbidden_lines.append(f"{py.name}: import {alias.name}")
            elif isinstance(node, ast.ImportFrom) and node.module:
                parts = node.module.split(".")
                if parts[0] == "fno_ai_paper_trading" and len(parts) > 1:
                    if parts[1] in FORBIDDEN_IMPORT_ROOTS:
                        forbidden_lines.append(f"{py.name}: from {node.module}")
    assert forbidden_lines == [], "; ".join(forbidden_lines)


# -------------------------------------------------------- challenger pipeline --

CHALLENGER_KEY = "suppress_buys_not_up"


def test_challenger_validation_is_future_only_and_leads_to_a_gate(tmp_path):
    days = _domain(42)
    cfg = _config()
    spec = CATALOG_BY_KEY[CHALLENGER_KEY]
    out = tmp_path / "run"
    with mock.patch(
        "fno_ai_paper_trading.walkforward.engine.select_challengers",
        return_value=[spec],
    ):
        result = WalkForwardEngine(cfg).run(days, out_dir=out, run_name="wf")

    challengers = {c["challenger_id"]: c for c in result.challengers}
    assert challengers, "expected a challenger to be spawned"
    cid, challenger = next(iter(challengers.items()))
    assert challenger["key"] == CHALLENGER_KEY
    created = date.fromisoformat(str(challenger["created_on"]))

    records = result.ledger_records
    by_day = {str(r["1.day"]): r for r in records}

    # The challenger never influences the algorithm, and never runs on or
    # before its creation day (future-only validation).
    validation_days = sorted(challenger.get("day_metrics", {}))
    assert all(date.fromisoformat(d) > created for d in validation_days)
    for row in records:
        assert row["2.algorithm_used"] != cid
    assert by_day[created.isoformat()]["14.challenger_generated"]
    assert by_day[created.isoformat()]["17.validation_period_assigned"]

    # Validation status rows track the assigned window.
    status_rows = [
        items
        for row in records
        for items in row["18.validation_status"]
        if items.get("challenger_id") == cid
    ]
    assert len(status_rows) >= 1
    assert all(
        int(items["required_days"]) == cfg.validation_window_days for items in status_rows
    )

    # A gate decision is recorded only after the window is complete.
    assert result.promotions, "expected at least one gate decision once the window closed"
    first_promotion = result.promotions[0]
    assert first_promotion["challenger_id"] == cid
    assert date.fromisoformat(str(first_promotion["day"])) > created
    assert by_day[first_promotion["day"]]["19.promotion_decision"]
    assert first_promotion["decision"] in ("PROMOTE", "REJECT", "INSUFFICIENT_EVIDENCE")

    if first_promotion["decision"] == "PROMOTE":
        promoted = first_promotion["promoted_version"]
        assert promoted == "model_1"
        gate_day = first_promotion["day"]
        assert by_day[gate_day]["20.next_day_algorithm"] == "model_1"
        next_row = records[records.index(by_day[gate_day]) + 1]
        assert next_row["2.algorithm_used"] == "model_1"
        versions = {v["version_id"]: v for v in result.versions}
        assert versions["model_1"]["promoted_at"] == f"{gate_day}T00:00:00"
        assert result.parent_of["model_1"] == "model_0"
        assert versions["model_0"]["status"] == "RETIRED"


def test_challenger_total_budget_cap_enforced():
    days = _domain(42)
    cfg = _config(max_challengers_total=3)
    specs = [CATALOG_BY_KEY[k] for k in ("suppress_buys_not_up", "suppress_sells_not_down")]
    with mock.patch(
        "fno_ai_paper_trading.walkforward.engine.select_challengers",
        return_value=specs,
    ):
        result = WalkForwardEngine(cfg).run(days)
    assert len(result.challengers) <= 3


def test_challenger_params_are_frozen_catalog_constants():
    days = _domain(42)
    cfg = _config()
    specs = [CATALOG_BY_KEY[k] for k in CATALOG_BY_KEY]
    with mock.patch(
        "fno_ai_paper_trading.walkforward.engine.select_challengers",
        return_value=specs,
    ):
        result = WalkForwardEngine(cfg).run(days)
    for challenger in result.challengers:
        spec = CATALOG_BY_KEY[str(challenger["key"])]
        assert challenger["strategy_params"] == dict(spec.strategy_params)


def test_open_challenger_blocks_same_key():
    from fno_ai_paper_trading.walkforward.catalog import (
        ChampionWindowStats,
        WindowSlice,
    )
    from fno_ai_paper_trading.walkforward.learning import select_challengers

    why = {
        ("UP", "BUY"): WindowSlice(trades=8, wins=6, realized_pnl=Decimal("100")),
        ("DOWN", "BUY"): WindowSlice(trades=8, wins=2, realized_pnl=Decimal("-80")),
        ("DOWN", "SELL"): WindowSlice(trades=8, wins=6, realized_pnl=Decimal("120")),
        ("UP", "SELL"): WindowSlice(trades=8, wins=2, realized_pnl=Decimal("-90")),
        ("SIDEWAYS", "BUY"): WindowSlice(trades=8, wins=1, realized_pnl=Decimal("-35")),
        ("SIDEWAYS", "SELL"): WindowSlice(trades=8, wins=1, realized_pnl=Decimal("-35")),
    }
    stats = ChampionWindowStats(
        by_regime_side=why,
        total=WindowSlice(
            trades=48,
            wins=18,
            realized_pnl=Decimal("-20"),
        ),
    )
    cfg = _config(max_challengers_per_round=4)
    unblocked = select_challengers(stats, cfg)
    assert CHALLENGER_KEY in {s.key for s in unblocked}
    blocked = select_challengers(stats, cfg, unavailable_keys={CHALLENGER_KEY})
    assert all(s.key != CHALLENGER_KEY for s in blocked)
    assert {s.key for s in blocked} == {
        "suppress_sells_not_down",
        "suppress_sideways_entries",
    }


def test_catalog_predicates_normalize_regime_trends():
    """Evidence buckets carry ``up_normal``-style labels; predicates must still
    separate on the trend token (UP/DOWN/SIDEWAYS) or they can never fire."""
    from fno_ai_paper_trading.walkforward.catalog import (
        CATALOG_BY_KEY,
        ChampionWindowStats,
        WindowSlice,
    )
    from fno_ai_paper_trading.walkforward.learning import select_challengers

    prefixed = {
        ("up_high", "BUY"): WindowSlice(trades=8, wins=6, realized_pnl=Decimal("100")),
        ("down_normal", "BUY"): WindowSlice(trades=8, wins=2, realized_pnl=Decimal("-80")),
        ("down_low", "SELL"): WindowSlice(trades=8, wins=6, realized_pnl=Decimal("120")),
        ("up_high", "SELL"): WindowSlice(trades=8, wins=2, realized_pnl=Decimal("-90")),
        ("sideways_low", "BUY"): WindowSlice(trades=8, wins=1, realized_pnl=Decimal("-35")),
        ("sideways_low", "SELL"): WindowSlice(trades=8, wins=1, realized_pnl=Decimal("-35")),
    }
    stats = ChampionWindowStats(
        by_regime_side=prefixed,
        total=WindowSlice(trades=48, wins=18, realized_pnl=Decimal("-20")),
    )
    cfg = _config(max_challengers_per_round=4)
    chosen = select_challengers(stats, cfg)
    assert CHALLENGER_KEY in {s.key for s in chosen}
    assert CATALOG_BY_KEY["suppress_sells_not_down"].predicate(stats, cfg)[0]
    assert CATALOG_BY_KEY["suppress_sideways_entries"].predicate(stats, cfg)[0]


# -------------------------------------------------------------- gate unit --


def _window(kind: str = "win", day_count: int = 20) -> dict[str, object]:
    if kind.startswith("win"):
        net = int(kind.split("_")[1]) if "_" in kind else 50
        return _metrics(day_count, lambda: _win_day(net))
    return _metrics(day_count, lambda: _lose_day())


def _gate_result(challenger_metrics, champion_metrics, base_equity=Decimal("100000")):
    gate = WalkForwardGate()
    c = agg_window_metrics(challenger_metrics)
    ch = agg_window_metrics(champion_metrics)
    return gate.evaluate(c, ch, "wfc-x-r001", base_equity=base_equity)


def test_gate_promotes_a_clearly_better_challenger():
    verdict = _gate_result(_window("win_50"), _window("win_5"))
    assert verdict.decision == "PROMOTE"
    assert verdict.promoted
    assert verdict.reasons
    assert verdict.metrics["net_pnl_delta"] == str(Decimal("900"))


def test_gate_rejects_a_losing_challenger():
    verdict = _gate_result(_window("lose"), _window("win_5"))
    assert verdict.decision == "REJECT"
    assert verdict.reasons


def test_gate_returns_insufficient_evidence_below_minimums():
    verdict = _gate_result(_window("win_50", day_count=4), _window("win_5", day_count=4))
    assert verdict.decision == "INSUFFICIENT_EVIDENCE"
    assert not verdict.promoted


def test_gate_criteria_from_config_round_trip():
    cfg = _config()
    criteria = WalkForwardGateCriteria.from_config(cfg)
    assert criteria.min_validation_days == cfg.validation_window_days
    assert criteria.min_validation_trades == cfg.min_validation_trades
    payload = criteria.to_dict()
    rebuilt = WalkForwardGateCriteria(
        min_validation_days=payload["min_validation_days"],
        min_validation_trades=payload["min_validation_trades"],
        min_profit_factor=payload["min_profit_factor"],
        max_drawdown_pct=payload["max_drawdown_pct"],
        max_consecutive_losing_days=payload["max_consecutive_losing_days"],
        max_tail_loss_pct=payload["max_tail_loss_pct"],
        min_net_pnl_per_cost=payload["min_net_pnl_per_cost"],
        max_trades_per_day=payload["max_trades_per_day"],
        min_regime_trades=payload["min_regime_trades"],
        regime_degradation_tolerance=payload["regime_degradation_tolerance"],
    )
    assert rebuilt == criteria


# ------------------------------------------------------------- aggregation --


def test_evidence_aggregator_scopes_windows_to_champion_version():
    aggregator = EvidenceAggregator()
    base = date(2026, 1, 5)
    day = base
    for i in range(5):
        aggregator.add_day(
            day + timedelta(days=i),
            [
                ChampionTradeSlice(
                    day=day + timedelta(days=i),
                    side="BUY",
                    regime_label="up_normal",
                    realized_pnl=Decimal("10"),
                    costs=Decimal("1"),
                    win=True,
                    version="model_0",
                ),
                ChampionTradeSlice(
                    day=day + timedelta(days=i),
                    side="SELL",
                    regime_label="up_normal",
                    realized_pnl=Decimal("-20"),
                    costs=Decimal("1"),
                    win=False,
                    version="model_1",
                ),
            ],
        )
    stats_v0, _ = aggregator.window_stats(base + timedelta(days=4), 10, version="model_0")
    stats_v1, _ = aggregator.window_stats(base + timedelta(days=4), 10, version="model_1")
    assert stats_v0.total.realized_pnl == Decimal("50")
    assert stats_v1.total.realized_pnl == Decimal("-100")
    totals, _ = aggregator.totals()
    assert totals.total.realized_pnl == Decimal("-50")


# ----------------------------------------------------------------- reports --


def test_reports_are_written_and_clearly_labelled(tmp_path):
    days = _domain(12)
    result = WalkForwardEngine(_config()).run(days, out_dir=tmp_path, run_name="wf")
    written = write_reports(tmp_path, result)
    assert {"summary.json", "versions.json", "promotions.jsonl",
            "evolution_timeline.html", "evolution_timeline.md"}.issubset(written)

    summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert summary["scope"]["research_only"] is True
    assert summary["scope"]["live_trading"] is False
    assert summary["algorithm_evolution"]["history_never_rewritten"] is True
    assert summary["algorithm_evolution"]["protected_oos_untouched"] is True
    assert summary["ledger"]["fields"].startswith("21-field")

    html = (tmp_path / "evolution_timeline.html").read_text(encoding="utf-8")
    assert "Algorithm evolution" in html and "Walk-forward" in html
    assert "Disclaimer" in html

    markdown = (tmp_path / "evolution_timeline.md").read_text(encoding="utf-8")
    assert "Walk-forward algorithm evolution" in markdown
    assert "|" in markdown
    assert "model_0" in markdown