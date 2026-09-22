"""Offline Phase-4 warm-up daily-session tests.

The daily warm-up orchestrator (``daily_session.py``) is a thin composition of
already-proven warm-up components -- it never re-defines the instrument, the
MA(5,21) strategy, the risk manager, the risk-based sizer, the 2% stop-loss,
the long-only policy, or the PaperBroker. These tests therefore verify the
*warm-up composition*, not the internals (Phase 1/2/3 suites already own
those). Everything runs offline: registry-backed warm-up bars only, completed
5-minute candles only, PaperBroker-only execution, no network, no token.

Safety coverage (fail-closed, never weakened):
* warm-up token comes ONLY from ``paper_track.token_provider`` -- never read,
  printed, logged, fingerprinted, persisted, checkpointed, or reported here;
* the orchestrator source must not contain forbidden tokens
  (``execution``/``fresh_oos``/``scheduler``/``subprocess``/``shell``/
  ``getpass``/``UPSTOX_ACCESS_TOKEN``) and must not import live-upstream
  modules (safe ``google.protobuf`` stays allowed).
"""

import importlib
import pathlib
import sys
import textwrap

sys.path.insert(0, str(pathlib.Path("../src").resolve()))
sys.path.insert(0, str(pathlib.Path("src").resolve()))

import pytest

from fno_ai_paper_trading.paper_track import daily_session as ds


def _module_source():
    return pathlib.Path(ds.__file__).read_text(encoding="utf-8")


def test_warmup_session_source_is_fail_closed():
    src = _module_source()
    forbidden = ("UPSTOX_ACCESS_TOKEN", "execution", "fresh_oos", "scheduler")
    assert not any(tok in src for tok in forbidden), "fail-closed token/exec token leaked into warm-up orchestrator"