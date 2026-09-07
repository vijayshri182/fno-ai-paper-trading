"""Backtest engine: deterministic strategy replay with simulated paper execution.

The engine is the only place a strategy meets a broker during backtesting.
It reuses :class:`PaperBrokerConfig` for cost computation but overrides the
fill-timestamp logic so that every fill is stamped with the bar's timestamp
rather than ``datetime.now()`` — this is critical for deterministic,
reproducible results and for correct daily risk-limit calculations.

No live broker, no live API, no live order placement.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from fno_ai_paper_trading.broker.paper_broker import PaperBroker, PaperBrokerConfig
from fno_ai_paper_trading.config.settings import Environment, PaperSettings
from fno_ai_paper_trading.models.enums import OrderSide, OrderStatus
from fno_ai_paper_trading.models.market import MarketPrice
from fno_ai_paper_trading.models.order import Fill, Order
from fno_ai_paper_trading.portfolio.portfolio import Portfolio
from fno_ai_paper_trading.risk.manager import RiskManager
from fno_ai_paper_trading.strategies.base import Strategy
from fno_ai_paper_trading.utils.functions import new_id

from fno_ai_paper_trading.backtest.config import BacktestConfig
from fno_ai_paper_trading.backtest.result import BacktestResult, EquityPoint


# ---------------------------------------------------------------------------
# BacktestBroker — fills with caller-supplied timestamps
# ---------------------------------------------------------------------------

class BacktestBroker(PaperBroker):
    """PaperBroker variant that stamps fills with a caller-provided timestamp.

    Optionally accepts a duck-typed ``cost_schedule`` (any object with
    ``compute(side, notional, quantity) -> obj.total``). When present, the full
    fill charge comes from the schedule instead of the legacy
    commission-rate/fixed model.

    This is an implementation detail of the backtest engine and must not be
    used outside the backtest package.
    """

    def __init__(
        self,
        config: PaperBrokerConfig | None = None,
        cost_schedule: object | None = None,
    ) -> None:
        super().__init__(config)
        self._fill_timestamp: datetime | None = None
        self.cost_schedule = cost_schedule

    def set_timestamp(self, ts: datetime) -> None:
        self._fill_timestamp = ts

    def place_order(self, order: Order, market_price: MarketPrice | None = None) -> Fill | None:
        if market_price is None:
            order.status = OrderStatus.REJECTED
            order.rejection_reason = "no market price available for paper fill"
            return None

        ts = self._fill_timestamp or datetime.now()

        order.order_id = new_id("ORD")
        order.created_at = ts
        order.submitted_at = ts
        order.status = OrderStatus.SUBMITTED

        fill_price = self._apply_slippage(market_price.close, order.side)
        commission = self._resolve_commission(order, fill_price)

        order.filled_quantity = order.quantity
        order.average_fill_price = fill_price
        order.status = OrderStatus.FILLED
        order.filled_at = ts

        fill = Fill(
            order_id=order.order_id,
            instrument=order.instrument,
            side=order.side,
            quantity=order.quantity,
            price=fill_price,
            commission=commission,
            filled_at=ts,
        )
        self._orders[order.order_id] = order
        self._fills.append(fill)
        return fill

    def _resolve_commission(self, order: Order, fill_price: Decimal) -> Decimal:
        """Full fill charge: cost-schedule total if configured, else legacy model."""
        if self.cost_schedule is not None:
            notional = fill_price * order.quantity * order.instrument.multiplier
            return self.cost_schedule.compute(
                side=order.side, notional=notional, quantity=order.quantity
            ).total
        return self._compute_commission(order, fill_price)


# ---------------------------------------------------------------------------
# BacktestEngine
# ---------------------------------------------------------------------------

class BacktestEngine:
    """Runs a strategy over a chronological bar series with paper execution.

    Guarantees:
    - Strategy only sees ``bars[:i+1]`` at bar index ``i`` (no look-ahead).
    - All fills are stamped with the bar's timestamp (deterministic).
    - Only :class:`PaperBroker` / :class:`Portfolio` are used — no live
      execution path exists.
    """

    def run(
        self,
        bars: list[MarketPrice],
        strategy: Strategy,
        config: BacktestConfig | None = None,
    ) -> BacktestResult:
        config = config or BacktestConfig()
        slippage = (
            config.execution.total_adverse_rate
            if config.execution is not None
            else config.slippage_rate
        )
        broker = BacktestBroker(
            PaperBrokerConfig(
                commission_rate=config.commission_rate,
                commission_fixed=config.commission_fixed,
                slippage_rate=slippage,
            ),
            cost_schedule=config.cost_schedule,
        )
        portfolio = Portfolio(config.initial_capital)
        risk_manager = self._build_risk_manager(config)

        equity_curve: list[EquityPoint] = []
        peak_equity = config.initial_capital
        signals_generated = 0
        orders_submitted = 0
        orders_filled = 0
        closed_trades: list = []
        slippage_cost = Decimal("0")

        for i, bar in enumerate(bars):
            # --- strategy sees only past + present ---
            signal = strategy.analyze(bars[: i + 1])

            if signal.actionable and signal.instrument is not None:
                signals_generated += 1
                side = OrderSide.BUY if signal.signal.value == "BUY" else OrderSide.SELL
                order = Order(instrument=bar.instrument, side=side, quantity=config.quantity)

                approved = True
                if risk_manager is not None:
                    realized_today = self._realized_on_date(portfolio, bar.timestamp)
                    decision = risk_manager.evaluate(order, portfolio, bar.close, realized_today=realized_today)
                    approved = decision.approved

                if approved:
                    orders_submitted += 1
                    broker.set_timestamp(bar.timestamp)
                    fill = broker.place_order(order, bar)
                    if fill is not None:
                        old_qty = portfolio.current_quantity(fill.instrument.symbol)
                        trade = portfolio.apply_fill(fill)
                        orders_filled += 1
                        slippage_cost += abs(fill.price - bar.close) * fill.quantity * fill.instrument.multiplier
                        if self._is_closing_fill(old_qty, order.quantity, side):
                            closed_trades.append(trade)

            # --- equity snapshot at this bar's close ---
            equity_point = self._snapshot(portfolio, bar, i, peak_equity)
            equity_curve.append(equity_point)
            peak_equity = max(peak_equity, equity_point.equity)

        return self._build_result(config, portfolio, equity_curve, bars, signals_generated,
                                  orders_submitted, orders_filled, closed_trades, slippage_cost)

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_risk_manager(config: BacktestConfig) -> RiskManager | None:
        if not config.enable_risk_manager:
            return None
        settings = PaperSettings(
            environment=Environment.TEST,
            initial_capital=config.initial_capital,
            max_position_quantity=config.max_position_quantity,
            max_order_notional=config.max_order_notional,
            max_daily_loss=config.max_daily_loss,
            commission_rate=config.commission_rate,
            commission_fixed=config.commission_fixed,
            slippage_rate=config.slippage_rate,
        )
        return RiskManager(settings)

    @staticmethod
    def _realized_on_date(portfolio: Portfolio, ts: datetime) -> Decimal:
        """Sum realized P&L for trades on the same date as *ts*."""
        target = ts.date()
        return sum(
            (t.realized_pnl for t in portfolio.trade_history if t.executed_at.date() == target),
            Decimal("0"),
        )

    @staticmethod
    def _is_closing_fill(old_qty: int, fill_qty: int, side: OrderSide) -> bool:
        """True when this fill reduces (or reverses) an existing open position."""
        if old_qty == 0:
            return False
        delta = fill_qty if side == OrderSide.BUY else -fill_qty
        return abs(old_qty + delta) < abs(old_qty)

    @staticmethod
    def _snapshot(
        portfolio: Portfolio,
        bar: MarketPrice,
        bar_index: int,
        peak_equity: Decimal,
    ) -> EquityPoint:
        prices = {bar.instrument.symbol: bar.close}
        equity = portfolio.total_value(prices)
        unrealized = portfolio.unrealized_pnl(prices)
        drawdown = peak_equity - equity if equity < peak_equity else Decimal("0")
        return EquityPoint(
            timestamp=bar.timestamp,
            bar_index=bar_index,
            equity=equity,
            cash=portfolio.cash,
            unrealized_pnl=unrealized,
            drawdown_from_peak=drawdown,
        )

    @staticmethod
    def _build_result(
        config: BacktestConfig,
        portfolio: Portfolio,
        equity_curve: list[EquityPoint],
        bars: list[MarketPrice],
        signals_generated: int,
        orders_submitted: int,
        orders_filled: int,
        closed_trades: list | None = None,
        slippage_cost: Decimal = Decimal("0"),
    ) -> BacktestResult:
        final_equity = equity_curve[-1].equity if equity_curve else config.initial_capital
        total_pnl = final_equity - config.initial_capital
        total_return_pct = (
            (total_pnl / config.initial_capital * Decimal("100")) if config.initial_capital > 0 else Decimal("0")
        )

        # Trade stats are based on closing fills only (round-trip semantics).
        if closed_trades is None:
            closed_trades = []
        num_trades = len(closed_trades)
        winning = sum(1 for t in closed_trades if t.realized_pnl > 0)
        losing = sum(1 for t in closed_trades if t.realized_pnl < 0)
        win_rate = (Decimal(winning) / Decimal(num_trades) * Decimal("100")) if num_trades > 0 else Decimal("0")

        gross_profit = sum((t.realized_pnl for t in closed_trades if t.realized_pnl > 0), Decimal("0"))
        gross_loss = sum((t.realized_pnl for t in closed_trades if t.realized_pnl < 0), Decimal("0"))

        if gross_loss < 0:
            profit_factor = (gross_profit / abs(gross_loss)) if gross_profit > 0 else Decimal("0")
        else:
            profit_factor = Decimal("Infinity") if gross_profit > 0 else Decimal("0")

        total_commission = sum((t.commission for t in portfolio.trade_history), Decimal("0"))

        max_drawdown = max((ep.drawdown_from_peak for ep in equity_curve), default=Decimal("0"))
        overall_peak = max((ep.equity for ep in equity_curve), default=config.initial_capital)
        max_drawdown_pct = (
            (max_drawdown / overall_peak * Decimal("100")) if overall_peak > 0 else Decimal("0")
        )

        return BacktestResult(
            initial_capital=config.initial_capital,
            final_equity=final_equity,
            total_pnl=total_pnl,
            total_return_pct=total_return_pct,
            num_bars_processed=len(bars),
            signals_generated=signals_generated,
            orders_submitted=orders_submitted,
            orders_filled=orders_filled,
            num_trades=num_trades,
            winning_trades=winning,
            losing_trades=losing,
            win_rate=win_rate,
            gross_profit=gross_profit,
            gross_loss=gross_loss,
            total_commission=total_commission,
            slippage_cost=slippage_cost,
            profit_factor=profit_factor,
            max_drawdown=max_drawdown,
            max_drawdown_pct=max_drawdown_pct,
            equity_curve=equity_curve,
            trades=list(portfolio.trade_history),
        )
