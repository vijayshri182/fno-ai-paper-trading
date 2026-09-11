"""Session operations and monitoring for the V1 paper session (WS 6.6).

This is the operator-facing observability layer for
:class:`~fno_ai_paper_trading.services.paper_session.PaperSession`:

* :class:`SessionHealth` / :func:`health` -- a deterministic view of the live
  session's counters, cash, equity and open-position posture,
* :func:`log_results` / :func:`log_health` -- step / poll / health logging for
  the operator console (stdlib logging, no secrets, no environment values),
* :class:`SessionReport` / :func:`build_report` and
  :func:`report_from_snapshot` -- an offline-friendly, deterministic session
  report (fees, ledger, equity curve, win/loss statistics) built either from a
  live session or purely from a stored :class:`SessionSnapshot`,
* :func:`report_to_dict` / :func:`report_to_html` / :func:`write_html_report`
  -- plain-dict and labelled standalone-HTML rendering of a report, reusing the
  :mod:`~fno_ai_paper_trading.research.report` CSS and table primitives so the
  output stays visually consistent with the existing research reports.

Scheduling a running session remains an operator-level concern: the in-process
poller is ``PaperSession.run_loop`` and an OS scheduler (cron / Task Scheduler)
wraps the ``scripts/paper_session_report.py`` CLI for recurring reports. This
module performs **no** disk I/O (persistence belongs exclusively to
:mod:`fno_ai_paper_trading.persistence.session_store`) and never writes files
itself except through the explicit :func:`write_html_report` helper.

Everything here is deliberately dependency-free and deterministic when an
injected ``when`` timestamp is supplied.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Iterable, Mapping

from fno_ai_paper_trading.data.errors import MarketDataError
from fno_ai_paper_trading.models.enums import Signal
from fno_ai_paper_trading.models.order import Fill
from fno_ai_paper_trading.persistence.session_store import SessionSnapshot
from fno_ai_paper_trading.research.report import CSS, card, escape, fmt, kv_rows, table
from fno_ai_paper_trading.services.paper_session import PaperSession, SessionResult, SessionStep
from fno_ai_paper_trading.services.trading_service import OrderResult
from fno_ai_paper_trading.utils.logging import get_logger

_HEALTH_LOGGER = "session.operations"


@dataclass(frozen=True)
class SessionHealth:
    """A deterministic snapshot of the live session's operational state.

    All fields are derived from session state; nothing is mutated and no I/O
    occurs. ``equity`` is marked to market using the provider's last price
    (falling back to the position's average entry price when unavailable).
    """

    running: bool
    environment: str
    instrument: str
    interval: str
    warmup: bool
    consumed_bars: int
    orders_submitted: int
    fills: int
    trades: int
    rejections: int
    skips: int
    cash: Decimal
    equity: Decimal
    realized_pnl: Decimal
    realized_pnl_today: Decimal
    open_quantity: int


@dataclass(frozen=True)
class EquityPoint:
    """A single (time, equity) mark on the session's equity curve."""

    time: datetime
    equity: Decimal


@dataclass(frozen=True)
class SessionLedgerRow:
    """A single reportable event: a consumed candle, a fill, or an error."""

    time: datetime
    signal: Signal | None
    action: str
    price: Decimal | None
    quantity: int | None
    notional: Decimal | None
    commission: Decimal | None
    equity: Decimal | None


@dataclass(frozen=True)
class SessionReport:
    """A complete, deterministic report of a paper-session run.

    ``source`` records whether the report was built from a live session
    (``"live"``) or from a stored snapshot (``"snapshot"``). Decimal fields are
    never rounded here; rendering helpers chose their own precision.
    """

    instrument: str
    interval: str
    source: str
    data_source: str
    replay_mode: str
    live_orders: bool
    generated_at: datetime
    initial_cash: Decimal
    cash: Decimal
    equity: Decimal
    realized_pnl: Decimal
    realized_pnl_today: Decimal
    unrealized_pnl: Decimal
    open_quantity: int
    orders_submitted: int
    fills: int
    trades: int
    rejections: int
    skips: int
    consumed_bars: int
    wins: int
    losses: int
    round_trips: int
    win_rate: Decimal | None
    max_drawdown: Decimal | None
    max_drawdown_pct: Decimal | None
    return_pct: Decimal | None
    ledger: tuple[SessionLedgerRow, ...]
    equity_curve: tuple[EquityPoint, ...]


# --------------------------------------------------------------------------- #
# Health
# --------------------------------------------------------------------------- #


def _mark_prices(
    portfolio,
    *,
    mark_prices: Mapping[str, Decimal] | None = None,
    live_provider=None,
) -> dict[str, Decimal]:
    """Mark prices for every open position.

    Priority: an explicit ``mark_prices`` value, then the live provider's last
    price, then (offline) the position's average entry price as the documented
    cost-basis fallback.
    """
    prices: dict[str, Decimal] = {}
    for symbol, position in portfolio.open_positions().items():
        if mark_prices is not None and symbol in mark_prices:
            prices[symbol] = Decimal(mark_prices[symbol])
        elif live_provider is not None:
            try:
                prices[symbol] = live_provider.get_last_price(position.instrument)
            except (MarketDataError, KeyError, AttributeError):
                prices[symbol] = position.average_entry_price
        else:
            prices[symbol] = position.average_entry_price
    return prices


def _open_quantity(portfolio, symbol: str) -> int:
    position = portfolio.position_for(symbol)
    return position.quantity if position is not None else 0


def health(session: PaperSession, when: datetime | None = None) -> SessionHealth:
    """Capture the live session's operational state without mutating anything.

    ``when`` defaults to the session's injected decision clock so repeated
    health checks stay consistent with the session's own time reference.
    """
    now = when if when is not None else session.now()
    prices = _mark_prices(session.portfolio, live_provider=session.provider)
    return SessionHealth(
        running=session.is_running,
        environment=session.settings.environment.value,
        instrument=session.instrument.symbol,
        interval=session.interval_token,
        warmup=session.consumed_candles < session.warmup_bars,
        consumed_bars=session.consumed_candles,
        orders_submitted=session.orders_submitted,
        fills=session.fills,
        trades=session.trades,
        rejections=session.rejections,
        skips=session.skipped_candles,
        cash=session.portfolio.cash,
        equity=session.portfolio.total_value(prices),
        realized_pnl=session.portfolio.realized_pnl,
        realized_pnl_today=session.portfolio.realized_pnl_today(today=now.date()),
        open_quantity=_open_quantity(session.portfolio, session.instrument.symbol),
    )


# --------------------------------------------------------------------------- #
# Ledger helpers
# --------------------------------------------------------------------------- #


def _describe_order(order_result: OrderResult | None, label: str) -> str | None:
    if order_result is None or order_result.order is None:
        return None
    order = order_result.order
    side = order.side.value
    fill = order_result.fill
    if fill is not None:
        return f"{label}-{side} {fill.quantity} @ {fill.price:f}"
    reasons = list(order_result.decision.reasons) if order_result.decision is not None else []
    detail = order.rejection_reason or ("; ".join(reasons) if reasons else "no fill")
    return f"{label}-{side} rejected: {detail}"


def _step_action(step: SessionStep) -> str:
    if step.error is not None:
        return f"error: {step.error}"
    if step.skipped:
        return f"skip: {step.skip_reason}"
    parts = [
        _describe_order(step.order_result, "signal"),
        _describe_order(step.stop_result, "stop"),
    ]
    joined = " ; ".join(part for part in parts if part is not None)
    return joined if joined else "hold"


def _step_values(step: SessionStep) -> tuple[Decimal | None, int | None, Decimal | None, Decimal | None]:
    for order_result in (step.order_result, step.stop_result):
        if order_result is not None and order_result.fill is not None:
            fill = order_result.fill
            return fill.price, fill.quantity, fill.price * fill.quantity, fill.commission
    return None, None, None, None


def _row_from_step(step: SessionStep) -> SessionLedgerRow:
    price, quantity, notional, commission = _step_values(step)
    return SessionLedgerRow(
        time=step.bar.timestamp,
        signal=step.signal,
        action=_step_action(step),
        price=price,
        quantity=quantity,
        notional=notional,
        commission=commission,
        equity=step.equity,
    )


def _row_from_fill(fill: Fill) -> SessionLedgerRow:
    return SessionLedgerRow(
        time=fill.filled_at,
        signal=None,
        action=f"fill {fill.side.value} {fill.quantity} @ {fill.price:f}",
        price=fill.price,
        quantity=fill.quantity,
        notional=fill.price * fill.quantity,
        commission=fill.commission,
        equity=None,
    )


def _wins_losses(trade_history) -> tuple[int, int]:
    wins = 0
    losses = 0
    for trade in trade_history:
        if trade.realized_pnl > 0:
            wins += 1
        elif trade.realized_pnl < 0:
            losses += 1
    return wins, losses


def _win_rate(wins: int, losses: int) -> Decimal | None:
    if wins + losses <= 0:
        return None
    return Decimal(wins) / Decimal(wins + losses)


def _equity_curve_from_steps(steps: list[SessionStep]) -> tuple[EquityPoint, ...]:
    """Return presentation points after excluding warm-up's sentinel equity."""
    return tuple(
        EquityPoint(time=step.bar.timestamp, equity=step.equity)
        for step in steps
        if step.equity != 0
    )


def _return_pct(initial_cash: Decimal, equity: Decimal) -> Decimal | None:
    if initial_cash <= 0 or not initial_cash.is_finite() or not equity.is_finite():
        return None
    return (equity - initial_cash) / initial_cash * Decimal("100")


def _drawdown_metrics(
    equity_curve: tuple[EquityPoint, ...],
) -> tuple[Decimal | None, Decimal | None]:
    """Calculate drawdown only when a real, positive equity series exists."""
    if len(equity_curve) < 2 or any(
        not point.equity.is_finite() or point.equity <= 0
        for point in equity_curve
    ):
        return None, None

    peak = equity_curve[0].equity
    max_drawdown = Decimal("0")
    max_drawdown_pct = Decimal("0")
    for point in equity_curve[1:]:
        peak = max(peak, point.equity)
        drawdown = peak - point.equity
        if drawdown > max_drawdown:
            max_drawdown = drawdown
            max_drawdown_pct = drawdown / peak * Decimal("100")
    return max_drawdown, max_drawdown_pct


# --------------------------------------------------------------------------- #
# Report construction
# --------------------------------------------------------------------------- #


def build_report(
    session: PaperSession,
    results: Iterable[SessionResult] | None = None,
    *,
    when: datetime | None = None,
    mark_prices: Mapping[str, Decimal] | None = None,
    data_source: str = "Paper session data",
    replay_mode: str = "Paper session execution",
    live_orders: bool = False,
) -> SessionReport:
    """Build a report from the current session state.

    ``when`` defaults to the session's decision clock. When ``results`` (the
    output of ``session.run_loop(...)``) is supplied, the ledger and equity
    curve are taken from the consumed steps; otherwise they are derived from
    the session's recorded fills and a single terminal equity point.
    """
    now = when if when is not None else session.now()
    snap = session.snapshot()
    portfolio = snap.portfolio
    prices = _mark_prices(portfolio, mark_prices=mark_prices, live_provider=session.provider)
    equity = portfolio.total_value(prices)

    if results is not None:
        steps = [step for result in results for step in result.steps]
        ledger = tuple(_row_from_step(step) for step in steps)
        equity_curve = _equity_curve_from_steps(steps)
    else:
        ledger = tuple(_row_from_fill(fill) for fill in snap.fills)
        equity_curve = (EquityPoint(now, equity),)

    wins, losses = _wins_losses(portfolio.trade_history)
    max_drawdown, max_drawdown_pct = _drawdown_metrics(equity_curve)
    return SessionReport(
        instrument=snap.instrument.symbol,
        interval=snap.interval_token,
        source="live",
        data_source=data_source,
        replay_mode=replay_mode,
        live_orders=live_orders,
        generated_at=now,
        initial_cash=portfolio.initial_cash,
        cash=portfolio.cash,
        equity=equity,
        realized_pnl=portfolio.realized_pnl,
        realized_pnl_today=portfolio.realized_pnl_today(today=now.date()),
        unrealized_pnl=portfolio.unrealized_pnl(prices),
        open_quantity=_open_quantity(portfolio, snap.instrument.symbol),
        orders_submitted=snap.orders_submitted,
        fills=snap.fills_count,
        trades=snap.trades_count,
        rejections=snap.rejections,
        skips=snap.skips,
        consumed_bars=len(snap.consumed_timestamps),
        wins=wins,
        losses=losses,
        round_trips=wins + losses,
        win_rate=_win_rate(wins, losses),
        max_drawdown=max_drawdown,
        max_drawdown_pct=max_drawdown_pct,
        return_pct=_return_pct(portfolio.initial_cash, equity),
        ledger=ledger,
        equity_curve=equity_curve,
    )


def report_from_snapshot(
    snapshot: SessionSnapshot,
    *,
    mark_prices: Mapping[str, Decimal] | None = None,
    when: datetime | None = None,
    data_source: str = "Stored session snapshot",
    replay_mode: str = "Offline snapshot",
    live_orders: bool = False,
) -> SessionReport:
    """Build a report purely from a stored :class:`SessionSnapshot`.

    Offline-friendly: no provider is consulted. Open positions are marked at
    their average entry price (cost basis, zero unrealized P&L) unless explicit
    ``mark_prices`` are supplied.
    """
    now = when if when is not None else datetime.now()
    portfolio = snapshot.portfolio
    prices = _mark_prices(portfolio, mark_prices=mark_prices, live_provider=None)
    equity = portfolio.total_value(prices)
    wins, losses = _wins_losses(portfolio.trade_history)
    return SessionReport(
        instrument=snapshot.instrument.symbol,
        interval=snapshot.interval_token,
        source="snapshot",
        data_source=data_source,
        replay_mode=replay_mode,
        live_orders=live_orders,
        generated_at=now,
        initial_cash=portfolio.initial_cash,
        cash=portfolio.cash,
        equity=equity,
        realized_pnl=portfolio.realized_pnl,
        realized_pnl_today=portfolio.realized_pnl_today(today=now.date()),
        unrealized_pnl=portfolio.unrealized_pnl(prices),
        open_quantity=_open_quantity(portfolio, snapshot.instrument.symbol),
        orders_submitted=snapshot.orders_submitted,
        fills=snapshot.fills_count,
        trades=snapshot.trades_count,
        rejections=snapshot.rejections,
        skips=snapshot.skips,
        consumed_bars=len(snapshot.consumed_timestamps),
        wins=wins,
        losses=losses,
        round_trips=wins + losses,
        win_rate=_win_rate(wins, losses),
        max_drawdown=None,
        max_drawdown_pct=None,
        return_pct=_return_pct(portfolio.initial_cash, equity),
        ledger=tuple(_row_from_fill(fill) for fill in snapshot.fills),
        equity_curve=(EquityPoint(now, equity),),
    )


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


def report_to_dict(report: SessionReport) -> dict:
    """Return a plain, JSON-serialisable dictionary of the report."""
    return {
        "instrument": report.instrument,
        "interval": report.interval,
        "source": report.source,
        "data_source": report.data_source,
        "replay_mode": report.replay_mode,
        "live_orders": report.live_orders,
        "generated_at": report.generated_at.isoformat(timespec="seconds"),
        "summary": {
            "initial_cash": str(report.initial_cash),
            "cash": str(report.cash),
            "equity": str(report.equity),
            "realized_pnl": str(report.realized_pnl),
            "realized_pnl_today": str(report.realized_pnl_today),
            "unrealized_pnl": str(report.unrealized_pnl),
            "open_quantity": report.open_quantity,
            "orders_submitted": report.orders_submitted,
            "fills": report.fills,
            "trades": report.trades,
            "round_trips": report.round_trips,
            "rejections": report.rejections,
            "skips": report.skips,
            "consumed_bars": report.consumed_bars,
            "wins": report.wins,
            "losses": report.losses,
            "win_rate": str(report.win_rate) if report.win_rate is not None else None,
            "max_drawdown": str(report.max_drawdown) if report.max_drawdown is not None else None,
            "max_drawdown_pct": str(report.max_drawdown_pct) if report.max_drawdown_pct is not None else None,
            "return_pct": str(report.return_pct) if report.return_pct is not None else None,
        },
        "equity_curve": [
            {"time": point.time.isoformat(), "equity": str(point.equity)}
            for point in report.equity_curve
        ],
        "ledger": [
            {
                "time": row.time.isoformat(timespec="seconds"),
                "signal": row.signal.value if row.signal is not None else None,
                "action": row.action,
                "price": str(row.price) if row.price is not None else None,
                "quantity": row.quantity,
                "notional": str(row.notional) if row.notional is not None else None,
                "commission": str(row.commission) if row.commission is not None else None,
                "equity": str(row.equity) if row.equity is not None else None,
            }
            for row in report.ledger
        ],
    }


def _optional(value: Decimal | None) -> str:
    return fmt(value, "") if value is not None else "n/a"


def _ts(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M")


def report_to_html(report: SessionReport) -> str:
    """Render a self-contained, labelled HTML page for the report.

    Reuses the shared ``research.report`` CSS, so the page looks consistent
    with the existing research and backtest reports but is clearly labelled as
    a paper-session (not research) report.
    """
    generated = report.generated_at.strftime("%Y-%m-%d %H:%M:%S")
    title = f"Paper session report &middot; {escape(report.instrument)} @ {escape(report.interval)}"
    cards_html = "".join([
        card(fmt(report.equity), "Equity", "ok" if report.equity >= report.initial_cash else "bad"),
        card(fmt(report.cash), "Cash"),
        card(fmt(report.realized_pnl), "Realized P&L", "ok" if report.realized_pnl >= 0 else "bad"),
        card(_optional(report.return_pct), "Return %"),
        card(_optional(report.max_drawdown), "Max drawdown", "bad" if report.max_drawdown else "neutral"),
        card(_optional(report.win_rate), "Win rate"),
        card(str(report.round_trips), "Round trips"),
        card(str(report.trades), "Trade/accounting events"),
        card(str(report.orders_submitted), "Orders"),
        card(str(report.fills), "Fills"),
        card(str(report.rejections), "Rejections", "bad" if report.rejections else "neutral"),
    ])

    portfolio_rows = kv_rows([
        ("Instrument", f"{report.instrument} @ {report.interval}"),
        ("Data source", report.data_source),
        ("Replay mode", report.replay_mode),
        ("Live orders", "Yes" if report.live_orders else "No"),
        ("Initial cash", fmt(report.initial_cash)),
        ("Cash", fmt(report.cash)),
        ("Equity", fmt(report.equity)),
        ("Realized P&L", fmt(report.realized_pnl)),
        ("Realized P&L (today)", fmt(report.realized_pnl_today)),
        ("Unrealized P&L", fmt(report.unrealized_pnl)),
        ("Open quantity", str(report.open_quantity)),
        ("Wins / losses", f"{report.wins} / {report.losses}"),
        ("Return %", _optional(report.return_pct)),
        ("Max drawdown", _optional(report.max_drawdown)),
        ("Max drawdown %", _optional(report.max_drawdown_pct)),
    ])

    counter_rows = kv_rows([
        ("Consumed bars", str(report.consumed_bars)),
        ("Orders submitted", str(report.orders_submitted)),
        ("Fills", str(report.fills)),
        ("Round trips", str(report.round_trips)),
        ("Trade/accounting events", str(report.trades)),
        ("Rejections", str(report.rejections)),
        ("Skips", str(report.skips)),
    ])

    ledger_rows = [
        [
            _ts(row.time),
            row.signal.value if row.signal is not None else "-",
            row.action,
            _optional(row.price),
            str(row.quantity) if row.quantity is not None else "n/a",
            _optional(row.notional),
            _optional(row.commission),
            _optional(row.equity),
        ]
        for row in report.ledger
    ]

    curve_rows = [[_ts(point.time), fmt(point.equity)] for point in report.equity_curve]

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(report.instrument)} paper session report</title>
<style>{CSS}</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>{title}</h1>
    <p>Generated {escape(generated)} &middot; paper-session operations</p>
  </header>
  <div class="cards">{cards_html}</div>
  <section><h2>Portfolio</h2>{portfolio_rows}</section>
  <section><h2>Session counters</h2>{counter_rows}</section>
  <section><h2>Ledger ({len(report.ledger)} rows)</h2>{table(["Time", "Signal", "Action", "Price", "Qty", "Notional", "Commission", "Equity"], ledger_rows)}</section>
  <section><h2>Equity curve ({len(report.equity_curve)} points)</h2>{table(["Time", "Equity"], curve_rows)}</section>
  <div class="footer">Deterministic paper-session report &middot; no real orders &middot; no secret data</div>
</div>
</body>
</html>"""


def write_html_report(report: SessionReport, path: str | Path) -> Path:
    """Write the HTML rendering of ``report`` to ``path`` (UTF-8).

    This is the only helper here that touches the disk; it exists as the
    explicit operator escape hatch for generating a shareable report page.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(report_to_html(report), encoding="utf-8")
    return target


# --------------------------------------------------------------------------- #
# Operator logging
# --------------------------------------------------------------------------- #


def _default_logger(logger=None):
    return logger if logger is not None else get_logger(_HEALTH_LOGGER)


def log_health(health_obj: SessionHealth, logger=None) -> None:
    """Emit a single deterministic INFO line summarising session health."""
    state = "running" if health_obj.running else "stopped"
    _default_logger(logger).info(
        "health %s %s [%s] env=%s warmup=%s consumed=%s orders=%s fills=%s "
        "trades=%s rejects=%s skips=%s cash=%s equity=%s pnl=%s pnl_today=%s open=%s",
        health_obj.instrument,
        health_obj.interval,
        state,
        health_obj.environment,
        health_obj.warmup,
        health_obj.consumed_bars,
        health_obj.orders_submitted,
        health_obj.fills,
        health_obj.trades,
        health_obj.rejections,
        health_obj.skips,
        f"{health_obj.cash:f}",
        f"{health_obj.equity:f}",
        f"{health_obj.realized_pnl:f}",
        f"{health_obj.realized_pnl_today:f}",
        health_obj.open_quantity,
    )


def log_results(results: Iterable[SessionResult], logger=None) -> None:
    """Log every consumed step and poll summary from a ``run_loop`` result set.

    Deterministic and dependency-free; never logs secrets or environment
    values. Skipped candles are logged at INFO (they are normal throttling,
    not errors); provider failures are logged as warnings.
    """
    log = _default_logger(logger)
    processed = 0
    for result in results:
        for step in result.steps:
            processed += 1
            if step.skipped:
                log.info("skip %s: %s", _ts(step.bar.timestamp), step.skip_reason)
                continue
            log.info(
                "step %s signal=%s %s equity=%s",
                _ts(step.bar.timestamp),
                step.signal.value,
                _step_action(step),
                f"{step.equity:f}",
            )
        if result.provider_error is not None:
            log.warning("poll failed: %s", result.provider_error)
        elif result.consumed == 0:
            log.info("poll: no completed bars")
        else:
            log.info("poll consumed %s bar(s)", result.consumed)
    log.info("loop processed %s step(s)", processed)
