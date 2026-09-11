"""Deterministic outcome classification for experience records (WS 7.9).

Classification never looks at anything other than the realized, cost-net P&L of
a completed paper trade. There is no future information and no look-ahead: the
outcome is written into the records only after the paper position is closed.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from fno_ai_paper_trading.experience.enums import OutcomeKind


def classify_outcome(realized_pnl: Decimal) -> OutcomeKind:
    """Classify a completed trade by its net realized P&L.

    ``realized_pnl`` must already be net of commission and slippage. A zero P&L
    is ``BREAKEVEN`` (a genuinely completed round trip), never ``FLAT`` — the
    record itself is what distinguishes "no trade" from "a trade with zero P&L".
    """
    if not isinstance(realized_pnl, Decimal):
        raise TypeError("realized_pnl must be a Decimal")
    if not realized_pnl.is_finite():
        raise ValueError("realized_pnl must be finite")
    if realized_pnl > 0:
        return OutcomeKind.WIN
    if realized_pnl < 0:
        return OutcomeKind.LOSS
    return OutcomeKind.BREAKEVEN


def holding_seconds(entry: datetime, exit_: datetime) -> int:
    """Whole non-negative seconds between the entry and exit timestamps.

    The domain uses naive IST timestamps throughout; mixing aware and naive
    datetimes is rejected rather than silently coerced.
    """
    if not isinstance(entry, datetime) or not isinstance(exit_, datetime):
        raise TypeError("entry and exit must be datetimes")
    if (entry.tzinfo is None) != (exit_.tzinfo is None):
        raise ValueError("entry and exit must both be aware or both be naive")
    delta = exit_ - entry
    if delta < timedelta(0):
        raise ValueError("exit timestamp must not be before entry timestamp")
    return int(delta.total_seconds())