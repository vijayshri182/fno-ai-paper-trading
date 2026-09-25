"""Immutable run/session storage under runs/.

Every execution receives a unique ``run_id`` (research/discovery) or
``session_id`` (paper trading). Artifacts are written once; duplicate
write attempts raise ``FileExistsError`` to prevent silent data loss.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fno_ai_paper_trading.observability.hashes import sha256_file, sha256_canonical


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def write_json(path: Path, data: dict[str, Any], *, overwrite: bool = False) -> None:
    _ensure_parent(path)
    if path.exists() and not overwrite:
        raise FileExistsError(f"artifact exists and overwrite=False: {path}")
    path.write_text(json.dumps(data, indent=2, sort_keys=True, default=str), encoding="utf-8")


def write_text(path: Path, content: str, *, overwrite: bool = False) -> None:
    _ensure_parent(path)
    if path.exists() and not overwrite:
        raise FileExistsError(f"artifact exists and overwrite=False: {path}")
    path.write_text(content, encoding="utf-8")


def write_lines(path: Path, lines: list[str], *, overwrite: bool = False) -> None:
    _ensure_parent(path)
    if path.exists() and not overwrite:
        raise FileExistsError(f"artifact exists and overwrite=False: {path}")
    path.write_text("\n".join(lines), encoding="utf-8")


def write_run_manifest(base: Path, manifest: dict[str, Any], *, overwrite: bool = False) -> Path:
    path = base / "manifest.json"
    write_json(path, manifest, overwrite=overwrite)
    return path


def write_run_config(base: Path, config: dict[str, Any], *, overwrite: bool = False) -> Path:
    path = base / "config.json"
    write_json(path, config, overwrite=overwrite)
    return path


def write_run_candidates(base: Path, candidates: list[dict[str, Any]], *, overwrite: bool = False) -> Path:
    path = base / "candidates.json"
    write_json(path, candidates, overwrite=overwrite)
    return path


def write_run_decisions(base: Path, decisions: list[dict[str, Any]], *, overwrite: bool = False) -> Path:
    path = base / "decisions.jsonl"
    write_lines(path, [json.dumps(d, default=str) for d in decisions], overwrite=overwrite)
    return path


def write_run_results(base: Path, results: dict[str, Any], *, overwrite: bool = False) -> Path:
    path = base / "results.json"
    write_json(path, results, overwrite=overwrite)
    return path


def write_run_provenance(base: Path, provenance: dict[str, Any], *, overwrite: bool = False) -> Path:
    path = base / "provenance.json"
    write_json(path, provenance, overwrite=overwrite)
    return path


def create_run_base(root: Path, run_type: str, run_id: str) -> Path:
    base = root / run_type / run_id
    base.mkdir(parents=True, exist_ok=True)
    return base


def file_hashes(base: Path) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for p in sorted(base.iterdir()):
        if p.is_file():
            hashes[p.name] = sha256_file(p)
    return hashes
