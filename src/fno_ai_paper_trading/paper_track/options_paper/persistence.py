"""Atomic, hashed persistence for Phase 11 options paper-lifecycle records.

Reuses the repository's stable-JSON + SHA-256 sidecar barrier (``stable_dumps``
/ ``hash_bytes`` from ``paper_track.store``) so written records are tamper-
evident and restart-recoverable, mirroring the existing ``TrackStore``
conventions. Nothing here touches credentials, execution layers or the broker.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from fno_ai_paper_trading.paper_track.options_paper.models import LifecycleRecord
from fno_ai_paper_trading.paper_track.store import hash_bytes, stable_dumps

__all__ = ["OptionsPaperStore", "OptionsPaperStoreError"]


class OptionsPaperStoreError(RuntimeError):
    """Raised when a persisted record is missing, corrupt or tampered."""


class OptionsPaperStore:
    """File layout + atomic, hashed persistence for one paper account."""

    SCHEMA_VERSION = 1

    def __init__(self, base_dir: str | Path, account: str = "options-paper") -> None:
        self.root = Path(base_dir) / account
        self.records_dir = self.root / "records"
        self.index_path = self.root / "index.json"

    # ------------------------------------------------------------------ #
    # records
    # ------------------------------------------------------------------ #

    def save_record(self, record: LifecycleRecord) -> None:
        """Persist one record (atomic write + sha256 sidecar) and refresh index."""
        self._write_atomic(self._path(record.record_id), record.to_dict())
        self._update_index()

    def load_verified(self, record_id: str) -> LifecycleRecord | None:
        payload = self._read_verified(self._path(record_id))
        if payload is None:
            return None
        return LifecycleRecord.from_dict(payload)

    def load_all(self) -> tuple[LifecycleRecord, ...]:
        records: list[LifecycleRecord] = []
        for record_id in self.index():
            record = self.load_verified(record_id)
            if record is not None:
                records.append(record)
        return tuple(records)

    def index(self) -> list[str]:
        payload = self._read_verified(self.index_path)
        if payload is None:
            return []
        raw = payload.get("records")
        if not isinstance(raw, list):
            raise OptionsPaperStoreError("corrupt index: 'records' is not a list")
        return [item for item in raw if isinstance(item, str)]

    # ------------------------------------------------------------------ #
    # internals
    # ------------------------------------------------------------------ #

    def _path(self, record_id: str) -> Path:
        return self.records_dir / f"{record_id}.json"

    def _sha256_path(self, path: Path) -> Path:
        return path.with_name(path.name + ".sha256")

    def _write_atomic(self, path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        text = stable_dumps(payload)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
        digest = hash_bytes(path.read_bytes())
        self._sha256_path(path).write_text(digest, encoding="ascii")

    def _read_verified(self, path: Path) -> dict | None:
        if not path.exists():
            return None
        sha = self._sha256_path(path)
        if not sha.exists():
            raise OptionsPaperStoreError(f"missing sha256 sidecar for {path.name}")
        actual = hash_bytes(path.read_bytes())
        expected = sha.read_text(encoding="ascii").strip()
        if actual != expected:
            raise OptionsPaperStoreError(
                f"sha256 mismatch for {path.name}: expected {expected}, got {actual}"
            )
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise OptionsPaperStoreError(f"corrupt json in {path.name}: {exc}") from exc
        if not isinstance(payload, dict):
            raise OptionsPaperStoreError(f"unexpected payload shape in {path.name}")
        return payload

    def _update_index(self) -> None:
        record_paths = sorted(self.records_dir.glob("*.json"))
        index = {
            "schema_version": self.SCHEMA_VERSION,
            "records": [p.stem for p in record_paths],
        }
        self._write_atomic(self.index_path, index)