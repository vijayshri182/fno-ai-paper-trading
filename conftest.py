"""Root conftest — puts ``src/`` on sys.path so ``from fno_ai_paper_trading...`` works."""
from __future__ import annotations

import sys
from pathlib import Path

# Works when invoked via: pytest (repo root), python -m pytest, or IDE runners.
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))