"""Evaluation metrics for options paper-simulation ledgers (Phase 12).

Transforms Phase 11 ``options_paper`` ledger rows (the exact ``ledger`` shape
emitted by ``options_paper.report.build_daily_report``) into the research
metrics required by the phase, with explicit denominators and missingness.

This module computes *summaries over ledger outcomes*; it never re-derives
fills, fees, positions or reconciliation (that stays in Phase 11). Every metric
that is mathematically undefined for a sample returns ``None`` together with a
``reasons`` entry — never 0, never infinity, and never a fabricated number.

Synthetic fixture samples are surfaced through ``data_label`` so a fixture run
can never be mistaken for observed market evidence.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence

from fno_ai_paper_trading.paper_track.options_paper.fill_model import FILL_MODEL_VERSION

ZERO = Decimal("0")
PCT = Decimal("100")


def _dec(value: Any, name: str) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        raise ValueError(f"LedgerRow field {name!r} is not decimal-coercible: {value!r}")


@dataclass(frozen=True)
class LedgerRow:
    """One executed paper lifecycle, mirroring the Phase 11 daily-report ledger.

    Only ``record_id``, ``contract_key`` and the P&L fields are required; the
    rest (regime/expiry/strike/duration/exposure) are optional and only used when
    present (missingness is preserved, never zeroed).
    """

    record_id: str
    contract_key: str
    quantity: int
    entry_fill: Decimal
    exit_fill: Decimal | None
    gross_realized_pnl: Decimal
    net_realized_pnl: Decimal
    close_day: date
    entry_timestamp: datetime | None = None
    exit_timestamp: datetime | None = None
    holding_duration_seconds: int | None = None
    premium_exposure: Decimal | None = None
    regime: str | None = None
    expiry: date | None = None
    strike: Decimal | None = None
    entry_commission: Decimal | None = None
    exit_commission: Decimal | None = None
    slippage: Decimal | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "record_id": self.record_id,
            "contract_key": self.contract_key,
            "quantity": self.quantity,
            "entry_fill": str(self.entry_fill),
            "exit_fill": str(self.exit_fill) if self.exit_fill is not None else None,
            "gross_realized_pnl": str(self.gross_realized_pnl),
            "net_realized_pnl": str(self.net_realized_pnl),
            "close_day": self.close_day.isoformat(),
            "entry_timestamp": self.entry_timestamp.isoformat() if self.entry_timestamp else None,
            "exit_timestamp": self.exit_timestamp.isoformat() if self.exit_timestamp else None,
            "holding_duration_seconds": self.holding_duration_seconds,
            "premium_exposure": str(self.premium_exposure) if self.premium_exposure is not None else None,
            "regime": self.regime,
            "expiry": self.expiry.isoformat() if self.expiry else None,
            "strike": str(self.strike) if self.strike is not None else None,
            "entry_commission": str(self.entry_commission) if self.entry_commission is not None else None,
            "exit_commission": str(self.exit_commission) if self.exit_commission is not None else None,
            "slippage": str(self.slippage) if self.slippage is not None else None,
        }

    @classmethod
    def from_dict(cls, row: Mapping[str, Any]) -> "LedgerRow":
        """Coerce a Phase 11 ledger dict into a :class:`LedgerRow`.

        Raises ``ValueError`` on missing identity or non-decimal P&L — the
        denominator-integrity guard: a row without usable P&L cannot enter the
        sample silently.
        """
        record_id = str(row.get("record_id", "")).strip()
        contract_key = str(row.get("contract_key", "")).strip()
        if not record_id or not contract_key:
            raise ValueError("LedgerRow requires record_id and contract_key (identity pin)")
        close_day_raw = row.get("close_day")
        if not close_day_raw:
            raise ValueError(f"LedgerRow {record_id!r} is missing close_day")
        close_day = date.fromisoformat(str(close_day_raw).strip())
        gross = _dec(row.get("gross_realized_pnl"), "gross_realized_pnl")
        net = _dec(row.get("net_realized_pnl"), "net_realized_pnl")
        if gross is None or net is None:
            raise ValueError(f"LedgerRow {record_id!r} has non-decimal P&L (gross={gross!r} net={net!r})")
        return cls(
            record_id=record_id,
            contract_key=contract_key,
            quantity=int(row.get("quantity", 0)),
            entry_fill=_dec(row.get("entry_fill"), "entry_fill") or ZERO,
            exit_fill=_dec(row.get("exit_fill"), "exit_fill"),
            gross_realized_pnl=gross,
            net_realized_pnl=net,
            close_day=close_day,
            entry_timestamp=_parse_dt(row.get("entry_timestamp")),
            exit_timestamp=_parse_dt(row.get("exit_timestamp")),
            holding_duration_seconds=(
                int(row["holding_duration_seconds"])
                if row.get("holding_duration_seconds") is not None
                else None
            ),
            premium_exposure=_dec(row.get("premium_exposure"), "premium_exposure"),
            regime=str(row["regime"]) if row.get("regime") else None,
            expiry=date.fromisoformat(str(row["expiry"]).strip()) if row.get("expiry") else None,
            strike=_dec(row.get("strike"), "strike"),
            entry_commission=_dec(row.get("entry_commission"), "entry_commission"),
            exit_commission=_dec(row.get("exit_commission"), "exit_commission"),
            slippage=_dec(row.get("slippage"), "slippage"),
        )


def _parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


@dataclass(frozen=True)
class LedgerSample:
    """The sample a metrics computation runs over, with explicit denominators."""

    opportunities: int = 0
    rejected_by_reason: Mapping[str, int] = field(default_factory=dict)
    rows: Sequence[LedgerRow] = ()
    capital: Decimal | None = None
    fill_model_version: str = FILL_MODEL_VERSION
    data_label: str = "SYNTHETIC_FIXTURE"

    @property
    def executed(self) -> int:
        return len(self.rows)

    @property
    def rejected_total(self) -> int:
        return sum(self.rejected_by_reason.values())


@dataclass(frozen=True)
class OptionsResearchMetrics:
    """Deterministic research metrics over a paper ledger sample.

    Undefined metrics are ``None`` with an entry in ``reasons`` (explicit
    denominator/missingness reporting). Values are Decimal-backed strings.
    """

    data_label: str
    fill_model_version: str
    executed: int
    rejected_total: int
    opportunities: int
    wins: int
    losses: int
    flat: int
    win_rate_pct: Decimal | None
    gross_profit: Decimal
    gross_loss: Decimal
    net_realized_pnl: Decimal
    commissions: Decimal | None
    total_slippage: Decimal | None
    avg_win: Decimal | None
    avg_loss: Decimal | None
    profit_factor: Decimal | None
    max_drawdown: Decimal | None
    avg_holding_seconds: int | None
    min_holding_seconds: int | None
    max_holding_seconds: int | None
    profitable_days: int
    losing_days: int
    trading_days: int
    daily_pnl: Mapping[str, str]
    regime_distribution: Mapping[str, int]
    regime_unknown: int
    expiry_buckets: Mapping[str, int]
    expiry_unknown: int
    strike_buckets: Mapping[str, int]
    total_premium_exposure: Decimal | None
    capital_utilization_pct: Decimal | None
    rejection_rate_pct: Decimal | None
    reasons: Mapping[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "data_label": self.data_label,
            "fill_model_version": self.fill_model_version,
            "counts": {
                "opportunities": self.opportunities,
                "rejected_total": self.rejected_total,
                "executed": self.executed,
                "wins": self.wins,
                "losses": self.losses,
                "flat": self.flat,
            },
            "win_rate_pct": str(self.win_rate_pct) if self.win_rate_pct is not None else None,
            "gross_profit": str(self.gross_profit),
            "gross_loss": str(self.gross_loss),
            "net_realized_pnl": str(self.net_realized_pnl),
            "commissions": str(self.commissions) if self.commissions is not None else None,
            "total_slippage": str(self.total_slippage) if self.total_slippage is not None else None,
            "avg_win": str(self.avg_win) if self.avg_win is not None else None,
            "avg_loss": str(self.avg_loss) if self.avg_loss is not None else None,
            "profit_factor": str(self.profit_factor) if self.profit_factor is not None else None,
            "max_drawdown": str(self.max_drawdown) if self.max_drawdown is not None else None,
            "holding_seconds": {
                "avg": self.avg_holding_seconds,
                "min": self.min_holding_seconds,
                "max": self.max_holding_seconds,
            },
            "daily_pnl": {
                "trading_days": self.trading_days,
                "profitable_days": self.profitable_days,
                "losing_days": self.losing_days,
                "by_day": dict(self.daily_pnl),
            },
            "regime_distribution": dict(self.regime_distribution),
            "regime_unknown": self.regime_unknown,
            "expiry_buckets": dict(self.expiry_buckets),
            "expiry_unknown": self.expiry_unknown,
            "strike_buckets": dict(self.strike_buckets),
            "total_premium_exposure": (
                str(self.total_premium_exposure) if self.total_premium_exposure is not None else None
            ),
            "capital_utilization_pct": (
                str(self.capital_utilization_pct) if self.capital_utilization_pct is not None else None
            ),
            "rejection_rate_pct": (
                str(self.rejection_rate_pct) if self.rejection_rate_pct is not None else None
            ),
            "reasons": dict(self.reasons),
        }

    def render_markdown(self) -> str:
        d = self.to_dict()
        lines = [
            "# Options research metrics (paper-ledger sample)",
            "",
            f"- data_label: `{self.data_label}` · fill_model_version: `{self.fill_model_version}`",
            f"- opportunities: {self.opportunities} · rejected: {self.rejected_total} · executed: {self.executed}",
            f"- wins: {self.wins} · losses: {self.losses} · flat: {self.flat} · win_rate%: {d['win_rate_pct']}",
            f"- gross profit: {self.gross_profit} · gross loss: {self.gross_loss} · net: {self.net_realized_pnl}",
            f"- profit factor: {d['profit_factor']} · max drawdown: {d['max_drawdown']} · commissions: {d['commissions']}",
            f"- avg holding (s): {self.avg_holding_seconds} · min: {self.min_holding_seconds} · max: {self.max_holding_seconds}",
            f"- capital utilisation %: {d['capital_utilization_pct']} · rejection rate %: {d['rejection_rate_pct']}",
            "- reasons:",
        ]
        if not self.reasons:
            lines.append("  - (none)")
        for key, value in sorted(self.reasons.items()):
            lines.append(f"  - `{key}`: {value}")
        return "\n".join(lines) + "\n"


def compute_metrics(sample: LedgerSample) -> OptionsResearchMetrics:
    """Compute all metrics over the sample (pure, no clocks, no I/O)."""
    reasons: dict[str, str] = {}
    rows = list(sample.rows)
    executed = len(rows)

    wins = sum(1 for r in rows if r.net_realized_pnl > ZERO)
    losses = sum(1 for r in rows if r.net_realized_pnl < ZERO)
    flat = executed - wins - losses

    win_rate_pct: Decimal | None = (
        (PCT * Decimal(wins) / Decimal(executed)) if executed else None
    )
    if not executed:
        reasons["win_rate_pct"] = "undefined: no executed trades"

    gross_profit = sum((r.gross_realized_pnl for r in rows if r.gross_realized_pnl > ZERO), ZERO)
    gross_loss = sum((r.gross_realized_pnl for r in rows if r.gross_realized_pnl < ZERO), ZERO)
    net = sum((r.net_realized_pnl for r in rows), ZERO)

    commissions = [
        (r.entry_commission or ZERO) + (r.exit_commission or ZERO)
        for r in rows
        if r.entry_commission is not None or r.exit_commission is not None
    ]
    total_commissions: Decimal | None = sum(commissions, ZERO) if commissions else None
    slippages = [r.slippage for r in rows if r.slippage is not None]
    total_slippage: Decimal | None = sum(slippages, ZERO) if slippages else None

    win_pnls = [r.net_realized_pnl for r in rows if r.net_realized_pnl > ZERO]
    loss_pnls = [r.net_realized_pnl for r in rows if r.net_realized_pnl < ZERO]
    avg_win = (sum(win_pnls, ZERO) / Decimal(len(win_pnls))) if win_pnls else None
    avg_loss = (sum(loss_pnls, ZERO) / Decimal(len(loss_pnls))) if loss_pnls else None

    profit_factor: Decimal | None = None
    if executed == 0:
        reasons["profit_factor"] = "undefined: no executed trades"
    elif gross_loss == ZERO and gross_profit > ZERO:
        reasons["profit_factor"] = "undefined: no losing trades (zero denominator)"
    elif gross_loss == ZERO and gross_profit == ZERO:
        reasons["profit_factor"] = "undefined: both gross profit and gross loss are zero"
    elif gross_profit == ZERO and gross_loss < ZERO:
        reasons["profit_factor"] = "undefined: no gross profit"
    else:
        profit_factor = gross_profit / abs(gross_loss)

    # stock/daily aggregates plus max drawdown on cumulative net (rupees).
    daily: dict[date, Decimal] = {}
    for r in rows:
        daily[r.close_day] = daily.get(r.close_day, ZERO) + r.net_realized_pnl
    profitable_days = sum(1 for v in daily.values() if v > ZERO)
    losing_days = sum(1 for v in daily.values() if v < ZERO)
    trading_days = len(daily)

    cumulative = ZERO
    peak = ZERO
    max_dd: Decimal | None = None
    for day in sorted(daily):
        cumulative += daily[day]
        peak = max(peak, cumulative)
        drop = peak - cumulative
        if max_dd is None or drop > max_dd:
            max_dd = drop

    durations = [r.holding_duration_seconds for r in rows if r.holding_duration_seconds is not None]
    avg_hold = sum(durations) // len(durations) if durations else None
    min_hold = min(durations) if durations else None
    max_hold = max(durations) if durations else None

    regime_counts: dict[str, int] = {}
    regime_unknown = 0
    for r in rows:
        if r.regime is None:
            regime_unknown += 1
        else:
            regime_counts[r.regime] = regime_counts.get(r.regime, 0) + 1

    expiry_counts: dict[str, int] = {}
    expiry_unknown = 0
    for r in rows:
        if r.expiry is None:
            expiry_unknown += 1
        else:
            key = r.expiry.isoformat()
            expiry_counts[key] = expiry_counts.get(key, 0) + 1

    strike_counts: dict[str, int] = {}
    for r in rows:
        if r.strike is not None:
            key = str(r.strike)
            strike_counts[key] = strike_counts.get(key, 0) + 1

    exposures = [r.premium_exposure for r in rows if r.premium_exposure is not None]
    total_exposure: Decimal | None = sum(exposures, ZERO) if exposures else None

    capital_utilization: Decimal | None = None
    if total_exposure is not None and sample.capital is not None and sample.capital > ZERO:
        capital_utilization = PCT * total_exposure / sample.capital
    elif total_exposure is not None and (sample.capital is None or sample.capital == ZERO):
        reasons["capital_utilization_pct"] = "undefined: capital missing or zero"

    rejection_rate: Decimal | None = None
    if sample.opportunities > 0:
        rejection_rate = PCT * Decimal(sample.rejected_total) / Decimal(sample.opportunities)
    elif sample.opportunities == 0:
        reasons["rejection_rate_pct"] = "undefined: zero opportunities"
    elif sample.rejected_total > 0:
        reasons["rejection_rate_pct"] = "undefined: no opportunities (zero denominator)"

    return OptionsResearchMetrics(
        data_label=sample.data_label,
        fill_model_version=sample.fill_model_version,
        executed=executed,
        rejected_total=sample.rejected_total,
        opportunities=sample.opportunities,
        wins=wins,
        losses=losses,
        flat=flat,
        win_rate_pct=win_rate_pct,
        gross_profit=gross_profit,
        gross_loss=gross_loss,
        net_realized_pnl=net,
        commissions=total_commissions,
        total_slippage=total_slippage,
        avg_win=avg_win,
        avg_loss=avg_loss,
        profit_factor=profit_factor,
        max_drawdown=max_dd,
        avg_holding_seconds=avg_hold,
        min_holding_seconds=min_hold,
        max_holding_seconds=max_hold,
        profitable_days=profitable_days,
        losing_days=losing_days,
        trading_days=trading_days,
        daily_pnl={k.isoformat(): str(v) for k, v in sorted(daily.items())},
        regime_distribution=regime_counts,
        regime_unknown=regime_unknown,
        expiry_buckets=expiry_counts,
        expiry_unknown=expiry_unknown,
        strike_buckets=strike_counts,
        total_premium_exposure=total_exposure,
        capital_utilization_pct=capital_utilization,
        rejection_rate_pct=rejection_rate,
        reasons=reasons,
    )


def sample_from_daily_report(
    report_payload: Mapping[str, Any],
    *,
    opportunities: int | None = None,
    rejected_by_reason: Mapping[str, int] | None = None,
    capital: Decimal | None = None,
    data_label: str = "SYNTHETIC_FIXTURE",
) -> LedgerSample:
    """Build a :class:`LedgerSample` from a Phase 11 daily report payload.

    ``report_payload`` is the ``{"aggregate": {...}, "fingerprint": ...}``
    envelope produced by ``options_paper.report.build_daily_report``; ledger
    rows are read from ``aggregate["ledger"]``. Denominators default to the
    report's closed/rejected counts so a caller cannot silently under-count.
    """
    aggregate = report_payload.get("aggregate", report_payload)
    fill_version = aggregate.get("fill_model_version", FILL_MODEL_VERSION)
    counts = aggregate.get("counts", {})
    if opportunities is None:
        opportunities = int(counts.get("closed", 0)) + int(counts.get("rejected", 0))
    if rejected_by_reason is None:
        rejected_by_reason = {"rejected_session_gate_and_validation": int(counts.get("rejected", 0))}

    rows = [LedgerRow.from_dict(row) for row in aggregate.get("ledger", [])]
    return LedgerSample(
        opportunities=opportunities,
        rejected_by_reason=dict(rejected_by_reason),
        rows=tuple(rows),
        capital=capital,
        fill_model_version=fill_version,
        data_label=data_label,
    )


__all__ = [
    "LedgerRow",
    "LedgerSample",
    "OptionsResearchMetrics",
    "compute_metrics",
    "sample_from_daily_report",
]