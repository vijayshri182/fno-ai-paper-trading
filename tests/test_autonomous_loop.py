"""Focused deterministic tests for the autonomous research/validation loop.

Covers the fix that a protocol ``BLOCKED`` condition never silently terminates
the workflow, the human-decision gate, the fresh untouched single-use OOS pool,
the fixed safety invariants, and that Algorithm Health color is never a terminal
condition independent of the protocol state machine.  No network, no OOS replay,
no live side effects.
"""
from __future__ import annotations

import dataclasses
import datetime as _dt
import importlib.util
import json
import os
from pathlib import Path

import pytest

from fno_ai_paper_trading.autonomous.fresh_data import (
    FreshDataPool,
    FreshDataReadiness,
    FreshOosRequirement,
    FreshWindow,
    STATUS_CONSUMED,
    STATUS_UNTOUCHED,
)
from fno_ai_paper_trading.autonomous.loop import (
    ALL_TERMINAL_TOKENS,
    ALLOWED_LOOP_ACTIONS,
    TERMINAL_CONTINUE_AUTONOMOUSLY,
    TERMINAL_GREEN_REQUIRES_HUMAN_APPROVAL,
    TERMINAL_HUMAN_DECISION_REQUIRED,
    TERMINAL_WAITING_FOR_FRESH_OOS_DATA,
    AutonomousLoopConfig,
    AutonomousResearchLoop,
    default_blockage,
)
from fno_ai_paper_trading.autonomous.states import Blockage, BlockageType, LoopState, PathKind

NOW = "2026-09-17T00:00:00Z"
HASH_64 = "a" * 64
BOUNDARY = _dt.date(2026, 9, 11)


def make_config(**kwargs) -> AutonomousLoopConfig:
    defaults = dict(now=NOW)
    defaults.update(kwargs)
    return AutonomousLoopConfig(**defaults)


def make_window(
    *,
    name: str = "window_x",
    start: _dt.date = _dt.date(2026, 9, 16),
    days: int = 25,
    bars: int = 1900,
    data_hash: str = HASH_64,
) -> FreshWindow:
    end = start + _dt.timedelta(days=days - 1)
    return FreshWindow(
        name=name,
        start=start,
        end=end,
        bars=bars,
        days=days,
        data_hash=data_hash,
    )


def make_pool(requirement: FreshOosRequirement | None = None) -> FreshDataPool:
    return FreshDataPool(
        requirement or FreshOosRequirement(first_bar_after=BOUNDARY)
    )


def write_dataset(
    directory: Path,
    name: str,
    start: _dt.date,
    bars: int,
    days: int,
    data_hash: str = HASH_64,
) -> None:
    end = start + _dt.timedelta(days=days - 1)
    (directory / f"{name}.meta.json").write_text(
        json.dumps(
            {
                "schema_version": "1",
                "provider": "upstox",
                "interval": "5m",
                "start_date": start.isoformat(),
                "end_date": end.isoformat(),
                "num_bars": bars,
                "data_hash": data_hash,
            }
        ),
        encoding="utf-8",
    )
    per_day = max(1, bars // days)
    rows = []
    for day_index in range(days):
        day = start + _dt.timedelta(days=day_index)
        for bar in range(min(per_day, bars)):
            rows.append(f"{day.isoformat()}T09:15:00,0,0,0,0,0")
    (directory / f"{name}.csv").write_text(
        "timestamp,open,high,low,close,volume\n" + "\n".join(rows) + "\n",
        encoding="utf-8",
    )


# --------------------------------------------------------------------------- #
# 1) BLOCKED with no valid path -> HUMAN_DECISION_REQUIRED (A/B/C)
# --------------------------------------------------------------------------- #


def test_blocked_creates_human_decision_when_no_valid_path() -> None:
    config = make_config(
        fresh_data_acquisition_permitted=False,
        allow_research_families=False,
        data_acquisition_available=False,
    )
    decision = AutonomousResearchLoop(config, make_pool()).run(LoopState.BLOCKED)
    assert decision.terminal_token == TERMINAL_HUMAN_DECISION_REQUIRED
    assert decision.next_action == PathKind.AWAIT_HUMAN_DECISION.value
    assert decision.human_decision_report is not None
    body = decision.human_decision_report.to_markdown()
    assert body.rstrip().endswith("Choose A / B / C")
    letters = [letter for letter, _ in decision.human_decision_report.options]
    assert letters == ["A", "B", "C"]


# --------------------------------------------------------------------------- #
# 2) BLOCKED with a valid autonomous path -> CONTINUE
# --------------------------------------------------------------------------- #


def test_blocked_continues_when_valid_path_exists() -> None:
    config = make_config(
        allow_research_families=True,
        pending_research_task=True,
        data_acquisition_available=False,
    )
    decision = AutonomousResearchLoop(config, make_pool()).run(LoopState.BLOCKED)
    assert decision.terminal_token == TERMINAL_CONTINUE_AUTONOMOUSLY
    assert decision.next_action == PathKind.OTHER_FAMILY_RESEARCH.value
    assert decision.human_decision_report is None


# --------------------------------------------------------------------------- #
# 3) BLOCKED never re-runs or re-consumes the protected OOS window
# --------------------------------------------------------------------------- #


def test_blocked_does_not_retry_consumed_oos() -> None:
    pool = make_pool()
    pool.add_window(make_window())
    pool.register("window_x")
    pool.consume("window_x", now=_dt.datetime(2026, 9, 17, 0, 0, 0))
    with pytest.raises(ValueError):
        pool.consume("window_x", now=_dt.datetime(2026, 9, 17, 0, 0, 1))

    decision = AutonomousResearchLoop(make_config(), pool).run(LoopState.BLOCKED)
    assert decision.terminal_token in (
        TERMINAL_CONTINUE_AUTONOMOUSLY,
        TERMINAL_WAITING_FOR_FRESH_OOS_DATA,
    )
    frame = decision.blockage
    assert frame is not None and frame.blockage_type is BlockageType.OOS_ALREADY_CONSUMED
    assert any("2025-10-06..2026-09-11" in line for line in frame.prohibited)
    if decision.terminal_token == TERMINAL_WAITING_FOR_FRESH_OOS_DATA:
        assert decision.next_action == PathKind.AWAIT_FRESH_OOS_DATA.value
    assert not any(path.ready and path.autonomous for path in decision.paths)


# --------------------------------------------------------------------------- #
# 4) RED does not auto-terminate-all; escalates only when nothing remains
# --------------------------------------------------------------------------- #


def test_red_does_not_auto_terminate_all() -> None:
    config_with_path = make_config(pending_research_task=True)
    decision = AutonomousResearchLoop(config_with_path, make_pool()).run(LoopState.RED)
    assert decision.terminal_token == TERMINAL_CONTINUE_AUTONOMOUSLY
    assert decision.next_action == PathKind.OTHER_FAMILY_RESEARCH.value

    config_no_path = make_config(
        fresh_data_acquisition_permitted=False,
        allow_research_families=False,
    )
    decision = AutonomousResearchLoop(config_no_path, make_pool()).run(LoopState.RED)
    assert decision.terminal_token == TERMINAL_HUMAN_DECISION_REQUIRED
    assert decision.human_decision_report is not None


# --------------------------------------------------------------------------- #
# 5) INSUFFICIENT_EVIDENCE names the exact missing evidence
# --------------------------------------------------------------------------- #


def test_insufficient_evidence_identifies_missing_evidence() -> None:
    pool = make_pool()
    pool.add_window(make_window(name="tiny", days=1, bars=75))
    config = make_config(data_acquisition_available=True)
    decision = AutonomousResearchLoop(config, pool).run(LoopState.INSUFFICIENT_EVIDENCE)
    assert decision.terminal_token == TERMINAL_CONTINUE_AUTONOMOUSLY
    assert decision.next_action == PathKind.FRESH_OOS_ACQUISITION.value
    assert "20 trading days" in decision.requirements_message
    assert "1500 bars" in decision.requirements_message
    readiness = pool.readiness()
    assert readiness.missing_days >= 19
    assert readiness.missing_bars >= 1425

    locked = make_config(
        data_acquisition_available=False,
        fresh_data_acquisition_permitted=True,
    )
    decision = AutonomousResearchLoop(locked, pool).run(LoopState.INSUFFICIENT_EVIDENCE)
    assert decision.terminal_token == TERMINAL_HUMAN_DECISION_REQUIRED
    assert "20 trading days" in decision.human_decision_report.data_required


# --------------------------------------------------------------------------- #
# 6) Fresh data stays UNTOUCHED until explicitly registered
# --------------------------------------------------------------------------- #


def test_fresh_data_untouched_before_registration(tmp_path: Path) -> None:
    write_dataset(tmp_path, "upstox_Nifty_50_5m_20260916_20260916", _dt.date(2026, 9, 16), 75, 1)
    pool = FreshDataPool.from_datasets(
        tmp_path,
        FreshOosRequirement(first_bar_after=BOUNDARY),
    )
    window = pool.get("upstox_Nifty_50_5m_20260916_20260916")
    assert window is not None
    assert window.status == STATUS_UNTOUCHED
    assert window.registered is False
    assert window.data_hash == HASH_64
    assert window.days == 1
    with pytest.raises(ValueError):
        pool.consume(window.name, now=_dt.datetime(2026, 9, 17, 0, 0, 0))
    readiness = pool.readiness()
    assert readiness.ready is False
    assert any("no collected window is registered yet" in reason for reason in readiness.reasons)
    registered = pool.register(window.name)
    assert registered.registered is True
    assert pool.readiness().ready is False  # still too small, but now consumable


# --------------------------------------------------------------------------- #
# 7) A registered fresh OOS window is consumed exactly once
# --------------------------------------------------------------------------- #


def test_registered_oos_consumed_once_only() -> None:
    pool = make_pool()
    pool.add_window(make_window())
    pool.register("window_x")
    now = _dt.datetime(2026, 9, 17, 0, 0, 0)
    consumed = pool.consume("window_x", now=now)
    assert consumed.status == STATUS_CONSUMED
    assert consumed.first_used.endswith("Z")
    with pytest.raises(ValueError):
        pool.consume("window_x", now=now)
    assert pool.get("window_x").status == STATUS_CONSUMED
    assert pool.readiness().ready is False


# --------------------------------------------------------------------------- #
# 8) GREEN stops autonomous work and requires human approval
# --------------------------------------------------------------------------- #


def test_green_stops_for_human_approval() -> None:
    decision = AutonomousResearchLoop(make_config(), make_pool()).run(LoopState.GREEN)
    assert decision.terminal_token == TERMINAL_GREEN_REQUIRES_HUMAN_APPROVAL
    assert decision.next_action == PathKind.AWAIT_HUMAN_APPROVAL.value
    assert decision.human_decision_report is not None
    assert decision.human_decision_report.requires_human_approval is True
    assert decision.human_decision_report.to_markdown().rstrip().endswith("Choose A / B / C")
    report = decision.report_markdown()
    assert "**LIVE GATE**: CLOSED" in report
    assert "**Real orders**: NONE SENT" in report


# --------------------------------------------------------------------------- #
# 9) Data-access dependency is distinct from a research-validation block
# --------------------------------------------------------------------------- #


def test_auth_dependency_distinguished_from_research_blocked() -> None:
    dependency = Blockage(
        candidate="OUR-ALGO-004",
        stage="data_acquisition",
        blockage_type=BlockageType.DATA_ACQUISITION_DEPENDENCY,
        reason="data provider returned 401; access token absent",
        detected_at=NOW,
    )
    config = make_config(data_acquisition_available=False)
    decision = AutonomousResearchLoop(config, make_pool()).run(
        LoopState.BLOCKED, blockage=dependency
    )
    assert decision.terminal_token == TERMINAL_WAITING_FOR_FRESH_OOS_DATA
    assert decision.blockage.blockage_type is BlockageType.DATA_ACQUISITION_DEPENDENCY
    assert "credential-gated" in decision.requirements_message
    assert "FNO_UPSTOX_ACCESS_TOKEN" in decision.requirements_message

    research_blocked = Blockage(
        candidate="OUR-ALGO-004",
        stage="protected_oos_validation",
        blockage_type=BlockageType.RESEARCH_VALIDATION_BLOCKED,
        reason="validation stage forbidden by protocol",
        detected_at=NOW,
    )
    config = make_config(
        fresh_data_acquisition_permitted=False,
        allow_research_families=False,
    )
    decision = AutonomousResearchLoop(config, make_pool()).run(
        LoopState.BLOCKED, blockage=research_blocked
    )
    assert decision.terminal_token == TERMINAL_HUMAN_DECISION_REQUIRED
    assert decision.blockage.blockage_type is BlockageType.RESEARCH_VALIDATION_BLOCKED


# --------------------------------------------------------------------------- #
# 10) Live gate, promotion, health and orders stay untouched on every outcome
# --------------------------------------------------------------------------- #


def test_live_gate_remains_closed() -> None:
    loop = AutonomousResearchLoop(make_config(), make_pool())
    decisions = [
        loop.run(LoopState.BLOCKED),
        loop.run(LoopState.RED),
        loop.run(LoopState.GREEN),
    ]
    for decision in decisions:
        assert decision.live_gate_closed is True
        assert decision.promotion is False
        assert decision.algo_ready == "NO"
        assert decision.algorithm_health == "RED"
        assert decision.real_order_sent is False
        encoded = decision.to_dict()
        assert encoded["live_gate_closed"] is True
        assert encoded["promotion"] is False

    with pytest.raises(ValueError):
        dataclasses.replace(loop.run(LoopState.GREEN), live_gate_closed=False)
    assert loop.run(LoopState.GREEN).live_gate_closed is True


# --------------------------------------------------------------------------- #
# 11) No decision ever places or sends a real order
# --------------------------------------------------------------------------- #


def test_no_real_order_sent() -> None:
    loop = AutonomousResearchLoop(make_config(), make_pool())
    for state in (
        LoopState.BLOCKED,
        LoopState.RED,
        LoopState.INSUFFICIENT_EVIDENCE,
        LoopState.GREEN,
    ):
        decision = loop.run(state)
        assert decision.next_action in ALLOWED_LOOP_ACTIONS
        assert "order" not in decision.next_action
        assert "place" not in decision.next_action
        assert "execute" not in decision.next_action
        assert "NONE SENT" in decision.report_markdown()
    forbidden = ("send_order", "place_order", "broker_order", "live_execution")
    for action in ALLOWED_LOOP_ACTIONS:
        assert action not in forbidden


# --------------------------------------------------------------------------- #
# 12) Credential values are never echoed into reports, pool or ledger
# --------------------------------------------------------------------------- #


def test_no_credential_leakage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "S3CR3T-UPSTOX-TOKEN-VALUE"
    monkeypatch.setenv("FNO_UPSTOX_ACCESS_TOKEN", secret)
    repo = tmp_path / "repo"
    datasets = repo / "datasets"
    datasets.mkdir(parents=True)
    exporter = Path(__file__).resolve().parents[1] / "scripts" / "run_autonomous_loop.py"
    spec = importlib.util.spec_from_file_location("run_autonomous_loop", exporter)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    out = repo / "reports" / "autonomous" / "autonomous_loop_update.md"
    ledger = repo / "docs" / "RESEARCH_DECISION_LEDGER.md"
    module.main(
        [
            "--repo", str(repo),
            "--datasets-dir", str(datasets),
            "--pool-path", str(repo / "reports" / "autonomous" / "pool.json"),
            "--ledger", str(ledger),
            "--out", str(out),
            "--now", NOW,
            "--data-acquisition-available", "no",
        ]
    )
    assert secret not in out.read_text(encoding="utf-8")
    assert secret not in ledger.read_text(encoding="utf-8")
    assert secret not in (repo / "reports" / "autonomous" / "pool.json").read_text(
        encoding="utf-8"
    )
    assert "FNO_UPSTOX_ACCESS_TOKEN" in out.read_text(encoding="utf-8")
    monkeypatch.delenv("FNO_UPSTOX_ACCESS_TOKEN", raising=False)


# --------------------------------------------------------------------------- #
# 13) WS 7.25 -- Algorithm Health color is never a terminal condition
#     These tests prove that a health-color GREEN does not terminate the loop
#     when a valid autonomous protocol path remains.  The loop's continuation
#     is decided solely by paths and pool readiness.
# --------------------------------------------------------------------------- #


def _insufficient_pool() -> FreshDataPool:
    """Pool with a single small window -- clearly does not meet 20d/1500b."""
    pool = make_pool()
    pool.add_window(make_window(name="tiny", days=1, bars=75))
    return pool


def _ready_pool() -> FreshDataPool:
    """Pool with one window that meets 20d/1500b and is registered."""
    pool = make_pool()
    pool.add_window(make_window())
    pool.register("window_x")
    return pool


# A. RED + insufficient fresh pool => CONTINUE / acquire


def test_red_insufficient_pool_continues_fresh_acquisition() -> None:
    decision = AutonomousResearchLoop(
        make_config(data_acquisition_available=True), _insufficient_pool()
    ).run(LoopState.RED)
    assert decision.terminal_token == TERMINAL_CONTINUE_AUTONOMOUSLY
    assert decision.next_action == PathKind.FRESH_OOS_ACQUISITION.value
    assert decision.live_gate_closed is True
    assert decision.promotion is False


# B. AMBER health does not exist as a loop state or terminal;
#    RED and GREEN produce identical continuation over an insufficient pool.


def test_amber_health_not_a_loop_state() -> None:
    """AMBER is an algorithm-health color, never a LoopState."""
    assert not hasattr(LoopState, "AMBER")
    assert "AMBER" not in ALL_TERMINAL_TOKENS
    assert "AMBER" not in ALLOWED_LOOP_ACTIONS


@pytest.mark.parametrize("state", [LoopState.RED, LoopState.GREEN])
def test_health_color_does_not_change_continuation(state) -> None:
    """Given the same path/pool config, RED and GREEN yield the same token."""
    config = make_config(data_acquisition_available=True)
    decision = AutonomousResearchLoop(config, _insufficient_pool()).run(state)
    assert decision.terminal_token == TERMINAL_CONTINUE_AUTONOMOUSLY
    assert decision.next_action == PathKind.FRESH_OOS_ACQUISITION.value


# C. GREEN + insufficient fresh pool => must NOT terminate merely because GREEN


def test_green_insufficient_pool_does_not_terminate() -> None:
    decision = AutonomousResearchLoop(
        make_config(data_acquisition_available=True), _insufficient_pool()
    ).run(LoopState.GREEN)
    assert decision.terminal_token == TERMINAL_CONTINUE_AUTONOMOUSLY
    assert decision.next_action == PathKind.FRESH_OOS_ACQUISITION.value
    assert decision.human_decision_report is None
    assert decision.live_gate_closed is True
    assert decision.promotion is False
    assert decision.real_order_sent is False


# D. GREEN + ALGO READY=NO => must NOT promote


def test_green_always_no_promotion() -> None:
    for state in (LoopState.BLOCKED, LoopState.RED, LoopState.GREEN):
        decision = AutonomousResearchLoop(make_config(), _insufficient_pool()).run(
            state
        )
        assert decision.promotion is False
        assert decision.algo_ready == "NO"


# E. GREEN + LIVE GATE=CLOSED => must NOT execute live trading


def test_green_gate_closed_no_live_trading() -> None:
    decision = AutonomousResearchLoop(
        make_config(data_acquisition_available=True), _insufficient_pool()
    ).run(LoopState.GREEN)
    assert decision.live_gate_closed is True
    assert decision.real_order_sent is False
    assert "order" not in decision.next_action
    assert "place" not in decision.next_action
    assert "execute" not in decision.next_action
    encoded = decision.to_dict()
    assert encoded["live_gate_closed"] is True
    assert encoded["real_order_sent"] is False


# F. Promotion criteria not satisfied => Promotion remains NO


def test_promotion_remains_no_regardless_of_health() -> None:
    for state in (LoopState.RED, LoopState.GREEN, LoopState.INSUFFICIENT_EVIDENCE):
        for pool in (_insufficient_pool(), make_pool()):
            config = make_config(data_acquisition_available=True)
            decision = AutonomousResearchLoop(config, pool).run(state)
            assert decision.promotion is False, f"state={state}, pool=..."


# G. Protected OOS remains frozen -- never touched by any loop decision


def test_protected_oos_frozen_under_all_states() -> None:
    for state in (
        LoopState.BLOCKED,
        LoopState.RED,
        LoopState.GREEN,
        LoopState.INSUFFICIENT_EVIDENCE,
    ):
        decision = AutonomousResearchLoop(make_config(), make_pool()).run(state)
        if decision.blockage is not None:
            assert decision.blockage.blockage_type is BlockageType.OOS_ALREADY_CONSUMED
            assert any(
                "2025-10-06..2026-09-11" in line
                for line in decision.blockage.prohibited
            )
            assert any(
                "relabel" in line for line in decision.blockage.prohibited
            )
        assert "2025-10-06..2026-09-11" not in decision.next_action


# H. Fresh pool stays untouched / unregistered until consumed via pool API


def test_fresh_pool_untouched_after_loop_decision() -> None:
    config = make_config(data_acquisition_available=True)
    loop = AutonomousResearchLoop(config, _insufficient_pool())
    for state in (LoopState.RED, LoopState.GREEN, LoopState.BLOCKED):
        decision = loop.run(state)
        assert decision.terminal_token != TERMINAL_HUMAN_DECISION_REQUIRED
    pool = _insufficient_pool()
    for w in pool.windows:
        assert w.status == STATUS_UNTOUCHED
        assert w.registered is False
    registered = pool.register("tiny")
    assert registered.registered is True
    assert pool.get("tiny").status == STATUS_UNTOUCHED


# I. True terminal -- still terminates when protocol actually requires it


def test_true_terminal_when_no_protocol_path_remains() -> None:
    locked = make_config(
        fresh_data_acquisition_permitted=False,
        allow_research_families=False,
        data_acquisition_available=False,
    )
    for state in (LoopState.BLOCKED, LoopState.RED):
        decision = AutonomousResearchLoop(locked, make_pool()).run(state)
        assert decision.terminal_token == TERMINAL_HUMAN_DECISION_REQUIRED
        assert decision.next_action == PathKind.AWAIT_HUMAN_DECISION.value
    green_decision = AutonomousResearchLoop(locked, make_pool()).run(LoopState.GREEN)
    assert green_decision.terminal_token == TERMINAL_GREEN_REQUIRES_HUMAN_APPROVAL
    assert green_decision.next_action == PathKind.AWAIT_HUMAN_APPROVAL.value
    assert green_decision.human_decision_report is not None
    assert green_decision.human_decision_report.requires_human_approval is True
    assert green_decision.human_decision_report.to_markdown().rstrip().endswith(
        "Choose A / B / C"
    )