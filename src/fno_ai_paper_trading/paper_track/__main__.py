"""Allow ``python -m fno_ai_paper_trading.paper_track``."""
from __future__ import annotations

import sys

from fno_ai_paper_trading.paper_track.runner import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))