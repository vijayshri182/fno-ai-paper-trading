"""Domain enumerations for the experience store (WS 7.9).

These labels are *evidence classification*, not execution. Every value is a
plain string enum so records serialize deterministically and queries stay
text-based.
"""
from __future__ import annotations

from enum import Enum


class OutcomeKind(str, Enum):
    """Classification of a realized paper trade outcome."""

    PENDING = "PENDING"  # decision recorded, outcome not yet known
    OPEN = "OPEN"  # position still open when this snapshot was recorded
    WIN = "WIN"
    LOSS = "LOSS"
    BREAKEVEN = "BREAKEVEN"
    FLAT = "FLAT"  # no trade (e.g. HOLD decision with no entry / no fill)


class DecisionStatus(str, Enum):
    """What happened to the decision at decision time (paper path only)."""

    EXECUTED = "EXECUTED"  # risk-approved and filled by the paper broker
    REJECTED = "REJECTED"  # rejected by the deterministic risk manager
    SKIPPED = "SKIPPED"  # intentionally not acted on (e.g. HOLD/no entry)
    NOT_ACTED = "NOT_ACTED"  # record created before execution outcome known


class AdvisoryUsage(str, Enum):
    """How the (always advisory) AI recommendation was used."""

    NONE = "NONE"  # no AI/advisory input at this decision
    ACCEPTED = "ACCEPTED"  # agreed with / aligned to the deterministic signal
    REJECTED = "REJECTED"  # deterministic signal overrode the advisory input
    OVERRIDDEN = "OVERRIDDEN"  # operator/human overrode the recommendation


class DataQualityStatus(str, Enum):
    """Quality status of the market data at decision time."""

    VALIDATED = "VALIDATED"
    PARTIAL = "PARTIAL"
    INSUFFICIENT = "INSUFFICIENT"
    UNKNOWN = "UNKNOWN"


class ExperienceSourceType(str, Enum):
    """Where an experience record came from."""

    PAPER_SESSION = "PAPER_SESSION"
    HISTORICAL_REPLAY = "HISTORICAL_REPLAY"
    FIVE_YEAR_REPLAY = "FIVE_YEAR_REPLAY"
    SYNTHETIC_TEST = "SYNTHETIC_TEST"
    MANUAL = "MANUAL"