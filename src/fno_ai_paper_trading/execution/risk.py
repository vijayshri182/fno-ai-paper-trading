"""Pre-trade preflight for the live execution test (WS 7.9).

The manager must re-verify every existing control immediately before any order:
the market session must be OPEN, the existing ``RiskManager`` must approve the
exact lot-size order at the current reference price, the existing ``Watchdog``
must not be in STOP, the margin estimate must fit the configured cap, and the
entry window must leave enough time for the mandatory 5-minute hold plus a
finish buffer before the 15:30 IST close (no overnight position). Every check
failure aborts the run with *no order*.

This module never places orders and never disables an existing control — it only
reads state and re-applies the authoritative rules of §17e/§17k/§17p.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Callable, Mapping

from fno_ai_paper_trading.alerting.health import SafetyDecision, Watchdog
from fno_ai_paper_trading.config.settings import PaperSettings
from fno_ai_paper_trading.models.enums import MarketPhase, OrderSide
from fno_ai_paper_trading.models.market import MarketSession
from fno_ai_paper_trading.models.order import Order
from fno_ai_paper_trading.portfolio.portfolio import Portfolio
from fno_ai_paper_trading.risk.manager import RiskManager

#: At least this much slack (seconds) must remain before 15:30 IST after the
#: mandatory hold so the exit + reconciliation finishes inside the session.
FINALIZATION_BUFFER_SECONDS = 900.0  # 15 minutes


@dataclass(frozen=True)
class PreflightDecision:
    """Result of the combined pre-trade gate: pass or the exact reasons."""

    ok: bool
    reasons: tuple[str, ...] = field(default_factory=tuple)

    @property
    def summary(self) -> str:
        if self.ok:
            return "preflight approved"
        return "preflight rejected: " + "; ".join(self.reasons)


class OvernightGuard:
    """Rejects entries that could not be flattened before the session close."""

    def __init__(
        self,
        close_time: datetime,
        hold_seconds: float = 300.0,
        finalization_buffer_seconds: float = FINALIZATION_BUFFER_SECONDS,
    ) -> None:
        if not isinstance(close_time, datetime):
            raise TypeError("close_time must be a datetime")
        self._close = close_time
        self._hold = float(hold_seconds)
        self._buffer = float(finalization_buffer_seconds)

    def allows_entry(self, now: datetime) -> tuple[bool, str]:
        deadline = self._close - timedelta(seconds=self._hold + self._buffer)
        if now >= self._close:
            return False, f"market already closed at {self._close.isoformat(timespec='minutes')}"
        if now > deadline:
            return False, (
                "entry window too late: hold + finalization would run past close "
                f"({self._close.isoformat(timespec='minutes')})"
            )
        return True, "overnight-position guard passed"


class RiskPreflight:
    """Re-applies RiskManager + Watchdog + market/margin guards before an order."""

    def __init__(
        self,
        settings: PaperSettings,
        risk_manager: RiskManager,
        watchdog: Watchdog,
        max_margin_notional: Decimal,
    ) -> None:
        self.settings = settings
        self.risk_manager = risk_manager
        self.watchdog = watchdog
        self.max_margin_notional = Decimal(max_margin_notional)

    def evaluate(
        self,
        *,
        instrument,
        side: OrderSide,
        quantity: int,
        reference_price: Decimal,
        premium: Decimal,
        margin_fn: Callable[[Decimal], Decimal],
        session: MarketSession,
        latest_bar_time: datetime,
        components: Mapping[str, bool],
        now: datetime,
        realized_today: Decimal = Decimal("0"),
        hold_seconds: float = 300.0,
        finalization_buffer_seconds: float = FINALIZATION_BUFFER_SECONDS,
    ) -> PreflightDecision:
        """Return pass/fail with every discrete reason the trade must satisfy."""
        reasons: list[str] = []

        # 1. Market must actually be open.
        if session is None or session.phase is not MarketPhase.OPEN or not session.is_open:
            reasons.append(
                "market not open (phase="
                f"{(session.phase.value if session else 'none')})"
            )

        # 2. The existing RiskManager must approve the exact order and price.
        risk_order = Order(instrument=instrument, side=side, quantity=int(quantity))
        snapshot = Portfolio(cash=self.settings.initial_capital)
        risk_decision = self.risk_manager.evaluate(
            risk_order, snapshot, reference_price, realized_today=realized_today
        )
        if not risk_decision.approved:
            reasons.append(f"RiskManager: {risk_decision.summary}")

        # 3. The existing Watchdog must not be in STOP.
        report = self.watchdog.evaluate(
            latest_bar_time=latest_bar_time, components=components
        )
        safety = self.watchdog.safety(report)
        if safety.decision is SafetyDecision.STOP:
            reasons.append(
                f"watchdog STOP: {safety.instruction} — {', '.join(safety.reasons)}"
            )

        # 4. Required margin must fit the configured cap.
        try:
            required = margin_fn(premium)
        except Exception as exc:  # noqa: BLE001 - a margin failure means no order
            reasons.append(f"margin estimate failed: {exc}")
        else:
            if required > self.max_margin_notional:
                reasons.append(
                    f"required margin {required} exceeds cap {self.max_margin_notional}"
                )

        # 5. No overnight position: enough slack must remain before the close.
        if session is not None and session.close_time is not None:
            guard = OvernightGuard(
                session.close_time,
                hold_seconds=hold_seconds,
                finalization_buffer_seconds=finalization_buffer_seconds,
            )
            ok, message = guard.allows_entry(now)
            if not ok:
                reasons.append(f"overnight guard: {message}")

        return PreflightDecision(ok=not reasons, reasons=tuple(reasons))