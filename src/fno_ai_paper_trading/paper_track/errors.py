"""Error taxonomy for the isolated Daily Paper Trading Track.

All exceptions raised by the track inherit from :class:`PaperTrackError` so a
runner can catch a single base type. The track is a fail-closed, paper-only
simulator: errors describe data, policy, persistence and concurrency faults —
never "order went to a live exchange".
"""

from __future__ import annotations

__all__ = [
    "PaperTrackError",
    "InjectedFailure",
    "TrackValidationError",
    "TrackPolicyViolation",
    "TrackLockError",
    "TrackCheckpointError",
    "TrackRecoveryError",
]


class PaperTrackError(Exception):
    """Base class for every error raised by the paper track."""


class InjectedFailure(PaperTrackError):
    """Simulated crash/restart failure raised at an explicit failpoint.

    Failpoints (``engine.failpoints``) raise this from a chosen step location so
    tests can verify that restarting from the latest checkpoint re-executes a
    bar exactly once and never duplicates or loses a fill.
    """

    def __init__(self, point: str) -> None:
        super().__init__(f"injected failure at failpoint {point!r}")
        self.point = point


class TrackValidationError(PaperTrackError):
    """A bar batch / signal failed the track's adversarial data validation."""


class TrackPolicyViolation(PaperTrackError):
    """The track observed a state that violates its operating policy (fail closed)."""


class TrackLockError(PaperTrackError):
    """A run could not acquire or release its account lock."""


class TrackCheckpointError(PaperTrackError):
    """A checkpoint or manifest could not be written or read honestly."""


class TrackRecoveryError(TrackCheckpointError):
    """A checkpoint was loadable but its contents were inconsistent or stale."""