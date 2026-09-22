"""Phase 7 — cumulative (multi-day) account reporting.

Every persisted day report embeds CUMULATIVE accounting figures (the account
ledger never resets across resumes).  ``build_cumulative_report`` re-derives a
per-day attribution table and reconciles the whole lifetime from the persisted,
hash-verified files alone.

Covered contract:

* one cumulative row per persisted day, with the day's OWN net/commission and
  count deltas (starts/quantity/notional are the cumulative ledger's);
* lifetime totals never reset and equal the last line's cumulative totals;
* reconciliation: daily deltas sum to lifetime, final cash = start + net, every
  day paper-only/accounting-clean;
* stable fingerprint across rebuilds; torn/missing state aborts the report.
"""
from __future__ import annotations

from datetime import date, time
from decimal import Decimal
from pathlib import Path

import pytest

from fno_ai_paper_trading.models.enums import Signal
from fno_ai_paper_trading.paper_track.engine import TrackConfig
from fno_ai_paper_trading.paper_track.errors import TrackCheckpointError
from fno_ai_paper_trading.paper_track.report import (
    assert_report_clean,
    build_cumulative_report,
    cumulative_fingerprint,
    reported_days,
)
from fno_ai_paper_trading.paper_track.runner import run_sessions
from fno_ai_paper_trading.paper_track.store import TrackStore
from fno_ai_paper_trading.strategies.base import SignalResult, Strategy
from tests.paper_track_testkit import closes_feed

DAY1 = date(2026, 9, 21)
DAY2 = date(2026, 9, 22)
DAY3 = date(2026, 9, 23)


class PlanStrategy(Strategy):
    name = "plan"

    def __init__(self, buy: time | None = None, sell: time | None = None) -> None:
        self.buy = buy
        self.sell = sell

    def analyze(self, bars):
        last = bars[-1]
        moment = last.timestamp.time()
        if self.buy is not None and moment == self.buy:
            return SignalResult(Signal.BUY, last.instrument, last.timestamp, "plan buy")
        if self.sell is not None and moment == self.sell:
            return SignalResult(Signal.SELL, last.instrument, last.timestamp, "plan sell")
        return SignalResult(Signal.HOLD, last.instrument, last.timestamp, "plan hold")


def _closes(day: date, path: list[int]) -> "object":
    return closes_feed(path, day=day)


def _three_day_account(tmp_path, account="cum"):
    """Three sessions on one persistent account: up, down, up-again."""
    strategy = TrackConfig(
        account=account, store_dir=Path(tmp_path),
        strategy=PlanStrategy(buy=time(10, 0), sell=time(12, 0)),
    )
    store = TrackStore(Path(tmp_path), account)
    feeds = {
        DAY1: _closes(DAY1, [25000 + 40 * i for i in range(75)]),
        DAY2: _closes(DAY2, [26000 - 30 * i for i in range(75)]),
        DAY3: _closes(DAY3, [25000 + 20 * i for i in range(75)]),
    }
    engine = None
    for i, day in enumerate((DAY1, DAY2, DAY3)):
        engine = run_sessions(
            config=strategy, store=store, feed=feeds[day], days=[day],
            resume=(i > 0), run_id=f"run-{day}",
        )
    return engine, store


def test_report_exists_for_every_day(tmp_path):
    _, store = _three_day_account(tmp_path)
    assert reported_days(store) == [DAY1, DAY2, DAY3]
    for day in (DAY1, DAY2, DAY3):
        assert store.load_report(day) is not None


def test_cumulative_report_reconciles(tmp_path):
    engine, store = _three_day_account(tmp_path)
    cum = build_cumulative_report(store)
    assert cum["reconciled"] is True, cum["violations"]
    assert cum["days"] == 3
    assert cum["start_cash"] == "100000"
    assert cum["final_cash"] == str(engine.portfolio.cash)
    assert Decimal(cum["lifetime"]["net_pnl"]) == Decimal(cum["final_cash"]) - Decimal("100000")
    assert cum["cumulative"]["fills"] % 2 == 0
    assert assert_report_clean(cum) == []


def test_per_day_deltas_sum_to_lifetime(tmp_path):
    _, store = _three_day_account(tmp_path)
    cum = build_cumulative_report(store)
    rows = cum["daily"]
    assert cum["days"] == len(rows)
    day_nets = [Decimal(r["day_net_pnl"]) for r in rows]
    day_costs = [Decimal(r["day_commission"]) for r in rows]
    day_fills = [r["fills"] for r in rows]
    assert sum(day_nets, Decimal("0")) == Decimal(cum["lifetime"]["net_pnl"])
    assert sum(day_costs, Decimal("0")) == Decimal(cum["lifetime"]["costs"])
    assert sum(day_fills) == cum["cumulative"]["fills"]
    assert all(r["eod_status"] in ("FLAT", "FLATTENED") for r in rows)
    assert all(r["paper_only"] for r in rows)
    assert all(r["accounting_clean"] for r in rows)


def test_capital_and_equity_never_reset_across_days(tmp_path):
    _, store = _three_day_account(tmp_path)
    cum = build_cumulative_report(store)
    rows = cum["daily"]
    # the account trades every day, so the closing equity must keep moving
    # instead of snapping back to the opening deposit
    assert Decimal(rows[0]["cash_eod"]) != Decimal("100000")
    assert Decimal(rows[1]["cash_eod"]) != Decimal("100000")
    assert Decimal(rows[2]["cash_eod"]) != Decimal("100000")
    # and the deposit base itself is preserved in the cumulative view
    assert Decimal(cum["start_cash"]) == Decimal("100000")
    # per-line closing cash == start + cumulative net up to and including that day
    running = Decimal("0")
    for row in rows:
        running += Decimal(row["day_net_pnl"])
        assert Decimal(row["cash_eod"]) == Decimal("100000") + running


def test_stable_fingerprint_across_rebuilds(tmp_path):
    _, store = _three_day_account(tmp_path)
    a = build_cumulative_report(store)
    b = build_cumulative_report(store)
    assert a["fingerprint"] == b["fingerprint"]
    assert cumulative_fingerprint(a["daily"]) == a["fingerprint"]


def test_corrupted_last_day_aborts_cumulative_report(tmp_path):
    _, store = _three_day_account(tmp_path)
    path = store.report_path(DAY2)
    path.write_text("{corrupt", encoding="utf-8")
    with pytest.raises(TrackCheckpointError):
        build_cumulative_report(store)


def test_no_reports_raises(tmp_path):
    store = TrackStore(Path(tmp_path), "empty")
    with pytest.raises(ValueError, match="no persisted reports"):
        build_cumulative_report(store)