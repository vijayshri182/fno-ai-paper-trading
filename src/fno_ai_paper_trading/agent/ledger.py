"""Sync closed paper round trips into the attributed paper-trade ledger.

The continuous paper agent is the only producer of
``reports/algorithm_state/paper_trades.json``, which is read by the dashboard,
:mod:`fno_ai_paper_trading.evaluation.paper_trades`, ``seed_algorithm_ledger.py``
and ``build_daily_performance.py`` but was previously written by nothing.

Round trips are paired from the session portfolio's fill history with the same
FIFO rule the research layer uses (:func:`pair_round_trips`), so the recorded
ledger numbers reconcile with the rest of the system. Recording is
deterministic and idempotent: a pair is matched by its (entry, exit) fill
timestamps and recorded once, regardless of how many times a sync runs.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from fno_ai_paper_trading.evaluation.paper_trades import (
    AttributedPaperTrade,
    load_paper_trades,
    save_paper_trades,
)
from fno_ai_paper_trading.learning.capture import pair_round_trips
from fno_ai_paper_trading.models.position import Trade

# Champion attribution default (matches scripts/build_daily_performance.py).
CHAMPION_IDENTITY: Mapping[str, str] = {
    "strategy_id": "moving_average_cross",
    "strategy_family": "TREND_FOLLOWING",
    "strategy_version": "1.0.0",
    "configuration_version": "",
}


def _fingerprint(entry: Trade, exit_: Trade) -> tuple[datetime, datetime]:
    return entry.executed_at, exit_.executed_at


def _fingerprint_from_record(record: AttributedPaperTrade) -> tuple[datetime, datetime]:
    return (
        datetime.fromisoformat(record.entry_time),
        datetime.fromisoformat(record.exit_time),
    )


def to_attributed_trade(
    entry: Trade,
    exit_: Trade,
    identity: Mapping[str, str] | None = None,
) -> AttributedPaperTrade:
    """Render a paired (entry, exit) round trip as an attributed ledger record.

    Numbers follow the research layer's round-trip convention: price P&L comes
    from the closing fill's realized P&L, commission is the sum of both legs
    and net P&L is price P&L minus commission.
    """
    attrs = CHAMPION_IDENTITY if identity is None else identity
    price_pnl = exit_.realized_pnl
    commission = entry.commission + exit_.commission
    return AttributedPaperTrade(
        strategy_id=str(attrs.get("strategy_id", CHAMPION_IDENTITY["strategy_id"])),
        strategy_family=str(attrs.get("strategy_family", CHAMPION_IDENTITY["strategy_family"])),
        strategy_version=str(attrs.get("strategy_version", CHAMPION_IDENTITY["strategy_version"])),
        configuration_version=str(
            attrs.get("configuration_version", CHAMPION_IDENTITY["configuration_version"])
        ),
        entry_time=entry.executed_at.isoformat(),
        exit_time=exit_.executed_at.isoformat(),
        side=exit_.side.value,
        entry_price=entry.price,
        exit_price=exit_.price,
        price_pnl=price_pnl,
        commission=commission,
        net_pnl=price_pnl - commission,
        signal=entry.side.value,
        regime=None,
        metadata={
            "symbol": entry.instrument.symbol,
            "quantity": entry.quantity,
            "multiplier": str(entry.instrument.multiplier),
        },
    )


def sync_paper_ledger(
    trades: Sequence[Trade],
    path: Path | str,
    identity: Mapping[str, str] | None = None,
) -> list[AttributedPaperTrade]:
    """Append closed round trips (new since last recorded) to the ledger.

    ``trades`` is the portfolio fill history (in execution order). The ledger is
    idempotent: pairs whose (entry, exit) timestamps already exist are skipped,
    so repeated syncs after crashes or restarts never duplicate a trade. The
    full updated ledger is returned (and persisted).
    """
    target = Path(path)
    existing = load_paper_trades(target)
    recorded = {_fingerprint_from_record(record) for record in existing}

    added: list[AttributedPaperTrade] = []
    paired, _open_count = pair_round_trips(list(trades))
    for entry, exit_ in paired:
        if _fingerprint(entry, exit_) in recorded:
            continue
        record = to_attributed_trade(entry, exit_, identity)
        recorded.add(_fingerprint(entry, exit_))
        added.append(record)

    if added:
        save_paper_trades(target, [*existing, *added])
    return [*existing, *added]