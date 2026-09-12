"""Champion / challenger evaluation framework (WS 7.11).

Compares the frozen champion strategy (MA(5,21) baseline) against one or more
challenger candidates on *shared* data under *identical* cost/execution
assumptions, so any performance difference comes from signal selection alone.
Period labels (training / validation / out-of-sample) come from the same no
look-ahead ``split_period`` machinery as the five-year replay: labels are
computed up front from day counts, never influenced by evaluation results.

Evidence only — running a comparison never promotes anyone. Every delta record
is marked ``evidence_only`` and the report's disclaimer states challengers are
NOT adopted. Promotion/rollback gating is a later workstream (WS 7.12).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Mapping, Sequence

from fno_ai_paper_trading.data.dataset_store import StoredDataset
from fno_ai_paper_trading.evaluation.five_year import DayBars, PeriodSplitConfig, split_period
from fno_ai_paper_trading.evaluation.historical import HistoricalEvaluator
from fno_ai_paper_trading.evaluation.records import (
    EvaluationConfig,
    EvaluationRun,
    SessionReplay,
)
from fno_ai_paper_trading.evaluation.report import evaluation_run_to_dict
from fno_ai_paper_trading.research.report import CSS, escape, fmt, kv_rows, table
from fno_ai_paper_trading.strategies.base import Strategy


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


PERIOD_LABELS = ("training", "validation", "out_of_sample")


@dataclass(frozen=True)
class ChallengerDelta:
    """Raw, evidence-only delta of one challenger versus the champion run.

    ``beats_champion`` is a single summary boolean, not a promotion decision:
    the challenger wins the headline comparison (net P&L >= champion and max
    drawdown % <= champion) on this particular data split. Promotion gating is
    a separate later workstream.
    """

    challenger_name: str
    period: str | None  # None = overall (whole run)
    champion_net_pnl: Decimal
    challenger_net_pnl: Decimal
    net_pnl_delta: Decimal
    champion_win_rate: Decimal
    challenger_win_rate: Decimal
    win_rate_delta: Decimal
    champion_max_drawdown_pct: Decimal
    challenger_max_drawdown_pct: Decimal
    max_drawdown_pct_delta: Decimal
    champion_profit_factor: Decimal
    challenger_profit_factor: Decimal
    beats_champion: bool
    evidence_only: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "challenger_name": self.challenger_name,
            "period": self.period,
            "champion_net_pnl": str(self.champion_net_pnl),
            "challenger_net_pnl": str(self.challenger_net_pnl),
            "net_pnl_delta": str(self.net_pnl_delta),
            "champion_win_rate": str(self.champion_win_rate),
            "challenger_win_rate": str(self.challenger_win_rate),
            "win_rate_delta": str(self.win_rate_delta),
            "champion_max_drawdown_pct": str(self.champion_max_drawdown_pct),
            "challenger_max_drawdown_pct": str(self.challenger_max_drawdown_pct),
            "max_drawdown_pct_delta": str(self.max_drawdown_pct_delta),
            "champion_profit_factor": str(self.champion_profit_factor),
            "challenger_profit_factor": str(self.challenger_profit_factor),
            "beats_champion": self.beats_champion,
            "evidence_only": self.evidence_only,
        }


@dataclass(frozen=True)
class ChallengeEntry:
    """One challenger's full run plus its delta versus the champion."""

    name: str
    run: EvaluationRun
    delta: ChallengerDelta | None = None


@dataclass(frozen=True)
class ComparisonReport:
    """The champion's run and every challenger run on one shared data split."""

    title: str
    period: str | None  # None = overall (whole run)
    champion: EvaluationRun
    entries: tuple[ChallengeEntry, ...]
    period_counts: Mapping[str, int] = field(default_factory=dict)
    created_at: str = field(default_factory=_now_iso)


@dataclass(frozen=True)
class MultiPeriodComparison:
    """Overall comparison plus one split by training/validation/out-of-sample."""

    title: str
    overall: ComparisonReport
    by_period: Mapping[str, ComparisonReport] = field(default_factory=dict)
    period_counts: Mapping[str, int] = field(default_factory=dict)


def challenger_delta(
    champion: EvaluationRun,
    challenger: EvaluationRun,
    *,
    period: str | None,
) -> ChallengerDelta:
    """Compute the raw delta of ``challenger`` versus ``champion``."""
    champ_agg = champion.aggregate
    cand_agg = challenger.aggregate
    return ChallengerDelta(
        challenger_name=challenger.strategy_name,
        period=period,
        champion_net_pnl=champ_agg.total_pnl,
        challenger_net_pnl=cand_agg.total_pnl,
        net_pnl_delta=cand_agg.total_pnl - champ_agg.total_pnl,
        champion_win_rate=champ_agg.win_rate,
        challenger_win_rate=cand_agg.win_rate,
        win_rate_delta=cand_agg.win_rate - champ_agg.win_rate,
        champion_max_drawdown_pct=champ_agg.max_drawdown_pct,
        challenger_max_drawdown_pct=cand_agg.max_drawdown_pct,
        max_drawdown_pct_delta=cand_agg.max_drawdown_pct - champ_agg.max_drawdown_pct,
        champion_profit_factor=champ_agg.profit_factor,
        challenger_profit_factor=cand_agg.profit_factor,
        beats_champion=(
            cand_agg.total_pnl >= champ_agg.total_pnl
            and cand_agg.max_drawdown_pct <= champ_agg.max_drawdown_pct
        ),
    )


class ChampionChallenger:
    """Drive side-by-side champion vs challenger evaluations on shared data.

    The same :class:`HistoricalEvaluator` (identical configuration) replays the
    champion and every challenger over the same bar series / days, so the only
    difference between runs is signal selection.
    """

    def __init__(
        self,
        *,
        config: EvaluationConfig | None = None,
        split: PeriodSplitConfig | None = None,
        evaluator: HistoricalEvaluator | None = None,
    ) -> None:
        self.config = config or EvaluationConfig()
        self.split = split or PeriodSplitConfig()
        self._evaluator = evaluator or HistoricalEvaluator(config=self.config)

    # -- whole-dataset comparison -------------------------------------------

    def run(
        self,
        datasets: Sequence[StoredDataset],
        champion: Strategy,
        challengers: Sequence[Strategy],
        *,
        title: str = "champion-vs-challenger",
    ) -> ComparisonReport:
        """Compare every strategy over the same validated datasets."""
        champion_run = self._evaluator.evaluate_strategy(
            datasets, champion, name=title, baseline=True
        )
        entries: list[ChallengeEntry] = []
        for candidate in challengers:
            candidate_run = self._evaluator.evaluate_strategy(
                datasets, candidate, name=title, baseline=False
            )
            entries.append(
                ChallengeEntry(
                    name=candidate.name,
                    run=candidate_run,
                    delta=challenger_delta(champion_run, candidate_run, period=None),
                )
            )
        return ComparisonReport(
            title=title,
            period=None,
            champion=champion_run,
            entries=tuple(entries),
            created_at=_now_iso(),
        )

    # -- day-by-day comparison with period splits ----------------------------

    def run_bars(
        self,
        bars: Sequence,
        champion: Strategy,
        challengers: Sequence[Strategy],
        *,
        title: str = "champion-vs-challenger",
        dataset_name: str = "inline",
        dataset_hash: str = "",
    ) -> ComparisonReport:
        """Compare every strategy over one inline bar series (tests/demos)."""
        champion_run = self._evaluator.build_run(
            [
                self._evaluator.replay_bars(
                    bars,
                    champion,
                    dataset_name=dataset_name,
                    dataset_hash=dataset_hash,
                )
            ],
            name=title,
            strategy_name=champion.name,
            strategy_params={},
            baseline=True,
        )
        entries: list[ChallengeEntry] = []
        for candidate in challengers:
            candidate_run = self._evaluator.build_run(
                [
                    self._evaluator.replay_bars(
                        bars,
                        candidate,
                        dataset_name=dataset_name,
                        dataset_hash=dataset_hash,
                    )
                ],
                name=title,
                strategy_name=candidate.name,
                strategy_params={},
                baseline=False,
            )
            entries.append(
                ChallengeEntry(
                    name=candidate.name,
                    run=candidate_run,
                    delta=challenger_delta(champion_run, candidate_run, period=None),
                )
            )
        return ComparisonReport(
            title=title,
            period=None,
            champion=champion_run,
            entries=tuple(entries),
            created_at=_now_iso(),
        )

    def run_days(
        self,
        days: Sequence[DayBars],
        champion: Strategy,
        challengers: Sequence[Strategy],
        *,
        title: str = "champion-vs-challenger",
    ) -> MultiPeriodComparison:
        """Compare champion vs challengers per period over processed days.

        Days are labelled up front with ``split_period`` (training / validation /
        out-of-sample). The champion and every challenger are replayed over the
        same days; the overall report covers every day and per-period reports
        cover only their own days.
        """
        ordered = sorted(days, key=lambda item: item.day)
        labels = split_period([item.day for item in ordered], self.split)
        period_counts = {
            label: sum(1 for item in ordered if labels.get(item.day) == label)
            for label in PERIOD_LABELS
        }

        def run_for(label: str | None) -> EvaluationRun:
            selected = (
                ordered
                if label is None
                else [item for item in ordered if labels.get(item.day) == label]
            )
            replays: list[SessionReplay] = []
            for day_bars in selected:
                replays.append(
                    self._evaluator.replay_bars(
                        list(day_bars.bars),
                        champion,
                        dataset_name=day_bars.day.isoformat(),
                        dataset_hash=day_bars.source_hash,
                    )
                )
            return self._evaluator.build_run(
                replays,
                name=f"{title}:{label or 'overall'}",
                strategy_name=champion.name,
                strategy_params={},
                baseline=True,
            )

        # -- per-period reports ------------------------------------------------
        overall_champion = run_for(None)

        def challenger_reports(
            champion_run: EvaluationRun,
            label: str | None,
        ) -> tuple[ChallengeEntry, ...]:
            entries: list[ChallengeEntry] = []
            for candidate in challengers:
                selected = ordered if label is None else [
                    item for item in ordered if labels.get(item.day) == label
                ]
                replays = [
                    self._evaluator.replay_bars(
                        list(day_bars.bars),
                        candidate,
                        dataset_name=day_bars.day.isoformat(),
                        dataset_hash=day_bars.source_hash,
                    )
                    for day_bars in selected
                ]
                candidate_run = self._evaluator.build_run(
                    replays,
                    name=f"{title}:{label or 'overall'}",
                    strategy_name=candidate.name,
                    strategy_params={},
                    baseline=False,
                )
                entries.append(
                    ChallengeEntry(
                        name=candidate.name,
                        run=candidate_run,
                        delta=challenger_delta(champion_run, candidate_run, period=label),
                    )
                )
            return tuple(entries)

        by_period: dict[str, ComparisonReport] = {}
        for label in PERIOD_LABELS:
            if period_counts.get(label, 0) == 0:
                continue
            period_champion = run_for(label)
            by_period[label] = ComparisonReport(
                title=f"{title}:{label}",
                period=label,
                champion=period_champion,
                entries=challenger_reports(period_champion, label),
                period_counts={label: period_counts[label]},
                created_at=_now_iso(),
            )
        return MultiPeriodComparison(
            title=title,
            overall=ComparisonReport(
                title=f"{title}:overall",
                period=None,
                champion=overall_champion,
                entries=challenger_reports(overall_champion, None),
                period_counts=dict(period_counts),
                created_at=_now_iso(),
            ),
            by_period=by_period,
            period_counts=dict(period_counts),
        )


# ---------------------------------------------------------------------------
# serialization + HTML rendering
# ---------------------------------------------------------------------------

def comparison_report_to_dict(report: ComparisonReport) -> dict[str, Any]:
    return {
        "title": report.title,
        "period": report.period,
        "period_counts": dict(report.period_counts),
        "created_at": report.created_at,
        "champion_run": evaluation_run_to_dict(report.champion),
        "challengers": [
            {
                "name": entry.name,
                "run": evaluation_run_to_dict(entry.run),
                "delta": entry.delta.to_dict() if entry.delta is not None else None,
            }
            for entry in report.entries
        ],
    }


def multi_period_comparison_to_dict(comparison: MultiPeriodComparison) -> dict[str, Any]:
    return {
        "title": comparison.title,
        "period_counts": dict(comparison.period_counts),
        "overall": comparison_report_to_dict(comparison.overall),
        "by_period": {
            label: comparison_report_to_dict(report)
            for label, report in comparison.by_period.items()
        },
    }


def _run_rows(report: ComparisonReport) -> list[list[str]]:
    rows: list[list[str]] = []
    for entry in report.entries:
        agg = entry.run.aggregate
        delta = entry.delta
        rows.append(
            [
                escape(entry.name),
                f"{fmt(agg.total_pnl)} ₹",
                f"{fmt(agg.win_rate, '%')}",
                str(agg.num_trades),
                f"{fmt(agg.max_drawdown_pct, '%')}",
                fmt(agg.profit_factor),
                "yes" if delta is not None and delta.beats_champion else "no",
            ]
        )
    return rows


def comparison_report_to_html(report: ComparisonReport, title: str | None = None) -> str:
    """Self-contained, labelled champion vs challenger HTML page."""
    champion = report.champion
    c_agg = champion.aggregate
    disclaimer = (
        "Champion vs challenger evidence on validated paper data. Deltas are "
        "research evidence only; no challenger is adopted by this comparison and "
        "no live order is ever placed. Not a profit guarantee."
    )
    header = (
        f"<header><h1>{escape(report.title)} — Champion vs Challenger</h1>"
        f"<p>Champion {escape(champion.strategy_name)} (FROZEN BASELINE) &middot; "
        f"period {report.period or 'overall'} &middot; "
        f"{len(report.entries)} challenger(s) &middot; generated {escape(report.created_at)}</p></header>"
    )
    rows = _run_rows(report)
    delta_table = table(
        ["Challenger", "Net P&L", "Win rate %", "Trades", "MaxDD %", "Profit factor", "Beats champion?"],
        rows,
    )
    champion_summary = kv_rows(
        [
            ("Champion (frozen baseline)", champion.strategy_name),
            ("Net P&L", f"{fmt(c_agg.total_pnl)} ₹"),
            ("Return %", f"{fmt(c_agg.total_return_pct, '%')}"),
            ("Win rate", f"{fmt(c_agg.win_rate, '%')}"),
            ("Round trips", str(c_agg.num_trades)),
            ("Max drawdown %", f"{fmt(c_agg.max_drawdown_pct, '%')}"),
            ("Profit factor", fmt(c_agg.profit_factor)),
        ]
    )
    return (
        f"<!doctype html><html><head><meta charset='utf-8'><style>{CSS}</style></head>"
        f"<body><div class='wrap'>{header}"
        f"<div class='note'>{escape(disclaimer)}</div>"
        f"<section><h2>Champion aggregate</h2><table>{champion_summary}</table></section>"
        f"<section><h2>Challenger deltas</h2>{delta_table}</section>"
        f"<div class='footer'>Deterministic champion/challenger evaluation &middot; paper trading only &middot; evidence, not promotion</div>"
        f"</div></body></html>"
    )


def multi_period_comparison_to_html(
    comparison: MultiPeriodComparison, title: str | None = None
) -> str:
    """HTML page with the overall comparison plus each period block."""
    overall = comparison_report_to_html(comparison.overall, title=title)
    blocks = [overall]
    for label in PERIOD_LABELS:
        report = comparison.by_period.get(label)
        if report is not None:
            blocks.append(comparison_report_to_html(report, title=title))
    return "".join(blocks)