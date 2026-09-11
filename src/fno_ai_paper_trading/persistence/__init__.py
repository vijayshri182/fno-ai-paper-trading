"""State persistence and recovery for deterministic paper sessions (WS 6.5).

``save_session`` / ``load_session`` follow the same local two-file pattern as
``data.dataset_store`` (payload + sidecar ``.meta.json`` with a deterministic
``state_hash``) so a running paper session can be checkpointed to disk and
restored into a fresh runtime after a restart, without a database and without
any ``backtest.*`` coupling.
"""
from fno_ai_paper_trading.persistence.session_store import (
    SCHEMA_VERSION,
    DEFAULT_STATE_DIR,
    SessionSnapshot,
    StoredSession,
    load_session,
    save_session,
)

__all__ = [
    "SCHEMA_VERSION",
    "DEFAULT_STATE_DIR",
    "SessionSnapshot",
    "StoredSession",
    "load_session",
    "save_session",
]