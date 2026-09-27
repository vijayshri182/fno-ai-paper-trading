"""Minimal production-safe scheduler for the fresh-OOS collector.

No external scheduler (Celery / APScheduler / cron) is used: the project has
none, and the smallest safe option is a plain stdlib loop that calls
:meth:`FreshOosCollector.collect_once` on an interval. Overlap protection
(`collector lock`), catch-up of missed dates and restart safety are all inside
the collector, so this loop only needs to be scheduled.

Two operation modes:

* ``run(once=True)`` -- a single pass (equivalent to ``collect --once``), used
  by Windows Task Scheduler / cron on the server; and
* ``run(once=False)`` -- a resident loop guarded against overlapping runs
  (``FNO_FRESH_OOS_SCHEDULE_SECONDS``, default 900s).

Market-session gate: the scheduled entry point (``main``) always evaluates the
NIFTY session gate (:mod:`fno_ai_paper_trading.fresh_oos.session_hours`) before
calling the collector. Outside the Monday-Friday ``[09:15, 15:30)`` IST session
the pass is a clean SKIP (exit 0, no lock, no network, no manifest write) and a
machine-readable reason is printed (``WEEKEND`` /
``OUTSIDE_NIFTY_MARKET_HOURS``). The bare-loop class (used by tests/embeds) is
not gated unless ``session_gate``/``gate_now_fn`` are supplied; direct/manual
collection via ``collect --once`` is never gated.

The scheduler NEVER triggers validation or tuning; it only acquires data.

Operational hardening:
* transient source failures (``RATE_LIMITED`` / ``NETWORK_ERROR`` /
  ``SOURCE_ERROR``) are retried in-pass with bounded exponential backoff;
* persistent failures are escalated: ``max_consecutive_failures`` failing
  passes in a row halt the resident loop with an explicit ``STALLED`` status
  (operator intervention) instead of endless silent retrying;
* every pass writes a machine-readable run-status artifact
  (``reports/algorithm_state/fresh_oos_scheduler.json``) so the dashboard can
  observe scheduler health without parsing stdout.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

_REPO = Path(__file__).resolve().parents[3]
if str(_REPO / "src") not in sys.path:
    sys.path.insert(0, str(_REPO / "src"))

from fno_ai_paper_trading.fresh_oos import factory  # noqa: E402
from fno_ai_paper_trading.fresh_oos.collector import FreshOosCollector  # noqa: E402
from fno_ai_paper_trading.fresh_oos.protocol import (  # noqa: E402
    STATUS_LOCKED,
    STATUS_NETWORK_ERROR,
    STATUS_RATE_LIMITED,
    STATUS_SOURCE_ERROR,
    RunOutcome,
)
from fno_ai_paper_trading.fresh_oos.session_hours import (  # noqa: E402
    SessionResult,
    evaluate_session,
    market_now,
)

# Statuses that are clearly transient provider/source failures: an in-pass
# bounded retry may recover them without operator action. Everything else is
# either a clean outcome, a data-integrity/protocol outcome (never retried —
# the collector already refuses it) or an overlap.
RETRYABLE_RUN_STATUSES = frozenset(
    {STATUS_RATE_LIMITED, STATUS_NETWORK_ERROR, STATUS_SOURCE_ERROR}
)

# Machine-readable run-status artifact (mirrors the paper-agent heartbeat):
# the only always-on place an operator/dashboard observes scheduler health.
DEFAULT_STATUS_PATH = _REPO / "reports" / "algorithm_state" / "fresh_oos_scheduler.json"


def default_interval_seconds() -> int:
    try:
        return max(30, int(os.getenv("FNO_FRESH_OOS_SCHEDULE_SECONDS", "900")))
    except ValueError:
        return 900


class FreshOosScheduler:
    """Guardable loop around the lock-protected collector.

    When ``session_gate`` is provided the gate is evaluated before every pass;
    a CLOSED session is skipped cleanly (no collector call, no lock, no network
    request, no manifest change). Without it the loop runs every pass, which
    keeps direct/embedded use deterministic and keeps manual collection
    available. The production entry point (:func:`main`) always enables the
    gate via :func:`evaluate_session`/:func:`market_now`.
    """

    def __init__(
        self,
        collector: FreshOosCollector,
        *,
        interval_seconds: int = 900,
        sleep: Callable[[float], None] = time.sleep,
        session_gate: Callable[[datetime], SessionResult] | None = None,
        gate_now_fn: Callable[[], datetime] = market_now,
        status_path: Path | None = None,
        retry_attempts: int = 3,
        retry_delay: float = 1.0,
        retry_backoff: float = 2.0,
        retry_max_delay: float = 30.0,
        max_consecutive_failures: int = 5,
    ) -> None:
        self.collector = collector
        self.interval_seconds = max(1, int(interval_seconds))
        self._sleep = sleep
        self._gate_enabled = session_gate is not None
        self._session_gate = session_gate or evaluate_session
        self._gate_now_fn = gate_now_fn or market_now
        self._stop = False
        self.status_path = Path(status_path) if status_path is not None else None
        self.retry_attempts = max(1, int(retry_attempts))
        self.retry_delay = max(0.0, float(retry_delay))
        self.retry_backoff = max(1.0, float(retry_backoff))
        self.retry_max_delay = max(0.0, float(retry_max_delay))
        self.max_consecutive_failures = max(1, int(max_consecutive_failures))
        self._consecutive_failures = 0

    def stop(self) -> None:
        """Request a clean stop after the current pass (SIGTERM hook)."""
        self._stop = True

    def _skip_pass(self, gate: SessionResult) -> None:
        print(
            f"[scheduler] schedule skip -> status=SKIPPED phase={gate.phase} "
            f"reason={gate.reason} "
            f"(observed {gate.observed_at:%Y-%m-%d %H:%M:%S} IST; market session "
            f"CLOSED; no network request made)"
        )

    def _collect_with_bounded_retry(self) -> RunOutcome:
        """One scheduled pass: transient source failures are retried in-cycle.

        Retries are bounded and back off exponentially; a pass only fails after
        every attempt has failed with a transient status. Locked, integrity and
        protocol outcomes are never retried.
        """
        attempts = self.retry_attempts
        wait = self.retry_delay
        while True:
            outcome = self.collector.collect_once()
            if outcome.status not in RETRYABLE_RUN_STATUSES or attempts <= 1:
                return outcome
            attempts -= 1
            self._sleep(max(0.0, wait))
            wait = min(wait * self.retry_backoff, self.retry_max_delay)

    def _pass_status(self, outcome: RunOutcome) -> str:
        if outcome.status is STATUS_LOCKED:
            return "LOCKED"
        return "OK" if outcome.ok else "FAILED"

    def _write_status(
        self, *, pass_status: str, outcome: RunOutcome | None, run_id: str
    ) -> None:
        """Persist a machine-readable scheduler run-status artifact (best effort)."""
        if self.status_path is None:
            return
        try:
            self.status_path.parent.mkdir(parents=True, exist_ok=True)
            payload: dict[str, object] = {
                "schema_version": 1,
                "run_id": run_id,
                "status": pass_status,
                "consecutive_failures": self._consecutive_failures,
                "max_consecutive_failures": self.max_consecutive_failures,
                "passed_at": datetime.now().isoformat(timespec="seconds"),
            }
            if outcome is not None:
                payload.update({
                    "pass_status": outcome.status,
                    "accepted": list(outcome.accepted),
                    "noop": list(outcome.noop),
                    "conflicts": list(outcome.conflicts),
                    "errors": dict(outcome.errors),
                    "message": outcome.message,
                })
            payload["escalated"] = pass_status == "STALLED"
            self.status_path.write_text(
                json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
            )
        except OSError as exc:
            print(f"[scheduler] WARNING: could not write run-status artifact: {exc}")

    def run(self, *, once: bool = False) -> int:
        while True:
            if self._gate_enabled:
                gate = self._session_gate(self._gate_now_fn())
                if not gate.is_open:
                    self._skip_pass(gate)
                    self._write_status(pass_status="SKIPPED", outcome=None, run_id="-")
                    if once or self._stop:
                        return 0
                    self._sleep(self.interval_seconds)
                    continue
            outcome = self._collect_with_bounded_retry()
            print(
                f"[{outcome.run_id}] schedule pass -> status={outcome.status} "
                f"accepted={len(outcome.accepted)} noop={len(outcome.noop)} "
                f"conflicts={len(outcome.conflicts)}"
            )
            if outcome.status == STATUS_LOCKED:
                print("  overlap detected; another pass is active -- skipping this cycle")

            if outcome.ok:
                self._consecutive_failures = 0
            elif outcome.status is not STATUS_LOCKED:
                self._consecutive_failures += 1

            pass_status = self._pass_status(outcome)
            self._write_status(
                pass_status=pass_status, outcome=outcome, run_id=outcome.run_id
            )

            escalated = (
                not once
                and not outcome.ok
                and outcome.status is not STATUS_LOCKED
                and self._consecutive_failures >= self.max_consecutive_failures
            )
            if escalated:
                self._write_status(
                    pass_status="STALLED", outcome=outcome, run_id=outcome.run_id
                )
                print(
                    f"[scheduler] STALLED after {self._consecutive_failures} "
                    f"consecutive failed passes (>= {self.max_consecutive_failures}); "
                    f"halting the loop for operator intervention"
                )
                return 2

            if once or self._stop:
                return 0 if outcome.ok else 1
            self._sleep(self.interval_seconds)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fresh-OOS collector scheduler (never validates; never orders)."
    )
    parser.add_argument("--once", action="store_true", help="run one pass and exit (for Task Scheduler/cron)")
    parser.add_argument("--interval", type=int, default=None,
                        help="loop interval seconds (default FNO_FRESH_OOS_SCHEDULE_SECONDS=900)")
    parser.add_argument("--root", help="immutable fresh-OOS store root")
    parser.add_argument("--datasets-dir", help="established datasets/ directory")
    args = parser.parse_args(argv)

    collector = factory.build_collector(root=args.root, datasets_dir=args.datasets_dir)
    interval = args.interval if args.interval is not None else default_interval_seconds()
    # The scheduled path is always NIFTY-session gated (no override flag): a
    # pass outside Monday-Friday [09:15, 15:30) IST is a clean SKIP with no
    # network request. Manual collection stays available via `collect --once`.
    scheduler = FreshOosScheduler(
        collector,
        interval_seconds=interval,
        session_gate=evaluate_session,
        gate_now_fn=market_now,
        status_path=DEFAULT_STATUS_PATH,
    )

    def _shutdown(_signum, _frame) -> None:
        scheduler.stop()

    try:
        signal.signal(signal.SIGTERM, _shutdown)
        signal.signal(signal.SIGINT, _shutdown)
    except (ValueError, AttributeError):  # not on the main thread / no signals
        pass

    return scheduler.run(once=args.once)


if __name__ == "__main__":
    raise SystemExit(main())