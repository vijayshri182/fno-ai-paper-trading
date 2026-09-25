"""SQLite migration runner shared by Python ingestion and Node backend.

Migrations live under gui/backend/db/migrations/ and are numbered
<version>_<name>.sql. Each file is applied inside its own transaction and
tracked in the ``schema_migrations`` table so repeated runs are idempotent.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_MIGRATIONS_DIR = _PROJECT_ROOT / "gui" / "backend" / "db" / "migrations"

_SCHEMA_MIGRATIONS_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
  version INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  applied_at TEXT NOT NULL
);
"""


def _discover(migrations_dir: Path | None = None) -> list[tuple[int, str, Path]]:
    d = migrations_dir or _MIGRATIONS_DIR
    files = sorted(d.glob("*.sql"))
    entries: list[tuple[int, str, Path]] = []
    for f in files:
        parts = f.stem.split("_", 1)
        try:
            v = int(parts[0])
        except ValueError:
            continue
        entries.append((v, f.stem, f))
    return entries


def apply_migrations(db_path: str | Path, migrations_dir: Path | None = None) -> int:
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(_SCHEMA_MIGRATIONS_SQL)
    conn.commit()

    applied = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
    count = 0
    for version, name, path in _discover(migrations_dir):
        if version in applied:
            continue
        sql = path.read_text(encoding="utf-8")
        conn.execute("BEGIN")
        try:
            conn.executescript(sql)
            conn.execute(
                "INSERT INTO schema_migrations(version, name, applied_at) VALUES(?,?, datetime('now'))",
                (version, name),
            )
            conn.execute("COMMIT")
            count += 1
        except Exception:
            conn.execute("ROLLBACK")
            raise
    conn.close()
    return count
