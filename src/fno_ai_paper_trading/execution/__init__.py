"""Controlled live execution integration test (WS 7.9 — execution layer).

This package is the *preparation* for the single, human-controlled Upstox live
F&O execution experiment: an explicitly-gated execution mode, a clean Upstox
execution adapter (dry-run capable), a single-entry/single-exit state machine, a
5-minute mandatory hold, position reconciliation and secret-free auditing.

It never trades by itself. The :class:`LiveExecutionTestManager` aborts before
any order write unless every preflight (signal, RiskManager, Watchdog, market
session, margin, and — for real sends — the LIVE_EXECUTION_TEST gate) approves.
No code path in this package touches ``PaperBroker`` or the paper session; a
PAPER run remains the default and is entirely unaffected.

Safety posture (mirrors PROJECT_PLAN §17p):

* ``ExecutionMode.PAPER`` is the only implicit mode.
* ``ExecutionMode.LIVE_EXECUTION_TEST`` requires an explicit human-controlled
  enablement (env flag + operator-authored consent file + env token whose
  fingerprint matches the consent file, within expiry).
* ``ExecutionMode.LIVE`` exists as a declared but **un-implementable** mode; the
  adapter refuses it outright.
"""
from fno_ai_paper_trading.execution.gate import ExecutionMode, GateDecision, LiveExecutionTestGate
from fno_ai_paper_trading.execution.manager import ExecutionTestResult, LiveExecutionTestManager
from fno_ai_paper_trading.execution.state import ExecutionTestState, ExecutionTestStateMachine

__all__ = [
    "ExecutionMode",
    "ExecutionTestResult",
    "ExecutionTestState",
    "ExecutionTestStateMachine",
    "GateDecision",
    "LiveExecutionTestGate",
    "LiveExecutionTestManager",
]