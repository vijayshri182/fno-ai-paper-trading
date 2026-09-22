"""The Daily Paper Trading Track engine (fail-closed orchestrator).

This is a *simulated* 5-minute, single-position, long-only strategy loop for the
NIFTY 50 index. It never touches a live exchange: the only broker it calls is
:class:`~fno_ai_paper_trading.broker.paper_broker.PaperBroker`, whose
``is_live`` is hard-coded ``False`` in the shared layer.

Trust boundary / safety rules encoded here:

* every clock tick is gated by :class:`SessionPolicy` (TRADING / FLATTENING /
  CLOSING / SKIP); outside the window nothing is fetched and nothing can be ordered;
* a BUY entry exists only after the ``RiskManager`` approves it (pre-trade caps:
  max qty / notional / daily-loss). Every approval is logged as
  ``entry_approvals`` and asserted by the invariant suite;
* exits (signal SELL, stop-loss, EOD flatten) are protective: they always stay
  executable and can never increase exposure — the long-only portfolio guard
  rejects any SELL that would open a short;
* the day always ends flat (flatten at 15:20+, emergency flatten after 15:30);
* each consumed bar is checkpointed atomically with a SHA-256 sidecar; crash
  points A-J exist so restart tests prove a bar is never traded twice and a
  fill is never dropped.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path

from fno_ai_paper_trading.broker.paper_broker import PaperBroker, PaperBrokerConfig
from fno_ai_paper_trading.config.settings import Environment, PaperSettings
from fno_ai_paper_trading.models.enums import OrderSide, Signal
from fno_ai_paper_trading.models.instruments import Instrument
from fno_ai_paper_trading.models.market import MarketPrice
from fno_ai_paper_trading.models.order import Order
from fno_ai_paper_trading.paper_track.accounting import max_drawdown
from fno_ai_paper_trading.paper_track.bars import BarValidator, ValidatedBars
from fno_ai_paper_trading.paper_track.clock import Clock, FixedClock
from fno_ai_paper_trading.paper_track.errors import (
    InjectedFailure,
    TrackPolicyViolation,
    TrackRecoveryError,
)
from fno_ai_paper_trading.paper_track.feed import SyntheticFeed, TRACK_INSTRUMENT
from fno_ai_paper_trading.paper_track.lock import RunLock
from fno_ai_paper_trading.paper_track.policy import Gate, SessionPolicy
from fno_ai_paper_trading.paper_track.report import build_report
from fno_ai_paper_trading.paper_track.store import (
    TrackStore,
    fill_from_dict,
    fill_to_dict,
    instrument_to_dict,
    market_price_from_dict,
    market_price_to_dict,
    order_from_dict,
    order_to_dict,
    position_from_dict,
    position_to_dict,
    stable_dumps,
    trade_from_dict,
    trade_to_dict,
)
from fno_ai_paper_trading.portfolio.portfolio import Portfolio
from fno_ai_paper_trading.risk.manager import RiskManager
from fno_ai_paper_trading.risk.sizer import RiskBasedPositionSizer, SizerConfig
from fno_ai_paper_trading.risk.stop_loss import StopLossPolicy, enforce_stop
from fno_ai_paper_trading.strategies.base import Strategy
from fno_ai_paper_trading.strategies.moving_average_cross import MovingAverageCrossStrategy
from fno_ai_paper_trading.utils.functions import new_id, positive_decimal

__all__ = [
    "StepStatus",
    "StepResult",
    "TrackConfig",
    "TrackEngine",
    "DEFAULT_ACCOUNT",
    "DEFAULT_STORE_DIR",
]

DEFAULT_ACCOUNT = "nifty_5m_daily"
DEFAULT_STORE_DIR = Path("data") / "paper_trading"


class StepStatus(str, Enum):
    """Per-tick outcome of :meth:`TrackEngine.step`."""

    SKIPPED = "SKIPPED"
    PROCESSED = "PROCESSED"  # a new completed bar was consumed (may have traded)
    FLATTENED = "FLATTENED"  # a protective EOD flatten fired this tick
    DATA_SKIP = "DATA_SKIP"  # bars were poisoned; nothing valid processed this tick
    IDLE = "IDLE"  # no new bar, nothing to flatten
    ERROR = "ERROR"  # hard structural failure; nothing was traded


@dataclass(frozen=True)
class StepResult:
    status: StepStatus
    moment: datetime
    detail: str = ""
    bar: MarketPrice | None = None

    def __str__(self) -> str:
        base = f"{self.status.value} at {self.moment:%Y-%m-%d %H:%M}"
        return f"{base}: {self.detail}" if self.detail else base


@dataclass(frozen=True)
class TrackConfig:
    """All fixed parameters of a track account (immutable once built)."""

    account: str = DEFAULT_ACCOUNT
    store_dir: Path = DEFAULT_STORE_DIR
    interval: str = "5m"
    initial_cash: Decimal = Decimal("100000")
    instrument: Instrument | None = None
    strategy: Strategy | None = None
    policy: SessionPolicy | None = None
    max_position_quantity: int = 75
    max_order_notional: Decimal = Decimal("250000")
    max_daily_loss: Decimal = Decimal("10000")
    commission_rate: Decimal = Decimal("0.0003")
    commission_fixed: Decimal = Decimal("0")
    slippage_rate: Decimal = Decimal("0.001")
    risk_per_trade_pct: Decimal = Decimal("0.01")
    stop_loss_pct: Decimal = Decimal("0.02")

    def __post_init__(self) -> None:
        object.__setattr__(self, "account", (self.account or "account").strip())
        instrument = self.instrument if self.instrument is not None else TRACK_INSTRUMENT()
        object.__setattr__(self, "instrument", instrument)
        strategy = self.strategy if self.strategy is not None else MovingAverageCrossStrategy()
        object.__setattr__(self, "strategy", strategy)
        policy = self.policy if self.policy is not None else SessionPolicy()
        object.__setattr__(self, "policy", policy)
        object.__setattr__(self, "initial_cash", positive_decimal(self.initial_cash, "initial_cash"))
        if self.max_position_quantity <= 0:
            raise ValueError("max_position_quantity must be > 0")


class TrackEngine:
    """One account/day (or multi-day) run of the Daily Paper Trading Track."""

    def __init__(
        self,
        config: TrackConfig | None = None,
        *,
        store: TrackStore | None = None,
        clock: Clock | None = None,
        bars_source=None,
        failpoints: set[str] | None = None,
        run_id: str | None = None,
    ) -> None:
        config = config if config is not None else TrackConfig()
        self.config = config
        self.store = store if store is not None else TrackStore(config.store_dir, config.account)
        self.clock = clock if clock is not None else FixedClock(datetime(1970, 1, 1))
        self.bars_source = bars_source
        self.failpoints = set(failpoints or ())
        self.run_id = run_id or f"paper-run-{datetime.now():%Y%m%d}-{new_id('RUN')[4:]}"

        settings = PaperSettings(
            environment=Environment.PAPER,
            initial_capital=config.initial_cash,
            max_position_quantity=config.max_position_quantity,
            max_order_notional=config.max_order_notional,
            max_daily_loss=config.max_daily_loss,
            commission_rate=config.commission_rate,
            commission_fixed=config.commission_fixed,
            slippage_rate=config.slippage_rate,
        )
        broker_config = PaperBrokerConfig(
            commission_rate=config.commission_rate,
            commission_fixed=config.commission_fixed,
            slippage_rate=config.slippage_rate,
        )
        self.broker = PaperBroker(config=broker_config, now_fn=self.clock.now)
        self.portfolio = Portfolio(cash=config.initial_cash, long_only=True)
        self.risk_manager = RiskManager(settings)
        self.sizer = RiskBasedPositionSizer(
            SizerConfig(
                risk_per_trade_pct=config.risk_per_trade_pct,
                stop_loss_pct=config.stop_loss_pct,
                commission_rate=config.commission_rate,
                commission_fixed=config.commission_fixed,
            )
        )
        self.stop = StopLossPolicy(config.stop_loss_pct)
        self.validator = BarValidator(
            interval=config.interval, policy=config.policy, expected_symbol=config.instrument.symbol
        )
        self.lock = RunLock(self.store.store_dir, config.account)

        self.day: date | None = None
        self.history: list[MarketPrice] = []
        self.consumed: set[datetime] = set()
        self.last_processed: datetime | None = None
        self.equity_curve: list[Decimal] = [config.initial_cash]
        self.max_intraday_drawdown: Decimal = Decimal("0")
        self.eod_status: str = "NONE"
        self.report_written: bool = False
        self._finalized_days: set[str] = set()
        self.entry_approvals: list[dict] = []
        self.counters = {
            "signals": 0,
            "signal_counts": {"BUY": 0, "SELL": 0, "HOLD": 0},
            "entries_filled": 0,
            "exits_filled": 0,
            "stops_fired": 0,
            "flatten_count": 0,
            "risk_refusals": [],
            "sizing_skips": [],
            "data_skips": [],
            "anomalies": [],
            "errors": [],
        }
        self._session_symbol = config.instrument.symbol

    # ------------------------------------------------------------------ state

    @property
    def symbol(self) -> str:
        return self._session_symbol

    @property
    def position_quantity(self) -> int:
        pos = self.portfolio.position_for(self.symbol)
        return pos.quantity if pos else 0

    @property
    def open(self) -> bool:
        return self.position_quantity != 0

    def _fault(self, point: str) -> None:
        if point in self.failpoints:
            raise InjectedFailure(point)

    # ------------------------------------------------------------------ restore

    def load(self, day: date | None = None) -> bool:
        """Restore this account's checkpoint for ``day`` (or the latest one)."""
        target = day
        if target is None:
            target = self.day
        if target is None:
            checkpoints = sorted(self.store.checkpoints_dir.glob(f"{self.config.account}.*.json"))
            if not checkpoints:
                return False
            target = date.fromisoformat(checkpoints[-1].name.split(".")[-2])
            self.day = target
        payload = self.store.load_checkpoint(target)
        if payload is None:
            return False

        self.day = date.fromisoformat(payload["day"])
        self.run_id = payload.get("run_id", self.run_id)
        portfolio_data = payload["portfolio"]
        self.portfolio = Portfolio(
            cash=Decimal(portfolio_data["cash"]),
            positions={
                symbol: position_from_dict(value)
                for symbol, value in portfolio_data.get("positions", {}).items()
            },
            trade_history=[trade_from_dict(t) for t in portfolio_data.get("trade_history", [])],
            realized_pnl=Decimal(portfolio_data.get("realized_pnl", "0")),
            long_only=True,
        )
        # A resume keeps the account's ORIGINAL deposit as the accounting base,
        # not the restored closing cash: the flat-day identity
        # ``cash = initial + cumulative_gross - cumulative_fees`` must hold
        # across day boundaries with the cumulative trade ledger.
        self.portfolio.initial_cash = Decimal(
            payload.get("initial_cash", str(self.config.initial_cash))
        )
        self.broker = PaperBroker(self.broker.config, now_fn=self.clock.now)
        self.broker.restore(
            [order_from_dict(o) for o in payload["broker"]["orders"]],
            [fill_from_dict(f) for f in payload["broker"]["fills"]],
        )
        self.history = [market_price_from_dict(b) for b in payload["history"]]
        self.consumed = {datetime.fromisoformat(x) for x in payload.get("consumed", [])}
        self.last_processed = (
            datetime.fromisoformat(payload["last_processed"]) if payload.get("last_processed") else None
        )
        self.equity_curve = [Decimal(x) for x in payload.get("equity_curve", [str(self.config.initial_cash)])]
        self.max_intraday_drawdown = Decimal(payload.get("max_intraday_drawdown", "0"))
        self.eod_status = payload.get("eod_status", "NONE")
        self.report_written = bool(payload.get("report_written", False))
        self._finalized_days = set(payload.get("finalized_days", []))
        if self._finalized_days and self.report_written is False:
            self.report_written = True
        self.entry_approvals = list(payload.get("entry_approvals", []))
        self.counters = self._counters_from_payload(payload)

        for position in self.portfolio.open_positions().values():
            if position.opened_at.date() != self.day:
                raise TrackRecoveryError(
                    f"checkpoint for {self.day} carries a position opened on "
                    f"{position.opened_at.date()}: refusing to resume an overnight state leak"
                )
        return True

    def _counters_from_payload(self, payload: dict) -> dict:
        data = payload.get("counters", {})
        signal_counts = data.get("signal_counts", {})
        return {
            "signals": int(data.get("signals", 0)),
            "signal_counts": {
                "BUY": int(signal_counts.get("BUY", 0)),
                "SELL": int(signal_counts.get("SELL", 0)),
                "HOLD": int(signal_counts.get("HOLD", 0)),
            },
            "entries_filled": int(data.get("entries_filled", 0)),
            "exits_filled": int(data.get("exits_filled", 0)),
            "stops_fired": int(data.get("stops_fired", 0)),
            "flatten_count": int(data.get("flatten_count", 0)),
            "risk_refusals": list(data.get("risk_refusals", [])),
            "sizing_skips": list(data.get("sizing_skips", [])),
            "data_skips": list(data.get("data_skips", [])),
            "anomalies": list(data.get("anomalies", [])),
            "errors": list(data.get("errors", [])),
        }

    def state_payload(self) -> dict:
        """Serializable snapshot of everything needed to resume identically."""
        strategy = self.config.strategy
        positions = {
            symbol: position_to_dict(pos)
            for symbol, pos in self.portfolio.positions.items()
            if not pos.is_flat
        }
        orders, _fills = self.broker.snapshot()
        return {
            "schema_version": 1,
            "run_id": self.run_id,
            "account": self.config.account,
            "day": self.day.isoformat() if self.day else None,
            "interval": self.config.interval,
            "instrument": instrument_to_dict(self.config.instrument),
            "initial_cash": str(self.config.initial_cash),
            "strategy": {"name": strategy.name, "fast": 5, "slow": 21},
            "policy": self.config.policy.describe(),
            "broker": {
                "orders": [order_to_dict(o) for o in orders],
                "fills": [fill_to_dict(f) for f in self.broker.fills],
            },
            "portfolio": {
                "cash": str(self.portfolio.cash),
                "positions": positions,
                "trade_history": [trade_to_dict(t) for t in self.portfolio.trade_history],
                "realized_pnl": str(self.portfolio.realized_pnl),
            },
            "history": [market_price_to_dict(b) for b in self.history],
            "consumed": sorted(x.isoformat() for x in self.consumed),
            "last_processed": self.last_processed.isoformat() if self.last_processed else None,
            "equity_curve": [str(x) for x in self.equity_curve],
            "max_intraday_drawdown": str(self.max_intraday_drawdown),
            "eod_status": self.eod_status,
            "report_written": self.report_written,
            "finalized_days": sorted(self._finalized_days),
            "entry_approvals": list(self.entry_approvals),
            "counters": self.counters,
        }

    def _checkpoint(self) -> None:
        if self.day is not None:
            self.store.save_checkpoint(self.day, self.state_payload())

    def save_report_payload(self) -> None:
        """Per-day report write, idempotent and crash-safe (atomic store writes).

        A day is finalized at most once; re-finalising on a restart of the same
        account/day is skipped so reports stay byte-identical across reruns.
        """
        if self.day is None or self.day.isoformat() in self._finalized_days:
            return
        report = build_report(self)
        self.store.save_report(self.day, report)
        self._finalized_days.add(self.day.isoformat())
        self.report_written = True
        self.store.update_run(self.run_id, day=self.day.isoformat(), status=self.eod_status or "RUNNING")

    # ------------------------------------------------------------------ stepping

    def step(self) -> StepResult:
        """Run one complete tick at the engine's current clock time."""
        now = self.clock.now()
        if self.day is None:
            self.day = now.date()
        elif now.date() != self.day:
            if self.open:
                raise TrackPolicyViolation(
                    f"date rolled to {now.date()} with an open position from {self.day}: must stay flat overnight"
                )
            self.day = now.date()
            # A new session starts un-finalized; never let a stale status leak
            # from the previous day into the new day's early checkpoints.
            self.eod_status = "NONE"

        decision = self.config.policy.gate(now)
        if decision.gate is Gate.SKIP:
            return StepResult(StepStatus.SKIPPED, now, decision.reason)
        if decision.gate is Gate.CLOSING and not self.open:
            return StepResult(StepStatus.SKIPPED, now, "after close and flat")

        bars = self.bars_source.bars_up_to(now) if self.bars_source is not None else []
        # Only bars after what we already consumed are candidates for this tick;
        # older bars in a cumulative/overlapping delivery are history, not a
        # data-quality anomaly (the validator's `prior` bucket still catches a
        # cold-start with a multi-day provider window).
        if self.last_processed is not None:
            bars = [bar for bar in bars if bar.timestamp > self.last_processed]
        self._fault("A")

        validated = self.validator.validate(bars, now=now, last_processed=self.last_processed)
        for bar, reason in zip(validated.poisoned, validated.poison_reasons):
            ts = getattr(bar, "timestamp", now)
            self.counters["data_skips"].append({"ts": ts.isoformat(), "reason": reason})
        self.counters["anomalies"].extend(validated.anomalies)
        if not validated.ok:
            self._record_error(validated.error or "validation failed")
            return StepResult(StepStatus.ERROR, now, validated.error or "validation failed")

        next_bar = self._next_bar(validated, now)
        if next_bar is None:
            return self._idle_or_flatten(now, validated.valid)

        status, detail = self._process_bar(next_bar, now, decision)
        self._fault("G")
        self._checkpoint()
        self._fault("H")
        self._maybe_finalize(now)
        return StepResult(status, now, detail, bar=next_bar)

    def _next_bar(self, validated: ValidatedBars, now: datetime) -> MarketPrice | None:
        for bar in validated.valid:
            if self.last_processed is not None and bar.timestamp <= self.last_processed:
                continue
            return bar
        return None

    def _idle_or_flatten(self, now: datetime, bars: tuple) -> StepResult:
        if self.open and self.config.policy.gate(now).allows_flatten:
            ref = self._flatten_reference(bars)
            if ref is None:
                self._record_error("flatten required but no usable bar exists")
                return StepResult(StepStatus.ERROR, now, "no reference bar for flatten")
            self._fault("I")
            trade = self._exit_long(ref, kind="flatten")
            self._fault("J")
            if trade is not None:
                self._checkpoint()
                return StepResult(
                    StepStatus.FLATTENED, now, "EOD flatten on last known close", bar=ref
                )
            self._record_error("flatten order did not fill")
            return StepResult(StepStatus.ERROR, now, "flatten did not fill")
        return StepResult(StepStatus.IDLE, now, "no new bar")

    def _flatten_reference(self, bars: tuple) -> MarketPrice | None:
        for bar in reversed(bars):
            return bar
        if self.history:
            return self.history[-1]
        return None

    def _process_bar(self, bar: MarketPrice, now: datetime, decision) -> tuple[StepStatus, str]:
        symbol = self._session_symbol
        prefix = self.history + [bar]

        self._fault("B")
        signal = self.config.strategy.analyze(prefix).signal
        self.counters["signals"] += 1
        self.counters["signal_counts"][signal.value] += 1

        flattens_before = self.counters["flatten_count"]

        if self.position_quantity == 0 and signal is Signal.BUY and decision.allows_entries:
            self._attempt_entry(bar, now)

        if self.position_quantity > 0:
            if signal is Signal.SELL and decision.allows_exits:
                self._exit_long(bar, kind="signal")
            else:
                self._enforce_stop(bar)

        if decision.allows_flatten and self.position_quantity > 0:
            self._fault("I")
            self._exit_long(bar, kind="flatten")
            self._fault("J")

        self.history.append(bar)
        self.consumed.add(bar.timestamp)
        self.last_processed = bar.timestamp
        self._append_equity(bar.close)

        flattened_now = self.counters["flatten_count"] > flattens_before
        status = StepStatus.FLATTENED if flattened_now else StepStatus.PROCESSED
        detail = f"{symbol} close={bar.close:g} signal={signal.value}"
        return status, detail

    # ------------------------------------------------------------------ trading

    def _attempt_entry(self, bar: MarketPrice, now: datetime) -> None:
        symbol = self._session_symbol
        equity = self.portfolio.total_value({symbol: bar.close})
        sizing = self.sizer.size(
            equity=equity,
            available_cash=self.portfolio.cash,
            entry_price=bar.close,
            instrument=self.config.instrument,
            current_quantity=0,
        )
        if not sizing.approved:
            self.counters["sizing_skips"].append({"ts": now.isoformat(), "reason": sizing.skip_reason})
            return

        order = Order(
            instrument=self.config.instrument, side=OrderSide.BUY, quantity=sizing.quantity
        )
        risk = self.risk_manager.evaluate(order, self.portfolio, bar.close)
        self._fault("C")
        if not risk.approved:
            self.counters["risk_refusals"].append(
                {
                    "ts": now.isoformat(),
                    "side": OrderSide.BUY.value,
                    "quantity": sizing.quantity,
                    "close": str(bar.close),
                    "reason": risk.summary,
                }
            )
            return

        self._fault("D")
        self._fault("E")
        fill = self.broker.place_order(order, bar)
        self._fault("F")
        if fill is None:
            self._record_error("broker declined BUY fill")
            return
        self.entry_approvals.append(
            {
                "bar_timestamp": bar.timestamp.isoformat(),
                "filled_at": fill.filled_at.isoformat(),
                "quantity": fill.quantity,
                "close": str(bar.close),
            }
        )
        self.portfolio.apply_fill(fill)
        self.counters["entries_filled"] += 1

    def _exit_long(self, bar: MarketPrice, *, kind: str):
        position = self.portfolio.position_for(self._session_symbol)
        if position is None or position.is_flat:
            return None
        # Protective exit by design: never increases exposure, stays executable
        # at the daily-loss cap, and needs no RiskManager approval.
        order = Order(
            instrument=self.config.instrument, side=OrderSide.SELL, quantity=position.quantity
        )
        self._fault("E")
        fill = self.broker.place_order(order, bar)
        self._fault("F")
        if fill is None:
            self._record_error(f"{kind} exit did not fill")
            return None
        trade = self.portfolio.apply_fill(fill)
        self.counters["exits_filled"] += 1
        if kind == "flatten":
            self.counters["flatten_count"] += 1
            self.eod_status = "FLATTENED"
        return trade

    def _enforce_stop(self, bar: MarketPrice) -> None:
        position = self.portfolio.position_for(self._session_symbol)
        if position is None or position.is_flat:
            return
        result = enforce_stop(
            broker=self.broker, portfolio=self.portfolio, position=position, bar=bar, policy=self.stop
        )
        if result is not None and result.fill is not None:
            self.counters["stops_fired"] += 1
            self.counters["exits_filled"] += 1

    def _append_equity(self, mark: Decimal) -> None:
        value = self.portfolio.total_value({self._session_symbol: mark})
        self.equity_curve.append(value)
        dd = max_drawdown(self.equity_curve)
        if dd > self.max_intraday_drawdown:
            self.max_intraday_drawdown = dd

    def _maybe_finalize(self, now: datetime) -> None:
        if self.day is not None and now.time() >= self.config.policy.window_close:
            if self.eod_status == "NONE" and not self.open:
                # Persist a finalized FLAT checkpoint for this day so the
                # day-end file (and checkpt/reloads) shows the true status
                # instead of the pre-finalize "NONE" snapshot.
                self.eod_status = "FLAT"
                self.store.save_checkpoint(self.day, self.state_payload())
            self.save_report_payload()

    def _record_error(self, message: str) -> None:
        self.counters["errors"].append(message)

    # ------------------------------------------------------------------ reporting

    def report(self) -> dict | None:
        """The daily report for the current day, rebuilt live (None before a day)."""
        if self.day is None:
            return None
        return build_report(self)

    def stable_fingerprint(self) -> str:
        """Economic fingerprint of the current run (rerun-comparable)."""
        import hashlib

        payload = {
            "day": self.day.isoformat() if self.day else None,
            "fills": [[f.side.value, f.quantity, str(f.price), str(f.commission)] for f in self.broker.fills],
            "trades": [
                [t.side.value, t.quantity, str(t.price), str(t.realized_pnl)]
                for t in self.portfolio.trade_history
            ],
            "cash": str(self.portfolio.cash),
            "realized": str(self.portfolio.realized_pnl),
        }
        return hashlib.sha256(stable_dumps(payload).encode("utf-8")).hexdigest()