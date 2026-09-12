"""Historical strategy evaluation (WS 7.4).

Deterministic, multi-session evaluation of a strategy over validated datasets
with a standardized metric set. Research/evidence only — this never places
orders or changes the frozen MA(5,21) baseline.
"""

from fno_ai_paper_trading.evaluation.champion_challenger import (
    ChallengeEntry,
    ChallengerDelta,
    ChampionChallenger,
    ComparisonReport,
    MultiPeriodComparison,
    challenger_delta,
    comparison_report_to_dict,
    comparison_report_to_html,
    multi_period_comparison_to_dict,
    multi_period_comparison_to_html,
)
from fno_ai_paper_trading.evaluation.historical import HistoricalEvaluator
from fno_ai_paper_trading.evaluation.records import (
    EvaluationAggregate,
    EvaluationConfig,
    EvaluationRun,
    SessionEvaluation,
)
from fno_ai_paper_trading.evaluation.report import (
    evaluation_run_to_dict,
    evaluation_run_to_html,
)

__all__ = [
    "ChallengeEntry",
    "ChallengerDelta",
    "ChampionChallenger",
    "ComparisonReport",
    "EvaluationAggregate",
    "EvaluationConfig",
    "EvaluationRun",
    "HistoricalEvaluator",
    "MultiPeriodComparison",
    "SessionEvaluation",
    "challenger_delta",
    "comparison_report_to_dict",
    "comparison_report_to_html",
    "evaluation_run_to_dict",
    "evaluation_run_to_html",
    "multi_period_comparison_to_dict",
    "multi_period_comparison_to_html",
]