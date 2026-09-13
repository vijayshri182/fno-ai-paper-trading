"""Tests for agent-level persistence (WS 7.8)."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from fno_ai_paper_trading.agent.heartbeat import AgentHeartbeat
from fno_ai_paper_trading.agent.persistence import (
    AGENT_STATE_NAME,
    AgentStateRecord,
    load_agent_state,
    save_agent_state,
)


def _record() -> AgentStateRecord:
    return AgentStateRecord(
        run_id="AGENT_TEST",
        state="MARKET_OPEN",
        state_since=datetime(2026, 9, 8, 9, 15).isoformat(timespec="seconds"),
        cycle_count=3,
        transitions=(
            {
                "source": "INIT",
                "target": "MARKET_OPEN",
                "at": datetime(2026, 9, 8, 9, 16).isoformat(timespec="seconds"),
                "reason": "boot",
            },
        ),
        jobs_last_run={
            "learning_cycle": datetime(2026, 9, 8, 16, 0).isoformat(timespec="seconds")
        },
        heartbeat=AgentHeartbeat(
            run_id="AGENT_TEST", state="MARKET_OPEN", consumed_bars=5, trades=1
        ),
    )


def test_save_load_round_trip(tmp_path):
    stored = save_agent_state(_record(), directory=tmp_path)
    assert stored.path.is_file()
    meta = stored.path.with_name(stored.path.stem + ".meta.json")
    assert meta.is_file()

    loaded = load_agent_state(stored.path)
    assert loaded.record.run_id == "AGENT_TEST"
    assert loaded.record.state == "MARKET_OPEN"
    assert loaded.record.cycle_count == 3
    assert loaded.record.transitions[0]["target"] == "MARKET_OPEN"
    assert loaded.record.jobs_last_run["learning_cycle"]
    assert loaded.record.heartbeat.consumed_bars == 5
    assert loaded.metadata["state_hash"]
    assert loaded.record.saved_at


def test_hash_detects_tampering(tmp_path):
    stored = save_agent_state(_record(), directory=tmp_path)
    stored.path.write_text(
        stored.path.read_text(encoding="utf-8").replace('"cycle_count": 3', '"cycle_count": 9'),
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        load_agent_state(stored.path)


def test_missing_meta_raises(tmp_path):
    payload = tmp_path / AGENT_STATE_NAME
    payload.write_text("{}", encoding="utf-8")
    with pytest.raises(FileNotFoundError):
        load_agent_state(payload)


def test_default_name_in_state_dir(tmp_path):
    stored = save_agent_state(_record(), directory=tmp_path)
    assert stored.path.name == AGENT_STATE_NAME


def test_saved_at_stamped(tmp_path):
    before = datetime.now().replace(microsecond=0)
    stored = save_agent_state(_record(), directory=tmp_path)
    after = datetime.now() + timedelta(seconds=2)
    saved = datetime.fromisoformat(stored.metadata["saved_at"])
    assert before <= saved <= after