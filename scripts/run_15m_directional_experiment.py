"""15M_DIRECTIONAL_OPTIONS_EXPERIMENT - isolated paper-only CLI entry point.

Paper-only: live_trading is never enabled; the only broker reachable is
PaperBroker (is_live=False). No scheduler, no token/credential pathway.
"""

from __future__ import annotations

from fno_ai_paper_trading.experiments.directional_15m.runner import main

if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())