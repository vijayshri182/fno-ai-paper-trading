"""Build the algorithm research & competition scoreboard from recorded evidence.

Reads only recorded artifacts (never datasets, never protected OOS trades):

* ``reports/model_performance/candidates_eval.json``
* ``reports/model_performance/oos_confirmation.json``
* ``reports/algorithm_state/trade_ledger.json`` (cross-check sums)

and writes the machine-readable ``reports/algorithm_state/research_scoreboard.json``
consumed by the dashboard.  PAPER ONLY - nothing here trades.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from fno_ai_paper_trading.evaluation.scoreboard import (
    DEFAULT_PATHS,
    build_scoreboard,
    compact_for_state,
    write_scoreboard,
)
from fno_ai_paper_trading.strategies.registry import discover

SCOREBOARD_PATH = REPO_ROOT / "reports" / "algorithm_state" / "research_scoreboard.json"
STATE_PATH = REPO_ROOT / "docs" / "project_state.json"

_CHECKED = ("candidates_eval", "oos_confirmation", "trade_ledger")


def _check_inputs(paths: dict[str, Path]) -> None:
    missing = [k for k in _CHECKED if not paths[k].exists()]
    if missing:
        sys.exit(
            f"missing recorded artifacts: {missing}; run the WS 7.16 pipeline "
            "before building the scoreboard (never fabricate OOS evidence)."
        )


def _update_state(compact: dict) -> None:
    state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    state["algorithm_laboratory"] = compact
    state["last_activity"] = (
        f"Algorithm Research & Competition scoreboard rebuilt "
        f"(best tested present={compact['best_tested_present']})"
    )
    STATE_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def main() -> None:
    paths = {k: Path(v) for k, v in DEFAULT_PATHS.items()}
    _check_inputs(paths)
    board = build_scoreboard(discover(), paths)
    write_scoreboard(SCOREBOARD_PATH, board)
    _update_state(compact_for_state(board))
    comp = board["competition"]
    print(f"wrote {SCOREBOARD_PATH}")
    print(f"registry: {len(board['registry_catalog'])} specs across {len(board['families'])} families")
    print(f"best tested present: {comp['best_tested_present']}")
    for family, rows in comp["leaderboard"].items():
        print(f"  {family}: " + ", ".join(f"{r['strategy_id']}={r['tier']}" for r in rows))


if __name__ == "__main__":
    main()