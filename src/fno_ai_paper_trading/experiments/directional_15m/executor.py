"""Isolated execution engine for the 15-minute directional experiment.

Reuses (never modifies) the committed machinery:

* ``PaperBroker`` (``is_live=False``) for simulated fills,
* ``RiskBasedPositionSizer`` + ``RiskManager`` (committed settings caps),
* ``BarValidator``/``SessionPolicy`` for intake and windowing,
* ``TrackStore`` for the hashed persistence contract (separate directory),
* ``DonchianBreakout`` (``strategies/research_candidates.py``) as the frozen
  deterministic signal source.

What is experiment-specific (and therefore isolated here):

* 15-minute decision cadence (see :mod:`window`/:mod:`contract`),
* CALL=long index / PUT=short index label-only legs (zero-premium),
* direction-aware protective stop (short-side anchor differs from the
  LONG-ONLY ``risk/stop_loss.py``, documented in ``contract``),
* its own store/report namespace tagged ``15M_DIRECTIONAL_OPTIONS_EXPERIMENT``.

No live trading can be reached from this module: the only broker constructed
is ``PaperBroker``, which hard-codes ``is_live=False``.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field
from datetime import datetime, date
from decimal import Decimal
from pathlib import Path

from fno_ai_paper_trading.broker.paper_broker import PaperBroker, PaperBrokerConfig
from fno_ai_paper_trading.config.settings import Environment, PaperSettings
from fno_ai_paper_trading.experiments.directional_15m.contract import (
    EXPERIMENT_ID,
    Action,
    Leg,
    decide,
    decision_moments,
)
from fno_ai_paper_trading.experiments.directional_15m.signal import donchian_signal_15m
from fno_ai_paper_trading.experiments.directional_15m.window import DecisionWindowAggregator
from fno_ai_paper_trading.models.enums import OrderSide
from fno_ai_paper_trading.models.instruments import Instrument
from fno_ai_paper_trading.models.market import MarketPrice
from fno_ai_paper_trading.models.order import Fill, Order
from fno_ai_paper_trading.paper_track.bars import BarValidator
from fno_ai_paper_trading.paper_track.clock import Clock, FixedClock
from fno_ai_paper_trading.paper_track.feed import TRACK_INSTRUMENT
from fno_ai_paper_trading.paper_track.policy import SessionPolicy
from fno_ai_paper_trading.paper_track.store import (
    TrackStore,
    fill_from_dict,
    fill_to_dict,
    market_price_from_dict,
    market_price_to_dict,
    stable_dumps,
)
from fno_ai_paper_trading.portfolio.portfolio import Portfolio
from fno_ai_paper_trading.risk.manager import RiskManager
from fno_ai_paper_trading.risk.sizer import RiskBasedPositionSizer, SizerConfig
from fno_ai_paper_trading.utils.functions import new_id

__all__ = ["DayAlreadyReported", "DirectionalOptionsConfig", "DirectionalOptionsEngine"]


class DayAlreadyReported(RuntimeError):
    """Raised when a session day already produced a report and must not re-run."""


@dataclass
class DirectionalOptionsConfig:
    """Paper-only configuration for one isolated experiment account."""

    account: str = "15m_directional_options"
    store_dir: Path = Path("data") / "experiments" / "15m_directional_options"
    instrument: Instrument = None  # type: ignore[assignment]
    interval: str = "5m"
    policy: SessionPolicy = dataclass_field(default_factory=SessionPolicy)
    initial_cash: Decimal = Decimal("100000")
    max_position_quantity: int = 75
    max_order_notional: Decimal = Decimal("250000")
    max_daily_loss: Decimal = Decimal("10000")
    commission_rate: Decimal = Decimal("0.0003")
    commission_fixed: Decimal = Decimal("0")
    slippage_rate: Decimal = Decimal("0.001")
    risk_per_trade_pct: Decimal = Decimal("0.01")
    stop_loss_pct: Decimal = Decimal("0.02")
    entry_channel: int = 20
    exit_channel: int = 10
    experiment_id: str = EXPERIMENT_ID

    def __post_init__(self) -> None:
        if self.instrument is None:
            self.instrument = TRACK_INSTRUMENT()
        self.store_dir = Path(self.store_dir)
        self.policy = self.policy if self.policy is not None else SessionPolicy()

    @property
    def symbol(self) -> str:
        return self.instrument.symbol


class DirectionalOptionsEngine:
    """One isolated experiment account, driven at every session tick."""

    def __init__(
        self,
        config: DirectionalOptionsConfig | None = None,
        *,
        store: TrackStore | None = None,
        clock: Clock | None = None,
        bars_source=None,
        failpoints: set[str] | None = None,
        run_id: str | None = None,
    ) -> None:
        self.config = config if config is not None else DirectionalOptionsConfig()
        self.store = store if store is not None else TrackStore(
            self.config.store_dir, self.config.account
        )
        self.clock = clock if clock is not None else FixedClock(datetime(1970, 1, 1))
        self.bars_source = bars_source
        self.failpoints = set(failpoints or ())
        self.run_id = run_id or f"exp-{datetime.now():%Y%m%d}-{new_id('RUN')[4:]}"

        settings = PaperSettings(
            environment=Environment.PAPER,
            initial_capital=self.config.initial_cash,
            max_position_quantity=self.config.max_position_quantity,
            max_order_notional=self.config.max_order_notional,
            max_daily_loss=self.config.max_daily_loss,
            commission_rate=self.config.commission_rate,
            commission_fixed=self.config.commission_fixed,
            slippage_rate=self.config.slippage_rate,
        )
        broker_config = PaperBrokerConfig(
            commission_rate=self.config.commission_rate,
            commission_fixed=self.config.commission_fixed,
            slippage_rate=self.config.slippage_rate,
        )
        # The only broker this engine can ever instantiate is PaperBroker,
        # which hard-codes ``is_live = False``.
        self.broker = PaperBroker(config=broker_config, now_fn=self.clock.now)
        self.portfolio = Portfolio(cash=self.config.initial_cash, long_only=False)
        self.risk_manager = RiskManager(settings)
        self.sizer = RiskBasedPositionSizer(
            SizerConfig(
                risk_per_trade_pct=self.config.risk_per_trade_pct,
                stop_loss_pct=self.config.stop_loss_pct,
                commission_rate=self.config.commission_rate,
                commission_fixed=self.config.commission_fixed,
            )
        )
        from fno_ai_paper_trading.experiments.directional_15m.contract import (
            DirectionalStopPolicy,
        )

        self.stop_policy = DirectionalStopPolicy(self.config.stop_loss_pct)
        self.validator = BarValidator(
            interval=self.config.interval,
            policy=self.config.policy,
            expected_symbol=self.config.instrument.symbol,
        )
        self.aggregator = DecisionWindowAggregator(
            bar_minutes=5, window_bars=3, decision_minutes=15
        )
        self.fills_meta: dict[str, str] = {}

        self.day: date | None = None
        self.last_processed: datetime | None = None
        self.leg: Leg = Leg.FLAT
        self._open_since: datetime | None = None
        self.equity_curve: list[Decimal] = [self.config.initial_cash]
        self.max_drawdown: Decimal = Decimal("0")
        self.eod_status: str = "NONE"
        self.entry_approvals: list[dict] = []
        self.decisions: list[dict] = []
        self.closures: list[dict] = []
        self._finalized_days: set[str] = set()
        self.counters = {
            "decision_points": 0,
            "signal_counts": {"BULLISH": 0, "BEARISH": 0, "NEUTRAL": 0},
            "entries_filled": 0,
            "exits_filled": 0,
            "stops_fired": 0,
            "flatten_count": 0,
            "closed_legs": 0,
            "reversals": 0,  # switches CALL<->PUT at a decision point
            "sizing_skips": [],
            "risk_refusals": [],
            "poisoned": [],
            "anomalies": [],
            "data_errors": [],
        }
        self._decision_moments: dict[str, set[datetime]] = {}
        self._session_symbol = self.config.instrument.symbol

    # ------------------------------------------------------------------ accessors

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
            from fno_ai_paper_trading.paper_track.errors import InjectedFailure

            raise InjectedFailure(point)

    # --------------------------------------------------------------- tick drive

    def step(self) -> str:
        """Process one clock instant (a session tick)."""
        now = self.clock.now()
        self._sync_day(now)
        self._attach_bars(now)
        if self.day is not None and now in self._moments_for(self.day):
            self._process_decision(now)
        self._maybe_flatten(now)
        self._maybe_finalize(now)
        return self._tick_summary(now)

    def _sync_day(self, now: datetime) -> None:
        if self.day == now.date():
            return
        if self.day is not None and not self._is_finalized(self.day):
            # cross-day without prior finalize: belt-and-suspenders flatten.
            self._maybe_flatten(now)
            self._maybe_finalize(now)
        self.day = now.date()
        self._decision_moments = {}
        self.eod_status = "NONE"

    def _moments_for(self, day: date) -> set[datetime]:
        key = day.isoformat()
        if key not in self._decision_moments:
            self._decision_moments[key] = set(decision_moments(day))
        return self._decision_moments[key]

    def _attach_bars(self, now: datetime) -> None:
        if self.bars_source is None:
            return
        raw = self.bars_source.bars_up_to(now)
        validated = self.validator.validate(raw, now=now, last_processed=self.last_processed)
        for bar, reason in zip(validated.poisoned, validated.poison_reasons):
            self.counters["poisoned"].append(
                {"timestamp": _iso(bar.timestamp), "reason": reason}
            )
        self.counters["anomalies"].extend(validated.anomalies or [])
        for bar in validated.valid:
            if self.last_processed is not None and bar.timestamp <= self.last_processed:
                continue
            self.last_processed = bar.timestamp
            if not self.aggregator.ingest(bar):
                self.counters["data_errors"].append(
                    f"bar {bar.timestamp:%Y-%m-%d %H:%M} rejected by aggregator"
                )

    # ------------------------------------------------------------- decisions

    def _process_decision(self, now: datetime) -> None:
        result = self.aggregator.decision_at(now)
        if not result.complete:
            self.counters["data_errors"].append(result.note)
            self._mark_equity(now, self._last_close(now))
            return
        bar = result.reference_bar
        if bar is None:  # pragma: no cover
            return

        # 1) Protective directional stop first (at the decision point only).
        stop_filled: list[str] = []
        if self.leg is not Leg.FLAT:
            pos = self.portfolio.position_for(self.symbol)
            if pos is not None and pos.quantity != 0:
                check = self.stop_policy.check(
                    leg=self.leg, entry_price=pos.average_entry_price, bar=bar
                )
                if check.triggered:
                    trade = self._exit_leg(self.leg, bar, now, kind="STOP")
                    if trade is not None:
                        self.counters["stops_fired"] += 1
                        stop_filled.append(self.leg.value)

        # 2) Signal applies from the resulting state.
        signal15m, _raw = donchian_signal_15m(
            self.aggregator.history,
            entry_channel=self.config.entry_channel,
            exit_channel=self.config.exit_channel,
        )
        decision = decide(self.leg, signal15m)
        state_before = self.leg
        new_fill_ids: list[str] = []
        for action in decision.actions:
            new_fill_ids.extend(self._apply_action(action, bar, now))
        state_after = self.leg

        record = {
            "moment": now.isoformat(),
            "signal": signal15m.value,
            "state_before": state_before.value,
            "state_after": state_after.value,
            "actions": [action.value for action in decision.actions],
            "reason": decision.reason,
            "fills": new_fill_ids,
        }
        self.decisions.append(record)
        self.counters["decision_points"] += 1
        self.counters["signal_counts"][signal15m.value] += 1
        self._mark_equity(now, bar.close)

    def _apply_action(self, action: Action, bar: MarketPrice, now: datetime) -> list[str]:
        prior_len = len(self.broker.fills)
        if action is Action.ENTER_CALL:
            self._enter_leg(Leg.CALL, bar, now)
        elif action is Action.ENTER_PUT:
            self._enter_leg(Leg.PUT, bar, now)
        elif action is Action.EXIT_CALL:
            self._exit_leg(Leg.CALL, bar, now, kind="signal")
        elif action is Action.EXIT_PUT:
            self._exit_leg(Leg.PUT, bar, now, kind="signal")
        elif action is Action.SWITCH_CALL_TO_PUT:
            self._exit_leg(Leg.CALL, bar, now, kind="signal")
            self.counters["reversals"] += 1
            self._enter_leg(Leg.PUT, bar, now)
        elif action is Action.SWITCH_PUT_TO_CALL:
            self._exit_leg(Leg.PUT, bar, now, kind="signal")
            self.counters["reversals"] += 1
            self._enter_leg(Leg.CALL, bar, now)
        return [fill.order_id for fill in self.broker.fills[prior_len:]]

    def _enter_leg(self, leg: Leg, bar: MarketPrice, now: datetime) -> list[str]:
        close = bar.close
        sizing = self.sizer.size(
            equity=self.portfolio.total_value({self.symbol: close}),
            available_cash=self.portfolio.cash,
            entry_price=close,
            instrument=self.config.instrument,
            current_quantity=0,
        )
        if not sizing.approved:
            self.counters["sizing_skips"].append(
                {"ts": now.isoformat(), "leg": leg.value, "reason": sizing.skip_reason}
            )
            return []
        side = OrderSide.BUY if leg is Leg.CALL else OrderSide.SELL
        order = Order(
            instrument=self.config.instrument, side=side, quantity=sizing.quantity
        )
        risk = self.risk_manager.evaluate(
            order,
            self.portfolio,
            close,
            realized_today=self.portfolio.realized_pnl_today(now.date()),
        )
        if not risk.approved:
            self.counters["risk_refusals"].append(
                {
                    "ts": now.isoformat(),
                    "leg": leg.value,
                    "side": side.value,
                    "quantity": sizing.quantity,
                    "close": str(close),
                    "reason": risk.summary,
                }
            )
            return []
        self._fault("E")
        fill = self.broker.place_order(order, bar)
        self._fault("F")
        if fill is None:
            self.counters["data_errors"].append(
                f"broker declined {leg.value} entry fill at {now:%H:%M}"
            )
            return []
        self.portfolio.apply_fill(fill)
        self.fills_meta[fill.order_id] = leg.value
        self.entry_approvals.append(
            {
                "bar_timestamp": bar.timestamp.isoformat(),
                "filled_at": fill.filled_at.isoformat(),
                "quantity": fill.quantity,
                "close": str(close),
                "leg": leg.value,
            }
        )
        self.leg = leg
        self._open_since = fill.filled_at
        self.counters["entries_filled"] += 1
        return [fill.order_id]

    def _exit_leg(self, leg: Leg, bar: MarketPrice, now: datetime, *, kind: str):
        pos = self.portfolio.position_for(self.symbol)
        if pos is None or pos.quantity == 0:
            return None
        qty = abs(pos.quantity)
        side = OrderSide.SELL if leg is Leg.CALL else OrderSide.BUY
        order = Order(instrument=self.config.instrument, side=side, quantity=qty)
        self._fault("E")
        fill = self.broker.place_order(order, bar)
        self._fault("F")
        if fill is None:
            self.counters["data_errors"].append(
                f"{kind} exit did not fill for {leg.value} at {now:%H:%M}"
            )
            return None
        trade = self.portfolio.apply_fill(fill)
        self.fills_meta[fill.order_id] = leg.value
        self.counters["exits_filled"] += 1
        self.counters["closed_legs"] += 1
        opened_at = self._open_since or fill.filled_at
        minutes = _minutes_between(opened_at, fill.filled_at)
        self.closures.append(
            {
                "leg": leg.value,
                "kind": kind,
                "entered_at": opened_at.isoformat(),
                "exited_at": fill.filled_at.isoformat(),
                "minutes": minutes,
                "quantity": qty,
                "realized": str(trade.realized_pnl),
            }
        )
        if kind == "EOD_FLATTEN":
            self.counters["flatten_count"] += 1
            self.eod_status = "FLATTENED"
        self.leg = Leg.FLAT
        self._open_since = None
        return trade

    # ----------------------------------------------------------------- housekeeping

    def _maybe_flatten(self, now: datetime) -> None:
        if self.day is None or self.eod_status != "NONE":
            return
        if not (self.config.policy.flatten_time <= now.time() < self.config.policy.window_close):
            return
        if self.leg is Leg.FLAT:
            self.eod_status = "FLAT"
            return
        done = self.aggregator.completed_by(now)
        bar = done[-1] if done else None
        if bar is None:
            self.counters["data_errors"].append(
                f"EOD flatten at {now:%H:%M} with no reference candle"
            )
            return
        self._exit_leg(self.leg, bar, now, kind="EOD_FLATTEN")

    def _maybe_finalize(self, now: datetime) -> None:
        if self.day is None:
            return
        if self._is_finalized(self.day):
            return
        if now.time() < self.config.policy.window_close:
            return
        if self.eod_status == "NONE" and self.leg is not Leg.FLAT:
            self._maybe_flatten(now)
        from fno_ai_paper_trading.experiments.directional_15m.report import (
            build_experiment_report,
        )

        report = build_experiment_report(self)
        self.store.save_report(self.day, report)
        self._finalized_days.add(self.day.isoformat())
        self.store.save_checkpoint(self.day, self.checkpoint_payload())
        self.store.update_run(
            self.run_id,
            last_day=self.day.isoformat(),
            fingerprint=report["fingerprint"],
        )
        self._finalized_days.add(self.day.isoformat())

    def _mark_equity(self, now: datetime, mark: Decimal | None) -> None:
        if mark is None:
            return
        value = self.portfolio.total_value({self.symbol: mark})
        self.equity_curve.append(value)
        peak = max(self.equity_curve)
        dd = peak - value
        if dd > self.max_drawdown:
            self.max_drawdown = dd

    def _last_close(self, now: datetime) -> Decimal | None:
        done = self.aggregator.completed_by(now)
        return done[-1].close if done else None

    def _tick_summary(self, now: datetime) -> str:
        return (
            f"{now:%Y-%m-%d %H:%M} leg={self.leg.value} "
            f"qty={self.position_quantity} eod={self.eod_status} "
            f"fills={len(self.broker.fills)}"
        )

    def _is_finalized(self, day: date) -> bool:
        return day.isoformat() in self._finalized_days

    # -------------------------------------------------------------- persistence

    def report(self) -> dict | None:
        """A live, deterministic report for the current run."""
        from fno_ai_paper_trading.experiments.directional_15m.report import (
            build_experiment_report,
        )

        if self.day is None and not self.closures and not self.broker.fills:
            return None
        return build_experiment_report(self)

    def stable_fingerprint(self) -> str:
        """Economic fingerprint of the run (rerun-comparable, id-free)."""
        import hashlib

        payload = {
            "experiment_id": EXPERIMENT_ID,
            "days": sorted(self._finalized_days),
            "fills": [
                [self.fills_meta.get(f.order_id, ""), f.side.value, f.quantity, str(f.price), str(f.commission)]
                for f in self.broker.fills
            ],
            "closures": [
                [c["leg"], c["kind"], c["minutes"], c["realized"]] for c in self.closures
            ],
            "decisions": [
                [d["moment"], d["signal"], d["state_before"], d["state_after"], d["actions"]]
                for d in self.decisions
            ],
            "cash": str(self.portfolio.cash),
            "realized": str(self.portfolio.realized_pnl),
        }
        return hashlib.sha256(stable_dumps(payload).encode("utf-8")).hexdigest()

    def checkpoint_payload(self) -> dict:
        fills = []
        for fill in self.broker.fills:
            item = fill_to_dict(fill)
            item["leg"] = self.fills_meta.get(fill.order_id, "")
            fills.append(item)
        return {
            "schema": "15m_directional_options.checkpoint",
            "version": 1,
            "experiment_id": EXPERIMENT_ID,
            "run_id": self.run_id,
            "day": self.day.isoformat() if self.day else None,
            "last_processed": _iso(self.last_processed),
            "leg": self.leg.value,
            "open_since": _iso(self._open_since),
            "eod_status": self.eod_status,
            "fills": fills,
            "history": [market_price_to_dict(bar) for bar in self.aggregator.history],
            "equity_curve": [str(v) for v in self.equity_curve],
            "max_drawdown": str(self.max_drawdown),
            "decisions": self.decisions,
            "closures": self.closures,
            "entry_approvals": self.entry_approvals,
            "counters": self.counters,
            "finalized_days": sorted(self._finalized_days),
        }

    def load(self, day: date | None = None) -> bool:
        """Restore the account from the checkpoint for ``day`` (or the latest)."""
        target = day
        if target is None:
            target = self.day
        if target is None:
            days = sorted(
                p.name.split(".")[-2]
                for p in self.store.checkpoints_dir.glob(f"{self.config.account}.*.json")
            )
            if not days:
                return False
            target = date.fromisoformat(days[-1])
            self.day = target
        payload = self.store.load_checkpoint(target)
        if payload is None:
            return False

        self.day = date.fromisoformat(payload["day"]) if payload.get("day") else None
        if payload.get("last_processed"):
            self.last_processed = datetime.fromisoformat(payload["last_processed"])
        self.leg = Leg(payload["leg"])
        if payload.get("open_since"):
            self._open_since = datetime.fromisoformat(payload["open_since"])
        self.eod_status = payload.get("eod_status", "NONE")

        self.portfolio = Portfolio(cash=self.config.initial_cash, long_only=False)
        broker = PaperBroker(
            config=PaperBrokerConfig(
                commission_rate=self.config.commission_rate,
                commission_fixed=self.config.commission_fixed,
                slippage_rate=self.config.slippage_rate,
            ),
            now_fn=self.clock.now,
        )
        self.fills_meta = {}
        restored: list[Fill] = []
        for item in payload.get("fills", []):
            fill = fill_from_dict(item)
            restored.append(fill)
            self.fills_meta[fill.order_id] = item.get("leg", "")
            self.portfolio.apply_fill(fill)
        broker.restore([], restored)
        self.broker = broker

        self.aggregator = DecisionWindowAggregator(
            bar_minutes=5, window_bars=3, decision_minutes=15
        )
        for bar_data in payload.get("history", []):
            self.aggregator.ingest(market_price_from_dict(bar_data))

        self.equity_curve = [Decimal(str(v)) for v in payload.get("equity_curve", [])]
        self.max_drawdown = Decimal(str(payload.get("max_drawdown", "0")))
        self.decisions = list(payload.get("decisions", []))
        self.closures = list(payload.get("closures", []))
        self.entry_approvals = list(payload.get("entry_approvals", []))
        self.counters = payload.get("counters", self.counters)
        self._finalized_days = set(payload.get("finalized_days", []))
        self.counters.setdefault("reversals", 0)
        return True


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _minutes_between(start: datetime, end: datetime) -> int:
    minutes = int((end - start).total_seconds() // 60)
    return max(minutes, 0)