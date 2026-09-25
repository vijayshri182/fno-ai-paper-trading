"""Single-use consumption record for the fresh-OOS pool.

The collector manifest stays untouched: consumption is recorded in a dedicated
sidecar ``validation_run.json`` under the store root, written atomically.  A
successful controlled validation marks the pool consumed exactly once; any
second run refuses (the gate checks this sidecar) and the recorded outcome is
never edited in place (a re-run writes a fresh file only on refusal/no-op).
"""
from __future__ import annotations

import json
import os
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import Mapping

CONSUME_FILE_NAME = "validation_run.json"


class ConsumptionError(Exception):
    """A single-use bookkeeping failure (never auto-repaired)."""


def is_consumed(root: Path) -> bool:
    """True when a successful single-use validation has already consumed the pool."""
    path = Path(root) / CONSUME_FILE_NAME
    if not path.is_file():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ConsumptionError(f"unreadable consumption record: {path}")
    return bool(data.get("consumed"))


def new_run_id(prefix: str = "fresh_validation") -> str:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{prefix}_{stamp}_{uuid.uuid4().hex[:8]}"


def write_consumption(
    root: Path,
    *,
    run_id: str,
    outcome: Mapping[str, object],
    now_fn=None,
) -> Path:
    """Atomically record a single-use validation consumption for ``root``."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    path = root / CONSUME_FILE_NAME
    if path.is_file():
        raise ConsumptionError(
            f"single-use consumption record already exists: {path}; the pool is NOT re-runnable"
        )
    content = {
        "schema_version": 1,
        "consumed": True,
        "consumed_at": (now_fn() if now_fn else datetime.now()).isoformat(timespec="seconds"),
        "run_id": run_id,
        "outcome": dict(outcome),
    }
    fd, scratch = tempfile.mkstemp(prefix=f".{CONSUME_FILE_NAME}.", dir=str(root), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(content, sort_keys=True, indent=2, default=str) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(scratch, path)
    except BaseException:
        try:
            os.unlink(scratch)
        except OSError:
            pass
        raise
    return path


def read_consumption(root: Path) -> Mapping[str, object]:
    path = Path(root) / CONSUME_FILE_NAME
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))