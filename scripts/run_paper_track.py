"""Daily Paper Trading Track entry-point.

PAPER-ONLY wrapper around the isolated track runner. Never enables any scheduler
and never submits a live order. See ``docs/paper_track_runbook.md``.
"""

from __future__ import annotations

import sys

from fno_ai_paper_trading.paper_track.runner import main

if __name__ == "__main__":
    if sys.argv[1:2] == ["live-daily"]:
        # Phase-5 server seam: paper-only daily warm-up.  Token stays
        # fail-closed in token_provider; it is never a CLI argument here.
        from fno_ai_paper_trading.paper_track.server_runner import cmd_live_daily

        raise SystemExit(cmd_live_daily(sys.argv[2:]))
    raise SystemExit(main(sys.argv[1:]))