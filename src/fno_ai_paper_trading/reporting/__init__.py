"""Reusable report-generation layer.

The paper-trading dashboard + discovery research report are produced from
persisted state only (no fabricated figures), so every session can regenerate
them deterministically.
"""

from fno_ai_paper_trading.reporting.paper_dashboard import (  # noqa: F401
    NO_PAPER_TRADES,
    NOT_AVAILABLE,
    OPTION_DATA_UNAVAILABLE,
    OPTION_PROFIT_NOT_PROVEN,
    Reconciliation,
    PaperDashboard,
    build_dashboard,
    gather_sources,
    render_html,
    trade_stats,
    write_dashboard,
)

__all__ = [
    "NO_PAPER_TRADES",
    "NOT_AVAILABLE",
    "OPTION_DATA_UNAVAILABLE",
    "OPTION_PROFIT_NOT_PROVEN",
    "Reconciliation",
    "PaperDashboard",
    "build_dashboard",
    "gather_sources",
    "render_html",
    "trade_stats",
    "write_dashboard",
]