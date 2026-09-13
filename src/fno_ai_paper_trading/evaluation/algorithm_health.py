"""Algorithm health and paper-trading readiness assessment (ALGO READY / HEALTH).

An objective, evidence-only evaluator over *recorded closed trades*. Metrics are
computed per bucket and the buckets are NEVER mixed:

* ``backtest``      — off-line historical replay trades (design+validation),
                      before the protected OOS start.
* ``protected_oos`` — the single-use out-of-sample segment (untouched during
                      selection).
* ``paper``         — closed trades from the LIVE paper-session ledger.
* ``today``         — paper trades closed on the current IST trading date.
* ``rolling_recent``— the most recent N closed trades from the newest evidence
                      source (paper when available, else the protected OOS).

Nothing here is derived from a win-rate shortcut: health/readiness are decided
from objective, documented thresholds over sample size, expectancy, profit
factor, drawdown, losses streaks, OOS evidence and integrity flags. A high win
rate with negative expectancy can never be "GREEN". See
``docs/algorithm_ready_spec.md`` for the documented threshold contract.

The health monitor never places orders and never changes risk/execution code.
Real-money trading remains disabled. PAPER ONLY.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from statistics import StatisticsError, mean, stdev
from typing import Mapping, Sequence


# --------------------------------------------------------------------------
# Documented thresholds (contract in docs/algorithm_ready_spec.md).
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class HealthThresholds:
    """Objective decision thresholds. Never loosen these to manufacture health."""

    min_backtest_trades_green: int = 500
    min_oos_trades_green: int = 30
    min_bucket_trades_for_metrics: int = 10
    min_segment_trades_for_trend: int = 100
    min_paper_trades_for_ready: int = 10
    profit_factor_green: Decimal = Decimal("1.0")
    expectancy_zero: Decimal = Decimal("0")
    trend_tolerance_ratio: Decimal = Decimal("0.10")

    def to_dict(self) -> dict[str, object]:
        return {
            "min_backtest_trades_green": self.min_backtest_trades_green,
            "min_oos_trades_green": self.min_oos_trades_green,
            "min_bucket_trades_for_metrics": self.min_bucket_trades_for_metrics,
            "min_segment_trades_for_trend": self.min_segment_trades_for_trend,
            "min_paper_trades_for_ready": self.min_paper_trades_for_ready,
            "profit_factor_green": str(self.profit_factor_green),
            "expectancy_zero": str(self.expectancy_zero),
            "trend_tolerance_ratio": str(self.trend_tolerance_ratio),
        }


DEFAULT_THRESHOLDS = HealthThresholds()


# --------------------------------------------------------------------------
# Recorded trade unit.
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class TradeRecord:
    """One closed (round-trip) trade with realized P&L. Confidence is optional;
    the frozen MA(5,21) baseline emits no confidence (recorded as None)."""

    bucket: str
    strategy_name: str
    algorithm_version: str
    configuration_version: str
    entry_time: datetime
    exit_time: datetime
    side: str
    entry_price: Decimal
    exit_price: Decimal
    price_pnl: Decimal
    commission: Decimal
    net_pnl: Decimal
    confidence: Decimal | None = None
    exit_reason: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "bucket": self.bucket,
            "strategy_name": self.strategy_name,
            "algorithm_version": self.algorithm_version,
            "configuration_version": self.configuration_version,
            "entry_time": self.entry_time.isoformat(),
            "exit_time": self.exit_time.isoformat(),
            "side": self.side,
            "entry_price": str(self.entry_price),
            "exit_price": str(self.exit_price),
            "price_pnl": str(self.price_pnl),
            "commission": str(self.commission),
            "net_pnl": str(self.net_pnl),
            "confidence": str(self.confidence) if self.confidence is not None else None,
            "exit_reason": self.exit_reason,
        }


# --------------------------------------------------------------------------
# Computed per-bucket metrics.
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class TradeMetrics:
    """Metrics for one dataset bucket (computed from recorded trades only)."""

    bucket: str
    total_closed: int
    winning: int
    losing: int
    win_rate_pct: Decimal
    net_pnl: Decimal
    profit_factor: Decimal
    avg_win: Decimal
    avg_loss: Decimal
    expectancy: Decimal
    net_expectancy_t: Decimal | None
    max_drawdown: Decimal
    max_drawdown_pct: Decimal
    current_drawdown: Decimal
    consecutive_wins: int
    consecutive_losses: int
    last_10_win_rate: Decimal | None
    last_20_win_rate: Decimal | None
    today_win_rate: Decimal | None
    today_closed: int
    avg_confidence: Decimal | None
    sample_sufficient: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "bucket": self.bucket,
            "total_closed": self.total_closed,
            "winning": self.winning,
            "losing": self.losing,
            "win_rate_pct": str(self.win_rate_pct) if self.win_rate_pct is not None else None,
            "net_pnl": str(self.net_pnl),
            "profit_factor": str(self.profit_factor) if self.profit_factor is not None else None,
            "avg_win": str(self.avg_win) if self.avg_win is not None else None,
            "avg_loss": str(self.avg_loss) if self.avg_loss is not None else None,
            "expectancy": str(self.expectancy) if self.expectancy is not None else None,
            "net_expectancy_t": str(self.net_expectancy_t) if self.net_expectancy_t is not None else None,
            "max_drawdown": str(self.max_drawdown) if self.max_drawdown is not None else None,
            "max_drawdown_pct": str(self.max_drawdown_pct) if self.max_drawdown_pct is not None else None,
            "current_drawdown": str(self.current_drawdown) if self.current_drawdown is not None else None,
            "consecutive_wins": self.consecutive_wins,
            "consecutive_losses": self.consecutive_losses,
            "last_10_win_rate": str(self.last_10_win_rate) if self.last_10_win_rate is not None else None,
            "last_20_win_rate": str(self.last_20_win_rate) if self.last_20_win_rate is not None else None,
            "today_win_rate": str(self.today_win_rate) if self.today_win_rate is not None else None,
            "today_closed": self.today_closed,
            "avg_confidence": str(self.avg_confidence) if self.avg_confidence is not None else None,
            "sample_sufficient": self.sample_sufficient,
        }


def _ratio(numer: Decimal, denom: Decimal) -> Decimal | None:
    if denom is None or denom == 0:
        return None
    return numer / denom


def _pct(wins: int, total: int) -> Decimal | None:
    if total <= 0:
        return None
    return (Decimal(wins) / Decimal(total)) * Decimal("100")


def _win_rate_of(trades: Sequence[TradeRecord]) -> Decimal | None:
    return _pct(sum(1 for t in trades if t.net_pnl > 0), len(trades))


def _one_sample_t(values: Sequence[Decimal]) -> Decimal | None:
    """One-sample t-statistic of a per-trade P&L series against 0 (float maths;
    statistics only — money stays Decimal)."""
    if len(values) < 2:
        return None
    samples = [float(v) for v in values]
    try:
        sample_mean = mean(samples)
        sample_sd = stdev(samples)
    except StatisticsError:
        return None
    if sample_sd == 0:
        return None
    t = sample_mean / (sample_sd / (len(samples) ** 0.5))
    return Decimal(str(t))


def _cumulative(trades: Sequence[TradeRecord]) -> list[Decimal]:
    total = Decimal("0")
    curve: list[Decimal] = []
    for trade in trades:
        total += trade.net_pnl
        curve.append(total)
    return curve


def _drawdowns(sequence: Sequence[Decimal]) -> tuple[Decimal, Decimal, Decimal]:
    """Return (max_loss_amount, max_drawdown_pct, current_drawdown_amount).

    Drawdowns are measured on the closed-trade cumulative P&L (equity proxy);
    per-trade records carry no intra-trade equity. Documented simplification.
    """
    peak = Decimal("0")
    max_dd = Decimal("0")
    current_equity = sequence[-1] if sequence else Decimal("0")
    for eq in sequence:
        if eq > peak:
            peak = eq
        dd = peak - eq
        if dd > max_dd:
            max_dd = dd
    max_dd_pct = _ratio(max_dd, peak) * Decimal("100") if peak and peak > 0 else None
    current_dd = peak - current_equity if peak > 0 else Decimal("0")
    return max_dd, (max_dd_pct if max_dd_pct is not None else Decimal("0")), current_dd


def _streaks(trades: Sequence[TradeRecord]) -> tuple[int, int]:
    """Most recent consecutive wins and consecutive losses (ending streak)."""
    consecutive_wins = 0
    consecutive_losses = 0
    for trade in reversed(trades):
        if trade.net_pnl > 0:
            if consecutive_losses:
                break
            consecutive_wins += 1
        else:
            if consecutive_wins:
                break
            consecutive_losses += 1
    return consecutive_wins, consecutive_losses


def _rolling_win_rate(trades: Sequence[TradeRecord], n: int) -> Decimal | None:
    window = trades[-n:]
    if len(window) < n:
        return None
    return _win_rate_of(window)


def compute_trade_metrics(
    trades: Sequence[TradeRecord],
    *,
    bucket: str,
    thresholds: HealthThresholds = DEFAULT_THRESHOLDS,
    today_date: date | None = None,
) -> TradeMetrics:
    """Compute the metric set for one bucket of recorded trades."""
    ordered = list(trades)
    winning = [t for t in ordered if t.net_pnl > 0]
    losing = [t for t in ordered if t.net_pnl <= 0]
    net_pnl = sum((t.net_pnl for t in ordered), Decimal("0"))
    gross_profit = sum((t.net_pnl for t in winning), Decimal("0"))
    gross_loss = sum((t.net_pnl for t in losing), Decimal("0"))
    profit_factor = (
        _ratio(gross_profit, abs(gross_loss)) if gross_loss else None
    )
    avg_win = _ratio(gross_profit, Decimal(len(winning))) if winning else None
    avg_loss = _ratio(gross_loss, Decimal(len(losing))) if losing else None
    expectancy = _ratio(net_pnl, Decimal(len(ordered))) if ordered else None
    sequence = _cumulative(ordered)
    max_dd, max_dd_pct, current_dd = _drawdowns(sequence)
    confidences = [t.confidence for t in ordered if t.confidence is not None]
    avg_confidence = (
        (sum(confidences, Decimal("0")) / Decimal(len(confidences)))
        if confidences else None
    )
    today_trades = (
        [t for t in ordered if t.exit_time.date() == today_date]
        if today_date is not None else []
    )
    today_win_rate = _win_rate_of(today_trades)
    consec_wins, consec_losses = _streaks(ordered)
    return TradeMetrics(
        bucket=bucket,
        total_closed=len(ordered),
        winning=len(winning),
        losing=len(losing),
        win_rate_pct=_win_rate_of(ordered),
        net_pnl=net_pnl,
        profit_factor=profit_factor,
        avg_win=avg_win,
        avg_loss=avg_loss,
        expectancy=expectancy,
        net_expectancy_t=_one_sample_t([t.net_pnl for t in ordered]),
        max_drawdown=max_dd,
        max_drawdown_pct=max_dd_pct,
        current_drawdown=current_dd,
        consecutive_wins=consec_wins,
        consecutive_losses=consec_losses,
        last_10_win_rate=_rolling_win_rate(ordered, 10),
        last_20_win_rate=_rolling_win_rate(ordered, 20),
        today_win_rate=today_win_rate if bucket == "paper" else None,
        today_closed=len(today_trades) if bucket == "paper" else 0,
        avg_confidence=avg_confidence,
        sample_sufficient=len(ordered) >= thresholds.min_bucket_trades_for_metrics,
    )


def segment_expectancies(
    trades: Sequence[TradeRecord],
    *,
    min_trades: int = DEFAULT_THRESHOLDS.min_segment_trades_for_trend,
) -> list[tuple[str, int, Decimal | None]]:
    """Ordered by-time expectancy per calendar-year segment (oldest first).

    Returns tuples ``(segment_label, trade_count, expectancy)`` so the trend
    logic can require a minimum number of trades per segment. Segments with too
    few trades keep ``expectancy=None`` (insufficient).
    """
    ordered = sorted(trades, key=lambda t: (t.entry_time))
    by_year: dict[int, list[TradeRecord]] = {}
    for trade in ordered:
        by_year.setdefault(trade.entry_time.year, []).append(trade)
    segments: list[tuple[str, int, Decimal | None]] = []
    for year in sorted(by_year):
        year_trades = by_year[year]
        if len(year_trades) < min_trades:
            segments.append((str(year), len(year_trades), None))
        else:
            expectancy = _ratio(
                sum((t.net_pnl for t in year_trades), Decimal("0")),
                Decimal(len(year_trades)),
            )
            segments.append((str(year), len(year_trades), expectancy))
    return segments


# --------------------------------------------------------------------------
# Assessment.
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class IntegrityAlerts:
    """Non-P&L quality/abuse signals. Empty means fully clean."""

    data_quality_ok: bool = True
    data_quality_notes: tuple[str, ...] = ()
    risk_control_violations: tuple[str, ...] = ()
    simulation_anomalies: tuple[str, ...] = ()
    confidence_uncalibrated: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "data_quality_ok": self.data_quality_ok,
            "data_quality_notes": list(self.data_quality_notes),
            "risk_control_violations": list(self.risk_control_violations),
            "simulation_anomalies": list(self.simulation_anomalies),
            "confidence_uncalibrated": self.confidence_uncalibrated,
        }


@dataclass(frozen=True)
class HealthAssessment:
    """Overall algorithm health + readiness decision with reasons."""

    health: str  # GREEN / YELLOW / RED
    ready: str  # YES / MONITOR / NO
    performance_trend: str  # IMPROVING / STABLE / DETERIORATING / INSUFFICIENT DATA
    sample_size_ok: bool
    health_reason: str
    readiness_reason: str
    trend_reason: str
    version: str
    configuration_version: str
    thresholds: dict[str, object]
    buckets: dict[str, TradeMetrics] = field(default_factory=dict)
    segments: list[dict[str, object]] = field(default_factory=list)
    integrity: IntegrityAlerts = field(default_factory=IntegrityAlerts)
    generated_at: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "algorithm_health": self.health,
            "algo_ready": self.ready,
            "performance_trend": self.performance_trend,
            "sample_size_ok": self.sample_size_ok,
            "health_reason": self.health_reason,
            "readiness_reason": self.readiness_reason,
            "trend_reason": self.trend_reason,
            "algorithm_version": self.version,
            "configuration_version": self.configuration_version,
            "thresholds": self.thresholds,
            "buckets": {k: v.to_dict() for k, v in self.buckets.items()},
            "segments": list(self.segments),
            "integrity": self.integrity.to_dict(),
            "last_updated": self.generated_at,
        }


def _trend(
    segments: list[tuple[str, int, Decimal | None]],
    *,
    tolerance: Decimal = DEFAULT_THRESHOLDS.trend_tolerance_ratio,
) -> tuple[str, str]:
    """Determine performance trend from ordered expectancy segments.

    Compares the mean expectancy of the older half of the segments against the
    newer half (a middle segment is excluded when the count is odd). A relative
    change beyond ``tolerance`` is IMPROVING/DETERIORATING; otherwise STABLE.
    Fewer than three sufficiently-sized segments is INSUFFICIENT DATA.
    """
    expectancies: list[Decimal] = []
    for _label, count, expectancy in segments:
        if expectancy is None or count < DEFAULT_THRESHOLDS.min_segment_trades_for_trend:
            return "INSUFFICIENT DATA", "fewer than the minimum trades per time segment"
        expectancies.append(expectancy)
    if len(expectancies) < 3:
        return "INSUFFICIENT DATA", "fewer than three comparable time segments"
    half = len(expectancies) // 2
    older = expectancies[:half]
    newer = expectancies[-half:]
    older_mean = sum(older, Decimal("0")) / Decimal(len(older))
    newer_mean = sum(newer, Decimal("0")) / Decimal(len(newer))
    denom = abs(older_mean) if older_mean != 0 else (abs(newer_mean) or Decimal("1"))
    change = (newer_mean - older_mean) / denom
    if change > tolerance:
        return "IMPROVING", f"newer-half expectancy improved {change:.2%} vs older half"
    if change < -tolerance:
        return "DETERIORATING", f"newer-half expectancy worsened {abs(change):.2%} vs older half"
    return "STABLE", f"newer-half expectancy within +/-{tolerance:.0%} of older half"


def assess_algorithm_health(
    buckets: Mapping[str, Sequence[TradeRecord]],
    *,
    version: str,
    configuration_version: str,
    thresholds: HealthThresholds = DEFAULT_THRESHOLDS,
    integrity: IntegrityAlerts | None = None,
    today_date: date | None = None,
) -> HealthAssessment:
    """Compute the overall health/readiness/trend for a set of recorded buckets.

    Buckets are kept separate: the assessment reads ``backtest`` and
    ``protected_oos`` (and optionally ``paper``/``today``) independently and
    never pools them. Rolling-recent metrics are representative of the newest
    evidence source (paper when present, else protected OOS); they are stored in
    the ``rolling_recent`` bucket for display but already exist as windows on the
    underlying recorded source.
    """
    alerts = integrity or IntegrityAlerts()
    metrics: dict[str, TradeMetrics] = {}
    for name, trades in buckets.items():
        metrics[name] = compute_trade_metrics(
            trades, bucket=name, thresholds=thresholds, today_date=today_date
        )

    backtest = metrics.get("backtest")
    oos = metrics.get("protected_oos")
    paper = metrics.get("paper")

    reasons: list[str] = []
    if not alerts.data_quality_ok:
        reasons.append(f"data quality failure: {'; '.join(alerts.data_quality_notes)}")
    if alerts.risk_control_violations:
        reasons.append(f"risk-control violation(s): {'; '.join(alerts.risk_control_violations)}")
    if alerts.simulation_anomalies:
        reasons.append(f"simulation anomaly(ies): {'; '.join(alerts.simulation_anomalies)}")
    if alerts.confidence_uncalibrated:
        reasons.append("signal confidence is uncalibrated")
    integrity_ok = not any((
        not alerts.data_quality_ok,
        alerts.risk_control_violations,
        alerts.simulation_anomalies,
        alerts.confidence_uncalibrated,
    ))

    trend_segments = segment_expectancies(
        list(buckets.get("backtest", ())) + list(buckets.get("protected_oos", ())),
        min_trades=thresholds.min_segment_trades_for_trend,
    )

    # Determine health.
    health: str
    hard_integrity_failures = list(alerts.risk_control_violations) + list(
        alerts.simulation_anomalies
    )
    if not alerts.data_quality_ok:
        hard_integrity_failures.append("data quality failure")
    if hard_integrity_failures:
        health = "RED"
        reasons.append(
            "integrity failure requires investigation: "
            + "; ".join(hard_integrity_failures)
        )
    elif (
        backtest is not None
        and backtest.total_closed >= thresholds.min_backtest_trades_green
        and backtest.expectancy is not None
        and backtest.expectancy <= thresholds.expectancy_zero
    ):
        health = "RED"
        reasons.append(
            f"backtest net expectancy {backtest.expectancy:f}/trade is not positive "
            f"over {backtest.total_closed} trades"
        )
    elif (
        oos is not None
        and oos.total_closed >= thresholds.min_oos_trades_green
        and oos.expectancy is not None
        and oos.expectancy <= thresholds.expectancy_zero
    ):
        health = "RED"
        reasons.append(
            f"protected OOS net expectancy {oos.expectancy:f}/trade is not positive "
            f"over {oos.total_closed} trades"
        )
    elif (
        backtest is not None
        and backtest.total_closed >= thresholds.min_backtest_trades_green
        and backtest.expectancy is not None
        and backtest.expectancy > thresholds.expectancy_zero
        and oos is not None
        and oos.total_closed >= thresholds.min_oos_trades_green
        and oos.expectancy is not None
        and oos.expectancy > thresholds.expectancy_zero
        and (backtest.profit_factor is not None and backtest.profit_factor >= thresholds.profit_factor_green)
        and (oos.profit_factor is not None and oos.profit_factor >= thresholds.profit_factor_green)
        and integrity_ok
    ):
        health = "GREEN"
        reasons.append(
            f"positive net expectancy on backtest ({backtest.expectancy:f}/trade) and "
            f"protected OOS ({oos.expectancy:f}/trade) with profit factor >= "
            f"{thresholds.profit_factor_green:f} and clean integrity"
        )
    else:
        health = "YELLOW"
        reasons.append(
            "insufficient or mixed evidence for a GREEN decision; no RED trigger fired"
        )

    # Record specific missing-evidence reasons for a YELLOW decision.
    if health == "YELLOW":
        if backtest is None:
            reasons.append("no backtest trade evidence recorded")
        elif backtest.total_closed < thresholds.min_backtest_trades_green:
            reasons.append(
                f"backtest sample {backtest.total_closed} < "
                f"{thresholds.min_backtest_trades_green} required for GREEN"
            )
        if oos is None or oos.total_closed < thresholds.min_oos_trades_green:
            reasons.append(
                f"protected OOS sample too small ({oos.total_closed if oos else 0} "
                f"< {thresholds.min_oos_trades_green})"
            )

    # Determine readiness (PAPER-TRADING readiness indicator).
    ready: str
    paper_sample = paper is not None and paper.total_closed >= thresholds.min_paper_trades_for_ready
    if health == "GREEN" and paper_sample and paper is not None:
        ready = "YES"
    elif health == "RED":
        ready = "NO"
    elif health == "YELLOW":
        ready = "MONITOR"
    elif health == "GREEN":
        ready = "MONITOR"
    else:
        ready = "NO"

    trend, trend_reason = _trend(trend_segments)
    sample_size_ok = (
        backtest is not None
        and backtest.total_closed >= thresholds.min_backtest_trades_green
        and oos is not None
        and oos.total_closed >= thresholds.min_oos_trades_green
    )

    if ready == "NO":
        readiness_reason = (
            f"paper readiness is NO because the health assessment is RED "
            f"({'; '.join(r for r in reasons if r)})"
        )
    elif ready == "MONITOR":
        reasons_after = [r for r in reasons if r]
        readiness_reason = (
            "paper readiness is MONITOR: GREEN evidence needs a sufficient live "
            "paper sample before YES"
            if health == "GREEN"
            else f"paper readiness is MONITOR because evidence is not conclusive ({'; '.join(reasons_after) or 'see health reason'})"
        )
    else:
        readiness_reason = (
            "paper readiness is YES: GREEN health, a sufficient live paper sample, "
            "and no deterioration"
        )

    segment_dicts = [
        {"segment": label, "trades": count, "expectancy": str(exp) if exp is not None else None}
        for label, count, exp in trend_segments
    ]
    return HealthAssessment(
        health=health,
        ready=ready,
        performance_trend=trend,
        sample_size_ok=sample_size_ok,
        health_reason="; ".join(dict.fromkeys(reasons)),
        readiness_reason=readiness_reason,
        trend_reason=trend_reason,
        version=version,
        configuration_version=configuration_version,
        thresholds=thresholds.to_dict(),
        buckets=metrics,
        segments=segment_dicts,
        integrity=alerts,
        generated_at=datetime.now().astimezone().isoformat(timespec="seconds"),
    )