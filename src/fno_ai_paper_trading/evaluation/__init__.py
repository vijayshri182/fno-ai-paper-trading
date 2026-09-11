"""Historical strategy evaluation (WS 7.4).

Deterministic, multi-session evaluation of a strategy over validated datasets
with a standardized metric set. Research/evidence only — this never places
orders or changes the frozen MA(5,21) baseline.
"""

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
    "EvaluationAggregate",
    "EvaluationConfig",
    "EvaluationRun",
    "HistoricalEvaluator",
    "SessionEvaluation",
    "evaluation_run_to_dict",
    "evaluation_run_to_html",
]