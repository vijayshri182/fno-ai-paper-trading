"""Walk-forward adaptive research & learning engine (WS 7.18).

Paper/historical only. The engine replays every research-domain trading day in
chronological order with a frozen per-day algorithm, accumulates only completed
champion evidence, generates fixed-parameter challengers from a reusable
catalog, validates each challenger on future days only, and promotes a new
champion version only when an explicit evidence-based gate passes. Every day
produces one immutable row of the 21-field algorithm evolution ledger.

Safety guarantees held by construction:

* the protected out-of-sample period is never loaded (:class:`WalkForwardEngine`
  raises if the domain reaches it);
* live trading is never enabled (no live-execution code is imported);
* champion and challenger parameters are never tuned (fixed catalog constants);
* history is append-only, so the day-by-day ledger can never be rewritten;
* the algorithm used on day D is decided before D is evaluated, so no future
  information can ever influence day D.
"""
from fno_ai_paper_trading.walkforward.config import (
    FRAMEWORK_VERSION,
    WalkForwardConfig,
)
from fno_ai_paper_trading.walkforward.engine import (
    DayRun,
    WalkForwardEngine,
    WalkForwardResult,
)
from fno_ai_paper_trading.walkforward.records import (
    DAILY_EVOLUTION_FIELDS,
    DailyEvolutionRecord,
    EvolutionLedger,
)

__all__ = [
    "DAILY_EVOLUTION_FIELDS",
    "DayRun",
    "DailyEvolutionRecord",
    "EvolutionLedger",
    "FRAMEWORK_VERSION",
    "WalkForwardConfig",
    "WalkForwardEngine",
    "WalkForwardResult",
]