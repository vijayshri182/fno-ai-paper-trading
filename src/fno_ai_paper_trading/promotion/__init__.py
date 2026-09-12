"""Controlled model promotion & rollback (WS 7.12).

A challenger becomes the champion only through the promotion gate (robust
out-of-sample evidence that preserves risk constraints) and is recorded in the
append-only version registry. Everything here is a decision over evidence; the
promotion never runs a strategy, places an order, or touches risk controls.
"""
from __future__ import annotations

from fno_ai_paper_trading.promotion.gate import (
    DeltaView,
    PromotionCriteria,
    PromotionGate,
    PromotionVerdict,
)
from fno_ai_paper_trading.promotion.registry import (
    DEFAULT_MODEL_REGISTRY_DIR,
    ModelVersion,
    VersionRegistry,
)

__all__ = [
    "DEFAULT_MODEL_REGISTRY_DIR",
    "DeltaView",
    "ModelVersion",
    "PromotionCriteria",
    "PromotionGate",
    "PromotionVerdict",
    "VersionRegistry",
]