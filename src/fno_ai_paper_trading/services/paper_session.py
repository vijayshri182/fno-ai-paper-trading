"""Deterministic current-data paper session runtime (WS 6.4b / WS 6.5).

This is the orchestration layer of the V1 paper-trading session. It owns exactly
the session lifecycle concerns and nothing else:

* completed-bar selection (``bar.timestamp + interval <= now``), so a forming
  bar is never traded and the decision clock is injectable,
* duplicate-candle guard (one decision per bar timestamp),
* NSE phase / holiday gating via ``market_hours``,
* the 22-bar (``slow + 1``) warm-up gate,
* signal -> action mapping (long-only V1: BUY opens, SELL closes, HOLD no-op),
* protective-stop ordering (signal first, stop second, entry candle excluded)
  routed through the single authoritative ``TradingService.protective_exit`` /
  :func:`~fno_ai_paper_trading.risk.stop_loss.enforce_stop` executor,
* equity snapshots, counters and the ``--once``/``--loop`` lifecycle,
* session-state snapshot and restore for crash-recovery across restarts (WS 6.5).

It deliberately contains **no** sizing math, **no** risk arithmetic, **no**
stop arithmetic, **no** environment parsing, and **no** disk I/O: sizing lives
in :class:`RiskBasedPositionSizer`, static caps in :class:`RiskManager`, the
stop rule/executor in ``risk.stop_loss``, snapshots are created by
:meth:`snapshot` and restored by :meth:`restore`, and settings come from
:class:`PaperSettings`. Disk persistence belongs exclusively to
:mod:`fno_ai_paper_trading.persistence.session_store`; this module never writes
files. It never imports ``backtest.*``.

``Portfolio.apply_fill`` remains the sole accounting path; the session never
mutates cash, positions, P&L or trades itself. Restore only deserializes prior
results — it never re-executes fills.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Callable

from fno_ai_paper_trading.broker.base import Broker
from fno_ai_paper_trading.broker.paper_broker import PaperBroker, PaperBrokerConfig
from fno_ai_paper_trading.config.settings import Environment, PaperSettings
from fno_ai_paper_trading.data.errors import MarketDataError
from fno_ai_paper_trading.data.intervals import canonical_interval, interval_minutes
from fno_ai_paper_trading.data.market_hours import NSE_TZ, is_trading_day, market_phase
from fno_ai_paper_trading.data.provider import MarketDataProvider
from fno_ai_paper_trading.models.enums import MarketPhase, OrderSide, OrderType, Signal
from fno_ai_paper_trading.models.instruments import Instrument
from fno_ai_paper_trading.models.market import MarketPrice
from fno_ai_paper_trading.models.order import Order
from fno_ai_paper_trading.models.position import Position
from fno_ai_paper_trading.persistence.session_store import SessionSnapshot
from fno_ai_paper_trading.portfolio.portfolio import Portfolio
from fno_ai_paper_trading.risk.manager import RiskDecision, RiskManager
from fno_ai_paper_trading.risk.sizer import RiskBasedPositionSizer, SizerConfig, SizingResult
from fno_ai_paper_trading.risk.stop_loss import StopLossPolicy
from fno_ai_paper_trading.services.trading_service import OrderResult, TradingService
from fno_ai_paper_trading.strategies.base import Strategy
from fno_ai_paper_trading.strategies.moving_average_cross import MovingAverageCrossStrategy
from fno_ai_paper_trading.utils.functions import non_negative_int, positive_int

_MISSING = object()


@dataclass(frozen=True)
class SessionStep:
    """Everything the session produced for a single consumed candle."""

    bar: MarketPrice
    signal: Signal
    skipped: bool = False
    skip_reason: str = ""
    sizing: SizingResult | None = None
    order_result: OrderResult | None = None
    stop_result: OrderResult | None = None
    equity: Decimal = Decimal("0")
    error: str | None = None


@dataclass(frozen=True)
class SessionResult:
    """Outcome of a single :meth:`PaperSession.poll`."""

    steps: list[SessionStep]
    consumed: int = 0
    provider_error: str | None = None

    @property
    def empty(self) -> bool:
        """True when this poll consumed no candle (nothing was processed)."""
        return self.consumed == 0


class PaperSession:
    """Deterministic, long-only, completed-bar paper session orchestrator.

    Everything the driver needs can be injected for unit tests: the provider
    (bars), a ``clock: Callable[[], datetime]`` for the decision time, the
    broker (``PaperBroker`` only), the portfolio, the risk manager, the sizer
    and the stop policy. When omitted, defaults are built from ``settings`` and
    the injected clock is used as the broker's fill clock so a full replay is
    bit-for-bit reproducible.
    """

    def __init__(
        self,
        settings: PaperSettings,
        provider: MarketDataProvider,
        strategy: Strategy | None = None,
        *,
        instrument: Instrument | None = None,
        broker: Broker | None = None,
        portfolio: Portfolio | None = None,
        risk_manager: RiskManager | None = None,
        sizer: RiskBasedPositionSizer | None = _MISSING,
        quantity: int = 1,
        stop_policy: StopLossPolicy | None = None,
        clock: Callable[[], datetime] | None = None,
        interval: str | None = None,
        warmup_bars: int | None = None,
        allow_sandbox: bool = False,
    ) -> None:
        if not isinstance(settings, PaperSettings):
            raise TypeError("settings must be a PaperSettings")
        self.settings = settings
        self.provider = provider
        self.allow_sandbox = allow_sandbox

        if instrument is None:
            instruments = provider.get_instruments()
            if len(instruments) != 1:
                raise ValueError(
                    "the paper session requires an explicit instrument "
                    "when the provider exposes more than one"
                )
            self.instrument = instruments[0]
        else:
            self.instrument = instrument

        self.strategy = strategy if strategy is not None else MovingAverageCrossStrategy()
        if not isinstance(self.strategy, Strategy):
            raise TypeError("strategy must be a Strategy")

        initial_capital = portfolio.initial_cash if portfolio is not None else settings.initial_capital
        self.portfolio = portfolio if portfolio is not None else Portfolio(initial_capital, long_only=True)
        self.risk_manager = risk_manager if risk_manager is not None else RiskManager(settings)
        self._clock = clock if clock is not None else datetime.now

        if broker is None:
            self.broker = PaperBroker(
                PaperBrokerConfig(
                    commission_rate=settings.commission_rate,
                    commission_fixed=settings.commission_fixed,
                    slippage_rate=settings.slippage_rate,
                ),
                now_fn=self._clock,
            )
        else:
            self.broker = broker
        if not isinstance(self.broker, PaperBroker):
            raise RuntimeError("non-paper brokers are not supported in Phase 1")

        self._trading = TradingService(settings, provider, self.risk_manager, self.broker, self.portfolio)

        if sizer is _MISSING:
            self.sizer = RiskBasedPositionSizer(
                SizerConfig(
                    risk_per_trade_pct=settings.paper_risk_per_trade_pct,
                    stop_loss_pct=settings.paper_stop_loss_pct,
                    commission_rate=settings.commission_rate,
                    commission_fixed=settings.commission_fixed,
                )
            )
        else:
            self.sizer = sizer
        self.quantity = positive_int(quantity, "quantity")

        self.stop_policy = stop_policy if stop_policy is not None else StopLossPolicy(
            stop_loss_pct=settings.paper_stop_loss_pct
        )

        token = canonical_interval(interval if interval is not None else settings.paper_interval)
        if token is None:
            raise ValueError(
                f"unknown session interval {interval or settings.paper_interval!r}"
            )
        minutes = interval_minutes(token)
        if minutes is None:
            raise ValueError("the session interval must be a fixed-minute interval")
        self.interval_token = token
        self.interval_minutes = minutes

        if warmup_bars is None:
            slow = getattr(self.strategy, "slow", None)
            self.warmup_bars = slow + 1 if isinstance(slow, int) else 1
        else:
            self.warmup_bars = non_negative_int(warmup_bars, "warmup_bars")

        self._running = False
        self._consumed: set[datetime] = set()
        self._entry_candle: dict[str, datetime] = {}
        self._orders = 0
        self._fills = 0
        self._trades = 0
        self._rejections = 0
        self._skips = 0

    # ------------------------------------------------------------------ clock
    def now(self) -> datetime:
        """Current session decision time from the injected clock."""
        return self._clock()

    # -------------------------------------------------------------- lifecycle
    @property
    def is_running(self) -> bool:
        return self._running

    def start(self) -> None:
        """Start the session; requires a paper (or approved sandbox) environment."""
        if self._running:
            return
        if self.settings.environment is not Environment.PAPER and not (
            self.allow_sandbox
            and self.settings.environment in (Environment.TEST, Environment.DEVELOPMENT)
        ):
            raise EnvironmentError(
                "the paper session requires Environment.PAPER "
                "(or an explicitly approved TEST/DEVELOPMENT sandbox override)"
            )
        self._running = True

    def stop(self) -> None:
        """Stop the session; further polls raise until :meth:`start` again."""
        self._running = False

    def _ensure_running(self) -> None:
        if not self._running:
            raise RuntimeError("the paper session is not running; call start() first")

    # ------------------------------------------------------------- execution
    def run_once(self, when: datetime | None = None) -> SessionResult:
        """Evaluate the latest completed, unconsumed candle and return."""
        self._ensure_running()
        return self.poll(when)

    def run_loop(self, polls: int = 1, when: Callable[[], datetime] | None = None) -> list[SessionResult]:
        """Run ``polls`` synchronous ``poll`` iterations (deterministic, no sleep).

        ``when`` is an optional clock used per iteration; when omitted each
        iteration uses the session clock.
        """
        self._ensure_running()
        positive_int(polls, "polls")
        results: list[SessionResult] = []
        for _ in range(polls):
            results.append(self.poll(when() if when is not None else None))
        return results

    def poll(self, when: datetime | None = None) -> SessionResult:
        """Process every completed, unconsumed candle up to ``when``.

        Candles are processed in chronological order, each at most once. A
        provider failure surfaces as ``provider_error`` and changes nothing.
        """
        self._ensure_running()
        now = when if when is not None else self.now()

        bars, error = self._fetch_bars()
        if bars is None:
            return SessionResult(steps=[], consumed=0, provider_error=error)

        by_ts: dict[datetime, MarketPrice] = {}
        for bar in bars:
            by_ts[bar.timestamp] = bar
        ordered = list(by_ts.values())
        index_of = {bar.timestamp: i for i, bar in enumerate(ordered)}

        eligible = [
            bar
            for bar in ordered
            if self._is_completed(bar, now) and bar.timestamp not in self._consumed
        ]
        eligible.sort(key=lambda bar: bar.timestamp)

        steps: list[SessionStep] = []
        for bar in eligible:
            prefix = ordered[: index_of[bar.timestamp] + 1]
            step = self._process_candle(prefix, bar, now)
            if step.skipped:
                self._skips += 1
            steps.append(step)
            self._consumed.add(bar.timestamp)

        return SessionResult(steps=steps, consumed=len(steps))

    # ------------------------------------------------------------ fetch/helpers
    def _fetch_bars(self) -> tuple[list[MarketPrice] | None, str | None]:
        try:
            return self.provider.get_ohlcv(self.instrument), None
        except (MarketDataError, KeyError) as exc:
            return None, str(exc)

    def _is_completed(self, bar: MarketPrice, now: datetime) -> bool:
        return bar.timestamp + timedelta(minutes=self.interval_minutes) <= now

    def _phase_gate(self, now: datetime) -> str | None:
        if not is_trading_day(now, tz=NSE_TZ):
            return "not a trading day"
        if market_phase(now, tz=NSE_TZ) is not MarketPhase.OPEN:
            return "outside the NSE OPEN phase"
        return None

    def _daily_loss_hit(self, now: datetime) -> bool:
        realized = self.portfolio.realized_pnl_today(today=now.date())
        return realized <= -self.settings.max_daily_loss

    def _decision_equity(self, close: Decimal) -> Decimal:
        prices: dict[str, Decimal] = {self.instrument.symbol: close}
        for symbol, position in self.portfolio.open_positions().items():
            if symbol not in prices:
                prices[symbol] = self.provider.get_last_price(position.instrument)
        return self.portfolio.total_value(prices)

    # ------------------------------------------------------------- per-candle
    def _process_candle(self, prefix: list[MarketPrice], bar: MarketPrice, now: datetime) -> SessionStep:
        symbol = self.instrument.symbol

        gate = self._phase_gate(now)
        if gate is not None:
            return self._step(bar, skipped=True, skip_reason=f"{gate} at {now:%Y-%m-%d %H:%M}")

        if len(prefix) < self.warmup_bars:
            return self._step(
                bar,
                skipped=True,
                skip_reason=(
                    f"warm-up: need at least {self.warmup_bars} completed bars; "
                    f"have {len(prefix)}"
                ),
            )

        step = self._step(bar, signal=self.strategy.analyze(prefix).signal)

        if step.signal is Signal.BUY:
            step = self._handle_buy(step, bar, now)
        elif step.signal is Signal.SELL:
            step = self._handle_sell(step, bar, now)
        # HOLD (and non-actionable) never create an order.

        if step.error is None:
            step = self._handle_stop(step, bar)

        return replace(step, equity=self._decision_equity(bar.close))

    def _handle_buy(self, step: SessionStep, bar: MarketPrice, now: datetime) -> SessionStep:
        symbol = self.instrument.symbol
        current = self.portfolio.current_quantity(symbol)
        if current > 0:
            return replace(step, skipped=True, skip_reason="already long; BUY ignored")
        if current < 0:
            return replace(step, skipped=True, skip_reason="short position; long-only BUY ignored")
        if self._daily_loss_hit(now):
            return replace(
                step,
                skipped=True,
                skip_reason="daily-loss limit reached; new entries suspended",
            )

        sizing: SizingResult | None = None
        if self.sizer is not None:
            sizing = self.sizer.size(
                equity=self._decision_equity(bar.close),
                available_cash=self.portfolio.cash,
                entry_price=bar.close,
                instrument=self.instrument,
                current_quantity=current,
            )
            if not sizing.approved:
                return replace(
                    step,
                    sizing=sizing,
                    skipped=True,
                    skip_reason=f"sizing rejected: {sizing.skip_reason}",
                )
        quantity = sizing.quantity if sizing is not None else self.quantity

        order_result, error = self._submit(
            OrderSide.BUY, quantity, reference_price=bar.close, fill_bar=bar
        )
        step = replace(step, sizing=sizing, order_result=order_result, error=error)
        if order_result is not None and order_result.fill is not None and self.portfolio.current_quantity(symbol) > 0:
            self._entry_candle[symbol] = bar.timestamp
        return step

    def _handle_sell(self, step: SessionStep, bar: MarketPrice, now: datetime) -> SessionStep:
        symbol = self.instrument.symbol
        current = self.portfolio.current_quantity(symbol)
        if current <= 0:
            return replace(step, skipped=True, skip_reason="flat; SELL ignored (long-only session)")

        position = self.portfolio.position_for(symbol)
        if position is None:
            return replace(step, skipped=True, skip_reason="no open position to close")
        quantity = min(position.quantity, current)

        if self._daily_loss_hit(now):
            # Protective boundary: the close stays executable at the loss cap.
            order_result = self._force_close(position, quantity, bar, "signal exit at daily-loss cap")
            error = f"accounting failed on exit: {_no_trade(order_result)}" if _accounting_failed(order_result) else None
        else:
            order_result, error = self._submit(
                OrderSide.SELL, quantity, reference_price=bar.close, fill_bar=bar
            )
            if order_result is not None and order_result.fill is None and any(
                "DAILY_LOSS" in reason for reason in order_result.decision.reasons
            ):
                order_result = self._force_close(position, quantity, bar, "signal exit at daily-loss cap")
                error = (
                    f"accounting failed on exit: {_no_trade(order_result)}"
                    if _accounting_failed(order_result)
                    else None
                )
        step = replace(step, order_result=order_result, error=error)

        if self.portfolio.current_quantity(symbol) == 0:
            self._entry_candle.pop(symbol, None)
        return step

    def _handle_stop(self, step: SessionStep, bar: MarketPrice) -> SessionStep:
        symbol = self.instrument.symbol
        entry_ts = self._entry_candle.get(symbol)
        position = self.portfolio.position_for(symbol)
        if position is None or not position.is_long:
            return step
        if entry_ts is None or entry_ts == bar.timestamp:
            return step  # the entry candle itself is never a stop candle
        # Present the position with its *entry-bar* open time so the WS 6.4
        # "opened_at >= bar.timestamp" exclusion is consistent with the session's
        # candle clock (decision time is not the fill wall-clock).
        stop_position = replace(position, opened_at=entry_ts)
        try:
            order_result = self._trading.protective_exit(stop_position, bar, self.stop_policy)
        except ValueError as exc:
            return replace(step, error=f"accounting failed on protective exit: {exc}")
        if order_result is None:
            return step
        self._orders += 1
        if order_result.fill is not None:
            self._fills += 1
            if order_result.trade is not None:
                self._trades += 1
        else:
            self._rejections += 1
        if self.portfolio.current_quantity(symbol) == 0:
            self._entry_candle.pop(symbol, None)
        return replace(step, stop_result=order_result)

    # ------------------------------------------------------------ order paths
    def _submit(
        self,
        side: OrderSide,
        quantity: int,
        *,
        reference_price: Decimal,
        fill_bar: MarketPrice,
    ) -> tuple[OrderResult | None, str | None]:
        self._orders += 1
        try:
            result = self._trading.submit_order(
                self.instrument,
                side,
                quantity,
                reference_price=reference_price,
                fill_bar=fill_bar,
            )
        except ValueError as exc:
            self._rejections += 1
            return None, f"order failed: {exc}"
        if result.fill is not None:
            self._fills += 1
            if result.trade is not None:
                self._trades += 1
        else:
            self._rejections += 1
        return result, None

    def _force_close(
        self,
        position: Position,
        quantity: int,
        bar: MarketPrice,
        reason: str,
    ) -> OrderResult:
        """Close an existing long through the protective-exit boundary.

        Used only when a signal exit must remain executable after the daily-loss
        limit is reached (the ``RiskManager`` is deliberately not consulted,
        exactly as for protective stops). Priced deterministically at the
        completed-bar close through the same broker + ``Portfolio.apply_fill``
        path; contains no stop arithmetic.
        """
        self._orders += 1
        order = Order(
            instrument=position.instrument,
            side=OrderSide.SELL,
            quantity=quantity,
            order_type=OrderType.MARKET,
        )
        exit_bar = MarketPrice(
            instrument=bar.instrument,
            timestamp=bar.timestamp,
            open=bar.close,
            high=bar.close,
            low=bar.close,
            close=bar.close,
            volume=bar.volume,
        )
        try:
            fill = self.broker.place_order(order, exit_bar)
        except ValueError as exc:
            self._rejections += 1
            return OrderResult(
                decision=RiskDecision(approved=False, reasons=[str(exc)]),
                order=order,
                reference_price=bar.close,
            )
        if fill is None:
            self._rejections += 1
            return OrderResult(
                decision=RiskDecision(approved=False, reasons=["exit fill unavailable"]),
                order=order,
                reference_price=bar.close,
            )
        try:
            trade = self.portfolio.apply_fill(fill)
        except ValueError as exc:
            self._rejections += 1
            return OrderResult(
                decision=RiskDecision(approved=False, reasons=[str(exc)]),
                order=order,
                fill=fill,
                reference_price=bar.close,
            )
        self._fills += 1
        if trade is not None:
            self._trades += 1
        return OrderResult(
            decision=RiskDecision(approved=True, reasons=[reason]),
            order=order,
            fill=fill,
            trade=trade,
            reference_price=bar.close,
        )

    # ------------------------------------------------------------ snapshot/restore
    def snapshot(self) -> SessionSnapshot:
        """A stable, detached capture of the session's entire evolving state.

        The snapshot is safe to hold while the session continues: portfolio
        positions are copied, orders are copied via ``dataclasses.replace``,
        and frozen fills are shared. The snapshot object itself is immutable.
        """
        broker_orders, broker_fills = self.broker.snapshot()
        portfolio = _portfolio_from_live(self.portfolio)
        return SessionSnapshot(
            instrument=self.instrument,
            interval_token=self.interval_token,
            interval_minutes=self.interval_minutes,
            warmup_bars=self.warmup_bars,
            quantity=self.quantity,
            portfolio=portfolio,
            orders=tuple(broker_orders),
            fills=tuple(broker_fills),
            consumed_timestamps=tuple(sorted(self._consumed)),
            entry_candles=tuple(sorted(self._entry_candle.items())),
            orders_submitted=self._orders,
            fills_count=self._fills,
            trades_count=self._trades,
            rejections=self._rejections,
            skips=self._skips,
        )

    def restore(self, snapshot: SessionSnapshot) -> None:
        """Overwrite this session's entire evolving state from ``snapshot``.

        The session must be stopped and must have been built for the same
        instrument, interval and warm-up gate as the snapshot. Restore is a pure
        deserialization of recorded results: it never re-runs accounting, fills or
        ``Portfolio.apply_fill``.
        """
        if self._running:
            raise RuntimeError(
                "cannot restore into a running session; call stop() first"
            )
        _validate_restore_target(snapshot, self.instrument, self.interval_token, self.warmup_bars)

        self.portfolio = _portfolio_from_snapshot(snapshot.portfolio)
        self.broker.restore(list(snapshot.orders), list(snapshot.fills))
        self._trading = TradingService(
            self.settings,
            self.provider,
            self.risk_manager,
            self.broker,
            self.portfolio,
        )
        self._consumed = set(snapshot.consumed_timestamps)
        self._entry_candle = dict(snapshot.entry_candles)
        self._orders = snapshot.orders_submitted
        self._fills = snapshot.fills_count
        self._trades = snapshot.trades_count
        self._rejections = snapshot.rejections
        self._skips = snapshot.skips
        self._running = False

    # ------------------------------------------------------------ step builder
    @staticmethod
    def _step(bar: MarketPrice, *, signal: Signal = Signal.HOLD, **kwargs) -> SessionStep:
        return SessionStep(bar=bar, signal=signal, **kwargs)

    # --------------------------------------------------------------- counters
    @property
    def orders_submitted(self) -> int:
        return self._orders

    @property
    def fills(self) -> int:
        return self._fills

    @property
    def trades(self) -> int:
        return self._trades

    @property
    def rejections(self) -> int:
        return self._rejections

    @property
    def skipped_candles(self) -> int:
        return self._skips

    @property
    def consumed_candles(self) -> int:
        return len(self._consumed)


# ------------------------------------------------------------------ #
# Snapshot helpers (module-level to avoid aliasing concerns)
# ------------------------------------------------------------------ #


def _validate_restore_target(
    snapshot: SessionSnapshot,
    session_instrument: Instrument,
    session_interval_token: str,
    session_warmup_bars: int,
) -> None:
    if snapshot.instrument.symbol != session_instrument.symbol:
        raise ValueError(
            f"snapshot instrument symbol {snapshot.instrument.symbol!r} "
            f"does not match session {session_instrument.symbol!r}"
        )
    if snapshot.instrument.exchange_token != session_instrument.exchange_token:
        raise ValueError(
            f"snapshot instrument token {snapshot.instrument.exchange_token!r} "
            f"does not match session {session_instrument.exchange_token!r}"
        )
    if snapshot.interval_token != session_interval_token:
        raise ValueError(
            f"snapshot interval {snapshot.interval_token!r} "
            f"does not match session interval {session_interval_token!r}"
        )
    if snapshot.warmup_bars != session_warmup_bars:
        raise ValueError(
            f"snapshot warmup_bars {snapshot.warmup_bars} "
            f"does not match session warmup_bars {session_warmup_bars}"
        )


def _portfolio_from_live(portfolio: Portfolio) -> Portfolio:
    """Copy a live portfolio into a new, snapshot-safe instance.

    Position objects are copied so later session activity on the original
    portfolio cannot mutate the snapshot. ``realized_pnl`` may be negative on a
    live position (partial-close at a loss) and must be set after construction
    because ``Position.__post_init__`` enforces non-negative validation.
    """
    positions: dict[str, Position] = {}
    for symbol, position in portfolio.positions.items():
        if position.is_flat:
            continue
        copied = Position(
            instrument=position.instrument,
            quantity=position.quantity,
            average_entry_price=position.average_entry_price,
            opened_at=position.opened_at,
        )
        copied.realized_pnl = position.realized_pnl
        positions[symbol] = copied
    snapshot_portfolio = Portfolio(
        portfolio.cash,
        positions=positions,
        trade_history=list(portfolio.trade_history),
        realized_pnl=portfolio.realized_pnl,
        long_only=portfolio.long_only,
    )
    snapshot_portfolio.initial_cash = portfolio.initial_cash
    return snapshot_portfolio


def _portfolio_from_snapshot(portfolio: Portfolio) -> Portfolio:
    """Build a fresh Portfolio from a deserialized snapshot portfolio.

    The snapshot portfolio already contains freshly deserialized Position objects
    (from ``session_store._position_from_dict``), so these are safe to pass
    directly. ``realized_pnl`` is applied after construction for the same reason
    as :func:`_portfolio_from_live`.
    """
    positions: dict[str, Position] = {}
    for symbol, position in portfolio.positions.items():
        if position.is_flat:
            continue
        reconstructed = Position(
            instrument=position.instrument,
            quantity=position.quantity,
            average_entry_price=position.average_entry_price,
            opened_at=position.opened_at,
        )
        reconstructed.realized_pnl = position.realized_pnl
        positions[symbol] = reconstructed
    reconstructed_portfolio = Portfolio(
        portfolio.cash,
        positions=positions,
        trade_history=list(portfolio.trade_history),
        realized_pnl=portfolio.realized_pnl,
        long_only=portfolio.long_only,
    )
    reconstructed_portfolio.initial_cash = portfolio.initial_cash
    return reconstructed_portfolio


def _accounting_failed(order_result: OrderResult | None) -> bool:
    return order_result is not None and order_result.fill is not None and order_result.trade is None


def _no_trade(order_result: OrderResult | None) -> str:
    return "no trade recorded" if _accounting_failed(order_result) else "order rejected"