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
"""
from __future__ import annotations

import argparse
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
from fno_ai_paper_trading.fresh_oos.protocol import STATUS_LOCKED  # noqa: E402
from fno_ai_paper_trading.fresh_oos.session_hours import (  # noqa: E402
    SessionResult,
    evaluate_session,
    market_now,
)


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
    ) -> None:
        self.collector = collector
        self.interval_seconds = max(1, int(interval_seconds))
        self._sleep = sleep
        self._gate_enabled = session_gate is not None
        self._session_gate = session_gate or evaluate_session
        self._gate_now_fn = gate_now_fn or market_now
        self._stop = False

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

    def run(self, *, once: bool = False) -> int:
        while True:
            if self._gate_enabled:
                gate = self._session_gate(self._gate_now_fn())
                if not gate.is_open:
                    self._skip_pass(gate)
                    if once or self._stop:
                        return 0
                    self._sleep(self.interval_seconds)
                    continue
            outcome = self.collector.collect_once()
            print(
                f"[{outcome.run_id}] schedule pass -> status={outcome.status} "
                f"accepted={len(outcome.accepted)} noop={len(outcome.noop)} "
                f"conflicts={len(outcome.conflicts)}"
            )
            if outcome.status == STATUS_LOCKED:
                print("  overlap detected; another pass is active -- skipping this cycle")
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