"""Model / strategy version registry with promotion & rollback (WS 7.12).

An append-only, durable log of *who the champion is*, exactly one ACTIVE model
at a time. ``start`` declares the frozen baseline (MA(5,21)); ``promote`` makes
a new version ACTIVE and retires the previous one; ``rollback`` / ``rollback_to``
return control to a previous approved version. The log is replayed at load time,
so the registry is fully deterministic given the log — same actions, same state.

This is bookkeeping over *evidence*. Nothing here runs a strategy, touches a
broker, or changes risk controls: promotion is a data decision that later stages
(paper loop) consume as a reference. The hard safety boundary (§17f.7) is that
this module must never bypass RiskManager / sizing / stop-loss / PaperBroker /
Portfolio — and it does not import any of them.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


@dataclass(frozen=True)
class ModelVersion:
    """One champion entry: strategy, parameters, evidence, lifecycle status."""

    version_id: str
    strategy_name: str
    strategy_params: Mapping[str, Any]
    feature_version: str
    description: str
    status: str  # ACTIVE | RETIRED | ROLLED_BACK
    promoted_at: str
    evidence: Mapping[str, Any]
    retired_at: str = ""
    rollback_reason: str = ""
    rollback_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "version_id": self.version_id,
            "strategy_name": self.strategy_name,
            "strategy_params": dict(self.strategy_params),
            "feature_version": self.feature_version,
            "description": self.description,
            "status": self.status,
            "promoted_at": self.promoted_at,
            "evidence": dict(self.evidence),
            "retired_at": self.retired_at,
            "rollback_reason": self.rollback_reason,
            "rollback_at": self.rollback_at,
        }


class VersionRegistry:
    """Append-only champion registry (JSONL). Replays the log on load.

    ``directory`` mode: a single JSONL file inside ``directory``; the default
    file name is ``model_registry.jsonl``. In-memory when no path is given.
    """

    def __init__(
        self,
        path: Path | str | None = None,
        *,
        name: str = "model_registry",
    ) -> None:
        self.path: Path | None = None
        if path is not None:
            p = Path(path)
            if p.is_dir():
                p = p / f"{name}.jsonl"
            self.path = p
        self.name = name
        self._versions: dict[str, dict[str, Any]] = {}
        self._chain: list[str] = []  # chronological order of champion selection
        self._active_index: int = -1
        self._records: list[dict[str, Any]] = []
        if self.path is not None and Path(self.path).is_file():
            self.load(Path(self.path))

    # ------------------------------------------------------------------ state

    @property
    def active(self) -> ModelVersion | None:
        if not self._chain:
            return None
        return self._from_dict(self._versions[self._chain[self._active_index]])

    @property
    def versions(self) -> tuple[ModelVersion, ...]:
        return tuple(
            self._from_dict(self._versions[vid])
            for vid in self._chain
        )

    @property
    def log(self) -> tuple[Mapping[str, Any], ...]:
        return tuple(self._records)

    def get(self, version_id: str) -> ModelVersion | None:
        raw = self._versions.get(version_id)
        return self._from_dict(raw) if raw is not None else None

    @property
    def champion_chain(self) -> tuple[str, ...]:
        """Champion timeline in promotion order (oldest first)."""
        return tuple(self._chain)

    # --------------------------------------------------------------- actions

    def start(
        self,
        *,
        strategy_name: str = "moving_average_cross",
        strategy_params: Mapping[str, Any] | None = None,
        feature_version: str = "features-1",
        description: str = "",
        version_id: str | None = None,
    ) -> ModelVersion:
        """Declare the initial champion. Cannot run when the registry has state."""
        if self._chain:
            raise ValueError("registry already has a champion; cannot start twice")
        vid = version_id or "model_0"
        payload = {
            "action": "start",
            "version": {
                "version_id": vid,
                "strategy_name": strategy_name,
                "strategy_params": dict(strategy_params or {}),
                "feature_version": feature_version,
                "description": description,
                "promoted_at": _now_iso(),
                "evidence": {},
            },
        }
        self._apply(payload)
        self._persist(payload)
        return self._from_dict(self._versions[vid])

    def promote(
        self,
        *,
        strategy_name: str,
        strategy_params: Mapping[str, Any],
        feature_version: str,
        description: str,
        evidence: Mapping[str, Any],
        version_id: str | None = None,
    ) -> ModelVersion:
        """Promote a new champion; the previous one is retired."""
        if not self._chain:
            raise ValueError("cannot promote before the baseline is started")
        if version_id is None:
            highest = 0
            for vid in self._chain:
                if vid.startswith("model_"):
                    try:
                        highest = max(highest, int(vid.split("_", 1)[1]))
                    except ValueError:
                        pass
            version_id = f"model_{highest + 1}"
        payload = {
            "action": "promote",
            "version": {
                "version_id": version_id,
                "strategy_name": strategy_name,
                "strategy_params": dict(strategy_params),
                "feature_version": feature_version,
                "description": description,
                "promoted_at": _now_iso(),
                "evidence": dict(evidence),
            },
        }
        self._apply(payload)
        self._persist(payload)
        return self._from_dict(self._versions[version_id])

    def rollback(self, *, reason: str) -> ModelVersion:
        """Roll the current champion back to the last previous champion."""
        if len(self._chain) < 2 or self._active_index < 1:
            raise ValueError("cannot rollback the baseline champion")
        current = self._chain[self._active_index]
        payload = {
            "action": "rollback",
            "version_id": current,
            "reason": reason,
            "at": _now_iso(),
        }
        self._apply(payload)
        self._persist(payload)
        return self.active  # type: ignore[return-value]

    def rollback_to(self, version_id: str, *, reason: str) -> ModelVersion:
        """Roll back to a specific earlier champion in the chain."""
        if version_id not in self._versions:
            raise ValueError(f"unknown version {version_id!r}")
        index = self._chain.index(version_id) if version_id in self._chain else -1
        if index < 0 or index >= self._active_index:
            raise ValueError(
                f"cannot roll back to {version_id!r}: not an earlier champion"
            )
        payload = {
            "action": "rollback_to",
            "version_id": version_id,
            "reason": reason,
            "at": _now_iso(),
        }
        self._apply(payload)
        self._persist(payload)
        return self.active  # type: ignore[return-value]

    # -------------------------------------------------------------- internals

    @staticmethod
    def _from_dict(raw: dict[str, Any]) -> ModelVersion:
        return ModelVersion(**raw)

    def _apply(self, payload: Mapping[str, Any]) -> None:
        action = payload.get("action")
        raw = dict(payload)
        self._records.append(raw)
        if action == "start":
            version = dict(payload["version"])
            vid = version["version_id"]
            if vid in self._versions:
                raise ValueError(f"duplicate version_id {vid!r} in start")
            version["status"] = "ACTIVE"
            version.setdefault("retired_at", "")
            version.setdefault("rollback_reason", "")
            version.setdefault("rollback_at", "")
            self._versions[vid] = version
            self._chain.append(vid)
            self._active_index = len(self._chain) - 1
            return
        if action == "promote":
            version = dict(payload["version"])
            vid = version["version_id"]
            if vid in self._versions:
                raise ValueError(f"duplicate version_id {vid!r} in promote")
            current = self._versions[self._chain[self._active_index]]
            current["status"] = "RETIRED"
            current["retired_at"] = version["promoted_at"]
            version["status"] = "ACTIVE"
            version.setdefault("retired_at", "")
            version["rollback_reason"] = ""
            version["rollback_at"] = ""
            self._versions[vid] = version
            self._chain.append(vid)
            self._active_index = len(self._chain) - 1
            return
        if action == "rollback":
            vid = payload.get("version_id")
            if vid is None or vid not in self._versions:
                raise ValueError(f"rollback references unknown version {vid!r}")
            current = self._versions[self._chain[self._active_index]]
            if current["version_id"] != vid:
                raise ValueError("rollback version is not the active champion")
            self._active_index -= 1
            self._versions[vid]["status"] = "ROLLED_BACK"
            self._versions[vid]["rollback_reason"] = str(payload.get("reason", ""))
            self._versions[vid]["rollback_at"] = str(payload.get("at", ""))
            restored = self._versions[self._chain[self._active_index]]
            restored["status"] = "ACTIVE"
            return
        if action == "rollback_to":
            vid = payload.get("version_id")
            if vid is None or vid not in self._versions:
                raise ValueError(f"rollback_to references unknown version {vid!r}")
            index = self._chain.index(vid)
            current = self._versions[self._chain[self._active_index]]
            self._versions[current["version_id"]]["status"] = "ROLLED_BACK"
            self._versions[current["version_id"]]["rollback_reason"] = str(
                payload.get("reason", "")
            )
            self._versions[current["version_id"]]["rollback_at"] = str(
                payload.get("at", "")
            )
            self._active_index = index
            self._versions[vid]["status"] = "ACTIVE"
            return
        raise ValueError(f"unknown registry action {action!r}")

    def _persist(self, payload: Mapping[str, Any]) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(dict(payload), sort_keys=True, default=str)
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def load(self, path: Path) -> None:
        """Read a persisted log and rebuild state deterministically."""
        self._records = []
        self._versions = {}
        self._chain = []
        self._active_index = -1
        if not path.is_file():
            self.path = path
            return
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"corrupt registry log line {number} in {path}: {exc}"
                ) from exc
            try:
                self._apply(payload)
            except (KeyError, TypeError, ValueError) as exc:
                if isinstance(exc, ValueError) and "corrupt" in str(exc):
                    raise
                raise ValueError(
                    f"corrupt registry log line {number} in {path}: {exc}"
                ) from exc
        self.path = path


DEFAULT_MODEL_REGISTRY_DIR = "model_registry"