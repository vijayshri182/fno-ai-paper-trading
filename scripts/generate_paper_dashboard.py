"""Generate the Paper Trading Transparency Dashboard from persisted state.

Reads only persisted artifacts (trade ledger, session snapshots, discovery
report, agent heartbeat) under ``--reports-root`` / ``--paper-state-dir`` and
emits a standalone, dependency-free HTML dashboard plus an audit-ready JSON.

Purely offline: no network, no orders, no secrets. Deterministic for a fixed set
of inputs. If a metric cannot be derived from persisted data it is rendered as
NOT AVAILABLE — never invented.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from fno_ai_paper_trading.reporting import build_dashboard, gather_sources, write_dashboard  # noqa: E402

DEFAULT_HTML = REPO_ROOT / "docs" / "paper_trading_dashboard.html"
DEFAULT_JSON = REPO_ROOT / "reports" / "algorithm_state" / "paper_dashboard.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="generate_paper_dashboard", description=__doc__)
    parser.add_argument("--reports-root", default=str(REPO_ROOT / "reports"))
    parser.add_argument("--paper-state-dir", default=str(REPO_ROOT / "paper_state"))
    parser.add_argument("--discovery-json", default="",
                        help="explicit discovery report JSON (default: newest reports/discovery_cycle_*.json)")
    parser.add_argument("--html", default=str(DEFAULT_HTML))
    parser.add_argument("--json", default=str(DEFAULT_JSON))
    parser.add_argument("--initial-capital", default="",
                        help="explicit starting capital (default: session snapshot / settings)")
    args = parser.parse_args(argv)

    sources = gather_sources(
        Path(args.reports_root),
        Path(args.paper_state_dir) if args.paper_state_dir else None,
        Path(args.discovery_json) if args.discovery_json else None,
    )
    initial_capital = Decimal(args.initial_capital) if args.initial_capital else None
    dashboard = build_dashboard(sources, generated_at=datetime.now(timezone.utc), initial_capital=initial_capital)

    written = write_dashboard(dashboard, args.html, args.json)
    print(json.dumps({
        "run_id": dashboard.run_id,
        "reconciliation": dashboard.reconciliation.status,
        "algo_ready": dashboard.sections.get("17_system_health", {}).get("algo_ready", "NO"),
        "live_trading": dashboard.sections.get("17_system_health", {}).get("live_trading", "CLOSED"),
        "paper_trades": dashboard.sections.get("4_trade_ledger", {}).get("count", 0),
        "written": {k: str(v) for k, v in written.items()},
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())