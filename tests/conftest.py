"""Isolated test config — puts ``tests/`` on sys.path for the vNext suite helpers.

Does not modify the root ``conftest.py``. Imported only by the isolated vNext
test modules.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))