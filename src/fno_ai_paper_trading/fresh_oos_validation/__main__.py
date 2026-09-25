"""Entry point: ``python -m fno_ai_paper_trading.fresh_oos_validation``."""
from __future__ import annotations

from fno_ai_paper_trading.fresh_oos_validation.cli import main as _cli_main

if __name__ == "__main__":
    raise SystemExit(_cli_main())