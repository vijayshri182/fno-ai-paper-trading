"""Secret-free run audit for the live execution test (WS 7.9).

Every stage of a run is recorded as one JSON line (JSONL). The sink applies a
defensive :func:`redact` to every string field so an access token or any
long-hex secret can never be persisted by accident; the manager additionally
never hands tokens to the audit in the first place.

The default sink writes under ``reports/execution/audit/`` (git-ignored, like
the rest of ``reports/``). An in-memory sink is available for tests.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Callable, Mapping

from fno_ai_paper_trading.utils.functions import new_id

#: Long hex strings (tokens have far more entropy than order ids/heartbeats) and
#: the symmetric Bearer header are treated as secrets.
_TOKEN_RE = re.compile(r"\b[0-9a-fA-F]{40,}\b")
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[^\s]+")


def redact(text: str) -> str:
    """Replace probable secrets in ``text`` with a placeholder."""
    if not isinstance(text, str):
        return text
    out = _TOKEN_RE.sub("<redacted>", text)
    out = _BEARER_RE.sub("Bearer <redacted>", out)
    return out


class ExecutionAudit:
    """Append-only, redacted audit trail for one execution-test run."""

    def __init__(
        self,
        run_id: str,
        *,
        sink: Callable[[Mapping[str, object]], None] | None = None,
        path: str | Path | None = None,
    ) -> None:
        if not (run_id or "").strip():
            raise ValueError("audit requires a run_id")
        self.run_id = run_id
        if sink is not None:
            self._sink = sink
        elif path is not None:
            path = Path(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            handle = path.open("a", encoding="utf-8")
            self._file = handle
            self._sink = _file_sink(handle)
        else:
            self._sink = None

    def record(self, kind: str, **fields: object) -> dict[str, object]:
        """Write one redacted audit entry and return the serialized payload."""
        payload: dict[str, object] = {
            "run_id": self.run_id,
            "kind": str(kind),
        }
        for key, value in fields.items():
            if isinstance(value, str):
                payload[key] = redact(value)
            elif isinstance(value, (list, tuple)):
                payload[key] = [redact(v) if isinstance(v, str) else v for v in value]
            elif isinstance(value, Mapping):
                payload[key] = {
                    k: redact(v) if isinstance(v, str) else v for k, v in value.items()
                }
            else:
                payload[key] = value
        if self._sink is not None:
            self._sink(payload)
        return payload

    def close(self) -> None:
        file_handle = getattr(self, "_file", None)
        if file_handle is not None:
            try:
                file_handle.close()
            except OSError:
                pass


def _file_sink(handle) -> Callable[[Mapping[str, object]], None]:
    def _emit(payload: Mapping[str, object]) -> None:
        handle.write(json.dumps(payload, sort_keys=True, default=str) + "\n")

    return _emit


def new_run_id() -> str:
    """A fresh, unique run identifier for an execution test."""
    return new_id("LIVETEST")