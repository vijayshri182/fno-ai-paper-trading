"""Durable agent-level state (WS 7.8).

The agent persists two kinds of state independently:

* the **session** snapshot (open position, consumed candles, broker ledger) via
  the existing :mod:`~fno_ai_paper_trading.persistence.session_store` — so a
  restart resumes where the loop stopped, and
* the **agent** state (run id, current state, transitions, heartbeat, job last
  runs) stored here as one canonical JSON payload plus a deterministic
  ``state_hash`` sidecar (SHA-256 over the re-serialized canonical form).

Like ``session_store`` this module is pure persistence: no accounting, no risk
math, and restoring never re-runs any fill. All timestamps are naive IST and all
money values are ``str(Decimal)``.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from fno_ai_paper_trading.agent.heartbeat import AgentHeartbeat

AGENT_STATE_SCHEMA_VERSION = "1"

#: Default file name of the agent-state payload inside the state directory.
AGENT_STATE_NAME = "agent_state.json"
_META_SUFFIX = ".meta.json"


@dataclass(frozen=True)
class AgentStateRecord:
    """The persisted agent record (everything except the session snapshot)."""

    run_id: str
    state: str
    state_since: str
    cycle_count: int
    transitions: tuple[Mapping[str, object], ...]
    jobs_last_run: Mapping[str, str]
    heartbeat: AgentHeartbeat
    saved_at: str = ""
    schema_version: str = AGENT_STATE_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "state": self.state,
            "state_since": self.state_since,
            "cycle_count": self.cycle_count,
            "transitions": [dict(t) for t in self.transitions],
            "jobs_last_run": dict(self.jobs_last_run),
            "heartbeat": self.heartbeat.to_dict(),
            "saved_at": self.saved_at,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "AgentStateRecord":
        transitions = payload.get("transitions") or ()
        return cls(
            schema_version=str(payload.get("schema_version", AGENT_STATE_SCHEMA_VERSION)),
            run_id=str(payload.get("run_id", "")),
            state=str(payload.get("state", "INIT")),
            state_since=str(payload.get("state_since", "")),
            cycle_count=int(payload.get("cycle_count", 0)),
            transitions=tuple(dict(item) for item in transitions),
            jobs_last_run=dict(payload.get("jobs_last_run", {})),
            heartbeat=AgentHeartbeat.from_dict(payload.get("heartbeat") or {}),
            saved_at=str(payload.get("saved_at", "")),
        )


@dataclass(frozen=True)
class StoredAgentState:
    """A stored agent record plus its integrity metadata."""

    record: AgentStateRecord
    path: Path
    metadata: Mapping[str, str]

    @property
    def state_hash(self) -> str:
        return str(self.metadata["state_hash"])


def _canonical_json(payload: Any) -> str:
    return json.dumps(payload, indent=2, sort_keys=True)


def _state_hash(payload: Any) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def save_agent_state(
    record: AgentStateRecord,
    *,
    directory: str | Path,
    name: str | None = None,
) -> StoredAgentState:
    """Persist ``record`` as ``<name>.json`` + ``<name>.meta.json``."""
    target_dir = Path(directory)
    safe_name = name or AGENT_STATE_NAME
    payload_path = target_dir / safe_name
    meta_path = payload_path.with_name(payload_path.stem + _META_SUFFIX)
    payload = record.to_dict()
    payload["saved_at"] = datetime.now().isoformat(timespec="seconds")
    digest = _state_hash(payload)

    target_dir.mkdir(parents=True, exist_ok=True)
    payload_path.write_text(_canonical_json(payload), encoding="utf-8")
    meta_path.write_text(
        _canonical_json(
            {
                "schema_version": AGENT_STATE_SCHEMA_VERSION,
                "saved_at": payload["saved_at"],
                "state_hash": digest,
            }
        ),
        encoding="utf-8",
    )
    return StoredAgentState(
        record=record,
        path=payload_path,
        metadata={
            "schema_version": AGENT_STATE_SCHEMA_VERSION,
            "saved_at": payload["saved_at"],
            "state_hash": digest,
        },
    )


def load_agent_state(
    payload_path: str | Path,
) -> StoredAgentState:
    """Load an agent state record (sidecar metadata required)."""
    payload_path = Path(payload_path)
    meta_path = payload_path.with_name(payload_path.stem + _META_SUFFIX)
    if not meta_path.exists():
        raise FileNotFoundError(
            f"agent-state metadata {meta_path} is missing; re-save with save_agent_state()"
        )
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    expected = str(meta.get("state_hash", ""))
    actual = _state_hash(payload)
    if actual != expected:
        raise ValueError(
            f"agent-state hash mismatch for {payload_path} "
            f"(expected {expected!r}, got {actual!r})"
        )
    record = AgentStateRecord.from_dict(payload)
    return StoredAgentState(
        record=record,
        path=payload_path,
        metadata={str(k): str(v) for k, v in meta.items()},
    )