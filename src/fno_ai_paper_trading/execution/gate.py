"""Explicit human-controlled enablement gate for live execution (WS 7.9).

The gate answers one question: *is a real Upstox order write currently
authorized?* It is strictly opt-in and refuses by default. A real send requires
all of the following, evaluated together at decision time:

1. the master flag ``FNO_LIVE_EXECUTION_TEST_ENABLED=1`` (env),
2. an operator-authored consent file (created by a human, expires, and
   references the exact access token by its **sha256 fingerprint** — the token
   itself is never stored),
3. a matching ``UPSTOX_ACCESS_TOKEN`` present in the process environment only,
4. the consent window within its configured validity (``expiry_hours`` cap).

``ExecutionMode.LIVE`` is declared but **never returned here and never
implemented**: the adapter raises if a caller ever tries to construct a normal
live order path. Only ``PAPER`` (implicit) and ``LIVE_EXECUTION_TEST`` (explicit)
exist operationally.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Callable, Mapping

from fno_ai_paper_trading.config.settings import LiveExecutionTestSettings
from fno_ai_paper_trading.data.market_hours import NSE_TZ

#: Consent-file field carrying the sha256 fingerprint of the access token.
FINGERPRINT_FIELD = "token_fingerprint_sha256"
CONSENT_FIELDS = ("operator", "purpose", "created_at", "expires_at", FINGERPRINT_FIELD)


class ExecutionMode(str, Enum):
    """Operational execution modes of the system."""

    PAPER = "PAPER"  # the only implicit mode; default everywhere
    LIVE_EXECUTION_TEST = "LIVE_EXECUTION_TEST"  # explicit, human-controlled, one-run
    LIVE = "LIVE"  # declared but unimplementable — never returned by the gate

    @property
    def is_real(self) -> bool:
        return self is ExecutionMode.LIVE_EXECUTION_TEST or self is ExecutionMode.LIVE

    @property
    def can_write_orders(self) -> bool:
        return self is ExecutionMode.LIVE_EXECUTION_TEST


@dataclass(frozen=True)
class GateDecision:
    """Outcome of asking the gate whether a real order write is allowed."""

    ok: bool
    mode: ExecutionMode = ExecutionMode.PAPER
    reasons: tuple[str, ...] = field(default_factory=tuple)

    @property
    def summary(self) -> str:
        if self.ok:
            return f"gate open: mode={self.mode.value}"
        return "gate closed: " + "; ".join(self.reasons)


def _naive_ist_now() -> datetime:
    return datetime.now(NSE_TZ).replace(tzinfo=None)


def sha256_hex(value: str) -> str:
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()


def _parse_iso(value: object, name: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(NSE_TZ).replace(tzinfo=None)
    return parsed


def consent_fingerprint(token: str) -> str:
    """Stable public fingerprint helper (also used by docs/tools to author a consent file)."""
    if not (token or "").strip():
        raise ValueError("cannot fingerprint an empty token")
    return sha256_hex(token)


def default_consent_loader(path: str | Path):
    """Read the consent file as a plain mapping (None when missing/invalid)."""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def default_token_loader() -> str:
    return os.getenv("UPSTOX_ACCESS_TOKEN", "").strip()


class LiveExecutionTestGate:
    """Evaluates whether a real order write is authorized right now."""

    def __init__(
        self,
        settings: LiveExecutionTestSettings,
        *,
        now_fn: Callable[[], datetime] = _naive_ist_now,
        token_loader: Callable[[], str] = default_token_loader,
        consent_loader: Callable[[], Mapping[str, object] | None] | None = None,
    ) -> None:
        self.settings = settings
        self._now = now_fn
        self._token_loader = token_loader
        self._consent_loader = consent_loader or (lambda: default_consent_loader(
            settings.consent_file
        ))

    def decision(self) -> GateDecision:
        reasons: list[str] = []

        if not self.settings.enabled:
            reasons.append(
                "master enable flag FNO_LIVE_EXECUTION_TEST_ENABLED is not set (explicit "
                "human-controlled enablement is required)"
            )

        consent = self._consent_loader()
        if not consent:
            reasons.append(
                f"operator consent file missing/invalid: "
                f"{self.settings.consent_file}"
            )
        else:
            reasons.extend(self._consent_reasons(consent))

        if consent is not None and self.settings.enabled:
            missing = [field for field in CONSENT_FIELDS if not str(consent.get(field, "")).strip()]
            if missing:
                reasons.append(f"consent file missing fields: {', '.join(missing)}")

        mode = (
            ExecutionMode.LIVE_EXECUTION_TEST
            if not reasons
            else ExecutionMode.PAPER
        )
        return GateDecision(ok=not reasons, mode=mode, reasons=tuple(reasons))

    def mode(self) -> ExecutionMode:
        return self.decision().mode

    # ------------------------------------------------------------------ parts

    def _consent_reasons(self, consent: Mapping[str, object]) -> list[str]:
        reasons: list[str] = []
        now = self._now()

        created = _parse_iso(consent.get("created_at"), "created_at")
        expires = _parse_iso(consent.get("expires_at"), "expires_at")
        if created is None:
            reasons.append("consent file has an invalid created_at")
        if expires is None:
            reasons.append("consent file has an invalid expires_at")
        if created is not None and expires is not None:
            if expires <= now:
                reasons.append(f"consent expired at {expires.isoformat(timespec='minutes')}")
            if now < created:
                reasons.append(f"consent is not yet valid (created {created.isoformat(timespec='minutes')})")
            window = expires - created
            if window > timedelta(hours=float(self.settings.expiry_hours)):
                reasons.append(
                    "consent window exceeds the configured expiry_hours cap "
                    f"({self.settings.expiry_hours:g}h); shorten expires_at"
                )

        token = self._token_loader()
        if not token:
            reasons.append("no UPSTOX_ACCESS_TOKEN present in the environment")
        elif consent:
            expected = str(consent.get(FINGERPRINT_FIELD, "") or "").strip().lower()
            actual = sha256_hex(token)
            if not expected:
                reasons.append(f"consent file missing {FINGERPRINT_FIELD}")
            elif actual != expected:
                reasons.append(
                    "the environment access-token fingerprint does not match the "
                    "consent file (re-authorize with a fresh consent file)"
                )
        return reasons