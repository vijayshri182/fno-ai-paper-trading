"""State semantics for the autonomous research/validation loop.

The loop never silently terminates on a protocol ``BLOCKED`` condition. This
module models the five loop states and the blockages that trigger them, plus the
declarative next-action policy each state must follow.

Algorithm Health color (RED/AMBER/GREEN) is never a terminal condition. A
``LoopState.GREEN`` value is a protocol signal ("candidate evidence satisfies
the current stage") that is evaluated against the same autonomous paths as any
other state; the loop only escalates to human approval when no autonomous path
remains.

Safety: research/bookkeeping only -- nothing here places orders, enables live
trading or promotes a version. LIVE GATE stays CLOSED.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Mapping


class LoopState(str, Enum):
    """The five autonomous loop states."""

    GREEN = "GREEN"
    RED = "RED"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    BLOCKED = "BLOCKED"
    HUMAN_DECISION_REQUIRED = "HUMAN_DECISION_REQUIRED"


class BlockageType(str, Enum):
    """Why a loop operation is blocked or a decision required."""

    OOS_ALREADY_CONSUMED = "OOS_ALREADY_CONSUMED"
    RESEARCH_VALIDATION_BLOCKED = "RESEARCH_VALIDATION_BLOCKED"
    DATA_ACQUISITION_DEPENDENCY = "DATA_ACQUISITION_DEPENDENCY"
    INSUFFICIENT_FRESH_DATA = "INSUFFICIENT_FRESH_DATA"
    MISSING_HUMAN_APPROVAL = "MISSING_HUMAN_APPROVAL"
    NO_VALID_PATH = "NO_VALID_PATH"


class PathKind(str, Enum):
    """Kinds of next-action the loop may take."""

    FRESH_OOS_ACQUISITION = "acquire_fresh_oos_data"
    OTHER_FAMILY_RESEARCH = "research_other_family"
    PREREGISTERED_HYPOTHESIS = "preregister_hypothesis"
    AWAIT_HUMAN_DECISION = "await_human_decision"
    AWAIT_HUMAN_APPROVAL = "await_human_approval"
    AWAIT_FRESH_OOS_DATA = "await_fresh_oos_data"


STATE_POLICY: Mapping[LoopState, str] = {
    LoopState.GREEN: (
        "GREEN is not a terminal condition; continue on any ready"
        " autonomous protocol-compliant path; escalate to human"
        " approval only when no autonomous path remains"
    ),
    LoopState.RED: "search_other_permitted_path",
    LoopState.INSUFFICIENT_EVIDENCE: "identify_missing_evidence",
    LoopState.BLOCKED: "search_protocol_compliant_path",
    LoopState.HUMAN_DECISION_REQUIRED: "await_human_decision",
}


@dataclass(frozen=True)
class ValidPath:
    """A protocol-compliant next step the loop may take."""

    kind: PathKind
    label: str
    description: str
    autonomous: bool
    ready: bool
    reason: str = ""

    def __post_init__(self) -> None:
        if not self.label.strip():
            raise ValueError("label is required")
        if not self.description.strip():
            raise ValueError("description is required")

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "label": self.label,
            "description": self.description,
            "autonomous": self.autonomous,
            "ready": self.ready,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class Blockage:
    """A specific blockage condition the loop has identified."""

    candidate: str
    stage: str
    blockage_type: BlockageType
    reason: str
    evidence: tuple[str, ...] = ()
    prohibited: tuple[str, ...] = ()
    detected_at: str = ""

    def __post_init__(self) -> None:
        if not self.candidate.strip():
            raise ValueError("candidate is required")
        if not self.stage.strip():
            raise ValueError("stage is required")
        if not self.reason.strip():
            raise ValueError("reason is required")

    def to_dict(self) -> dict[str, object]:
        return {
            "candidate": self.candidate,
            "stage": self.stage,
            "blockage_type": self.blockage_type.value,
            "reason": self.reason,
            "evidence": list(self.evidence),
            "prohibited": list(self.prohibited),
            "detected_at": self.detected_at,
        }
