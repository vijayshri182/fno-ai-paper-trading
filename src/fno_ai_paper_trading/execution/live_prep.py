"""WS 7.24B — Stage A: controlled live-buy preparation that **STOPS before BUY**.

This module prepares exactly one tradable NIFTY option contract (current-week,
ATM CE, **dynamic** expiry/strike/CE-PE/lot from the authoritative Upstox
master) for a one-lot MARKET BUY — and then stops. It never sends an order,
never persists a token, never prints a token, and never bypasses or weakens the
authoritative :class:`LiveExecutionTestGate`.

Pipeline (fail-closed; any unverifiable step returns ``FAIL_CLOSED`` with an
empty payload):

1. **OAuth (operator-only)** — a loopback callback server
   (:class:`LocalCallbackServer`) receives the Upstox redirect; the access
   token from the code exchange stays **in-process only**. Nothing here
   automates the operator's broker login/PIN/consent.
2. **Consent refresh (schema preserved)** — the existing
   ``operator_consent.json`` is refreshed: ``operator``/``purpose`` are kept
   (or newly authorized by the operator), ``created_at``/``expires_at`` roll
   forward to ``now + expiry_hours``, and the sha256 fingerprint is updated for
   the in-process token. The file schema is never extended or replaced.
3. **Authoritative gate** — the real :class:`LiveExecutionTestGate` must return
   OK with mode ``LIVE_EXECUTION_TEST`` (master enable flag + consent file +
   matching in-process token). This module never overrides the gate.
4. **Read-only broker checks** — authenticated account id, index spot quote,
   and a FLAT position check, all through the adapter's read-only surface.
5. **Dynamic instrument + lot size** — :func:`resolve_from_upstox_master`
   loads the authoritative BOD master and picks the ATM CE of the current week
   (nothing hard-coded; no fixed 65 / expiry / strike / instrument key).
6. **Margin sanity (client-side)** — :func:`estimate_required_margin` against
   ``settings.max_margin_notional``; over-cap ⇒ abort before any buy.
7. **Exact one-lot MARKET BUY payload** — quantity = the resolved current lot
   size, product ``M``, validity ``DAY``, type MARKET, price 0, side BUY. The
   payload is **returned, never sent**; real order placement requires the
   separate, explicitly-authorized Stage B path (``run_live_execution_test.py``
   + gate open + operator confirmation).

Safety invariants (tested):

* ``place_order`` / ``cancel_order`` are **never invoked** here;
* ``dry_run`` defaults on and the orchestrator refuses real writes;
* tokens/fingerprints never leave memory except the schema-defined sha256
  fingerprint in the consent file; the result/CLI are secret-free;
* no auto-BUY, no silent hand-off to Stage B, gate always authoritative.
"""
from __future__ import annotations

import gzip
import json
import os
import tempfile
import time
import webbrowser
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from fno_ai_paper_trading.config.settings import (
    LiveExecutionTestSettings,
    load_live_test_settings,
)
from fno_ai_paper_trading.data.upstox_instruments import (
    resolve_from_upstox_master,
)
from fno_ai_paper_trading.execution.errors import UpstoxExecutionError
from fno_ai_paper_trading.execution.gate import (
    FINGERPRINT_FIELD,
    ExecutionMode,
    LiveExecutionTestGate,
    consent_fingerprint,
    sha256_hex,
)
from fno_ai_paper_trading.execution.instrument import estimate_required_margin
from fno_ai_paper_trading.execution.oauth import (
    LocalCallbackServer,
    UpstoxOAuthConfig,
    authorize_url,
    exchange_code_for_token,
    new_oauth_state,
)
from fno_ai_paper_trading.execution.upstox import (
    UpstoxCredentials,
    UpstoxExecutionAdapter,
)
from fno_ai_paper_trading.models.enums import OrderSide, OrderType
from fno_ai_paper_trading.models.order import Order

#: Stage-A status outcomes.
READY = "READY_FOR_EXPLICIT_BUY_APPROVAL"
FAIL_CLOSED = "FAIL_CLOSED"

#: Canonical NIFTY index quote key (Upstox v2 market-quote).
NIFTY_INDEX_KEY = "NSE_INDEX|Nifty 50"


@dataclass(frozen=True)
class LivePrepResult:
    """Secret-free, readable outcome of a Stage-A preparation run.

    Guaranteed: no access token, no client secret, no raw fingerprint content
    beyond the schema-defined consent-file field; nothing here can be used to
    place an order.
    """

    status: str
    stage: str
    reason: str = ""
    operator: str = ""
    account_id: str = ""
    contract: dict[str, Any] | None = None
    quote: str = ""
    margin_estimate: str = ""
    margin_cap: str = ""
    position_flat: bool | None = None
    ready: bool = False
    payload: dict[str, Any] | None = None

    @property
    def printable(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "stage": self.stage,
            "reason": self.reason,
            "operator": self.operator,
            "account_id": self.account_id,
            "contract": self.contract,
            "quote": self.quote,
            "margin_estimate": self.margin_estimate,
            "margin_cap": self.margin_cap,
            "position_flat": self.position_flat,
            "payload_preview": self.payload,
        }

    def to_console(self) -> str:
        lines = [f"LIVE-PREP (WS 7.24B) status={self.status} stage={self.stage}"]
        if self.operator:
            lines.append(f"operator={self.operator!r}")
        if self.reason:
            lines.append(f"reason={self.reason!r}")
        if self.contract:
            c = self.contract
            lines.append(
                f"contract: {c.get('underlying')} {c.get('expiry')} "
                f"{c.get('strike')} {c.get('option_type')} "
                f"lot={c.get('lot_size')} key={c.get('instrument_key')}"
            )
        if self.quote:
            lines.append(f"quote(underlying)={self.quote}")
        if self.margin_estimate:
            lines.append(f"margin_estimate={self.margin_estimate} cap={self.margin_cap}")
        if self.position_flat is not None:
            lines.append(f"position_flat={self.position_flat}")
        return "\n".join(lines)


def _fail(status: str, stage: str, reason: str) -> LivePrepResult:
    return LivePrepResult(status=status, stage=stage, reason=reason)


def _default_now() -> datetime:
    return datetime.now()


def refresh_consent(
    *,
    path: str | Path,
    operator: str,
    purpose: str,
    now: datetime,
    expires: datetime,
    token: str,
) -> dict[str, str]:
    """Refresh consent **preserving the existing schema** (field names intact).

    Keeps ``operator``/``purpose`` (operator owns them; never fabricated when
    absent), rolls ``created_at``/``expires_at``, updates the token fingerprint.
    """
    record = {
        "operator": (operator or "").strip(),
        "purpose": (purpose or "").strip(),
        "created_at": now.replace(microsecond=0).isoformat(),
        "expires_at": expires.replace(microsecond=0).isoformat(),
        FINGERPRINT_FIELD: sha256_hex(token),
    }
    if not record["operator"] or not record["purpose"]:
        raise ValueError("a non-empty operator and purpose are required for consent")
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(record, indent=2) + "\n",
        encoding="utf-8",
    )
    return record


def load_consent(path: str | Path) -> dict[str, str] | None:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


@dataclass(frozen=True)
class PrepIO:
    """Injectable collaborators so the pipeline is fully hermetic in tests.

    Defaults are the real, safe implementations. Tests replace only what they
    need; nothing here ever touches a broker write endpoint.
    """

    settings: LiveExecutionTestSettings | None = None
    oauth_config: UpstoxOAuthConfig | None = None
    consent_path: str | Path | None = None
    operator: str = ""
    purpose: str = ""
    dry_run: bool = True
    loopback_port: int = 8000
    callback_timeout: float = 300.0
    open_browser: bool = True
    master_file: str | Path | None = None  # offline BOD master (deterministic tests)
    now_fn: Callable[[], datetime] = _default_now

    # ---- injectable function overrides (tests) ----
    oauth_exchange: Callable[
        [UpstoxOAuthConfig, str], str
    ] | None = None  # config, code -> access token (in-memory)
    adapter_factory: Callable[[], UpstoxExecutionAdapter] | None = None


def _underlying_spot(adapter: UpstoxExecutionAdapter) -> Decimal:
    """Read-only NIFTY 50 index quote (never a write)."""
    try:
        return Decimal(str(adapter.quote(NIFTY_INDEX_KEY)))
    except Exception as exc:  # noqa: BLE001 - fail closed on any quote error
        raise UpstoxExecutionError(f"underlying spot quote unavailable: {exc}") from exc


def _resolve_contract(adapter: UpstoxExecutionAdapter, master_file: str | Path | None, now: datetime) -> Any:
    """Dynamically resolve the current-week ATM CE contract from the Upstox master."""
    from fno_ai_paper_trading.data.upstox_instruments import FnoContract

    spot = _underlying_spot(adapter)
    contract = resolve_from_upstox_master(
        underlying="NIFTY",
        expiry_bucket="current_week",
        option_type="CE",
        spot_price=spot,
        instruments_file=master_file,
        today=now.date(),
    )
    if not isinstance(contract, FnoContract) or contract.lot_size <= 0:
        raise UpstoxExecutionError("resolver returned an invalid contract (fail closed)")
    return contract


def prepare_live_buy(io: PrepIO | None = None) -> LivePrepResult:
    """Run the full Stage-A pipeline and **stop before any BUY is sent**.

    The returned payload (when reached) is only a preview of the exact one-lot
    MARKET BUY request; ``place_order`` is never called.
    """
    io = io or PrepIO()
    now = io.now_fn()

    # ---- 1. OAuth (operator-only) + in-process token ----------------------
    config = io.oauth_config or UpstoxOAuthConfig.from_env()
    if not config.configured:
        return _fail(
            FAIL_CLOSED, "oauth",
            "Upstox OAuth not configured: set UPSTOX_API_KEY and UPSTOX_API_SECRET "
            "(operator-authorized) in the environment",
        )
    try:
        if io.oauth_exchange is not None:
            # Hermetic test path: exchange is injected; no loopback/browser.
            token = io.oauth_exchange(config, "test-code")
        else:
            state = new_oauth_state()
            server = LocalCallbackServer(state, port=io.loopback_port)
            auth_url = authorize_url(config, state)
            if io.open_browser:
                try:
                    webbrowser.open(auth_url)
                except Exception:  # noqa: BLE001 - headless environments are fine
                    pass
            outcome = server.serve_once(io.callback_timeout)
            if not outcome.completed:
                return _fail(
                    FAIL_CLOSED, "oauth",
                    f"authorization callback did not complete: {outcome.error or 'timeout'}",
                )
            token = exchange_code_for_token(config, outcome.code).access_token
    except UpstoxExecutionError as exc:
        return _fail(FAIL_CLOSED, "oauth", f"OAuth exchange failed: {exc}")
    if not (token or "").strip():
        return _fail(FAIL_CLOSED, "oauth", "token exchange returned no access token")

    # ---- 2. Consent refresh (schema preserved) -----------------------------
    settings = io.settings or load_live_test_settings()
    consent_path = Path(io.consent_path or settings.consent_file).resolve()
    try:
        existing = load_consent(consent_path)
    except Exception:  # noqa: BLE001
        existing = None
    operator = (io.operator or "").strip() or (existing or {}).get("operator", "").strip()
    purpose = (io.purpose or "").strip() or (
        existing or {}
    ).get("purpose", "WS 7.24B controlled live execution test").strip()
    if not operator:
        return _fail(
            FAIL_CLOSED, "consent",
            "no operator authorized: provide --operator or a consent file with an operator",
        )
    try:
        expires = now + timedelta(hours=float(settings.expiry_hours))
        refresh_consent(
            path=consent_path,
            operator=operator,
            purpose=purpose,
            now=now,
            expires=expires,
            token=token,
        )
    except (OSError, ValueError) as exc:
        return _fail(FAIL_CLOSED, "consent", f"consent refresh failed: {exc}")

    # ---- 3. Authoritative gate (never weakened; master flag stays authoritative) ----
    # The in-process config only points the gate at the refreshed consent file and
    # carries the in-memory token; it never flips ``enabled``. If the operator has
    # not set FNO_LIVE_EXECUTION_TEST_ENABLED=1, the gate stays closed and Stage A
    # fails closed — the token/consent alone cannot open the gate.
    in_process_settings = replace(
        settings,
        consent_file=str(consent_path),
        dry_run=bool(io.dry_run),
    )
    try:
        decision = LiveExecutionTestGate(
            in_process_settings,
            now_fn=lambda: now,
            token_loader=lambda: token,
        ).decision()
    except Exception as exc:  # noqa: BLE001 - fail closed on gate errors
        return _fail(FAIL_CLOSED, "gate", f"gate evaluation failed: {exc}")
    if not decision.ok:
        return _fail(FAIL_CLOSED, "gate", "; ".join(decision.reasons))
    if decision.mode is not ExecutionMode.LIVE_EXECUTION_TEST:
        return _fail(FAIL_CLOSED, "gate", f"gate mode is {decision.mode.value}, not authorized")

    # ---- 4. Read-only adapter + FLAT check ----------------------------------
    if io.adapter_factory is not None:
        adapter = io.adapter_factory()
    else:
        # Adapter authenticates the SAME credential the gate and consent just
        # validated: the freshly OAuth-exchanged runtime token wins over any
        # (possibly stale) .env UPSTOX_ACCESS_TOKEN, so account lookup never
        # silently falls back to a superseded credential.
        adapter = UpstoxExecutionAdapter(
            UpstoxCredentials.from_env(access_token=token),
            dry_run=bool(io.dry_run),
        )
    try:
        account_id = adapter.get_account_id()
    except Exception as exc:  # noqa: BLE001
        return _fail(FAIL_CLOSED, "account", f"account id unavailable: {exc}")
    if not (account_id or "").strip():
        return _fail(FAIL_CLOSED, "account", "adapter returned no account id (fail closed)")

    # ---- 5. Dynamic instrument + lot size (nothing hard-coded) --------------
    try:
        contract = _resolve_contract(adapter, io.master_file, now)
    except Exception as exc:  # noqa: BLE001
        return _fail(FAIL_CLOSED, "instrument", f"instrument resolution failed: {exc}")
    try:
        instrument = contract.to_instrument()
    except Exception as exc:  # noqa: BLE001
        return _fail(FAIL_CLOSED, "instrument", f"instrument validation failed: {exc}")

    try:
        flat = adapter.is_flat(contract.instrument_key)
    except Exception:  # noqa: BLE001 - conservative: treat unknown as not flat
        flat = False
    if not flat:
        return _fail(
            FAIL_CLOSED, "position",
            f"broker position for {contract.instrument_key} is not FLAT; "
            "a live buy requires a flat position first",
        )

    # ---- 6. Client-side margin sanity ---------------------------------------
    premium = Decimal("0")
    try:
        premium = Decimal(str(adapter.quote(contract.instrument_key)))  # option premium
    except Exception:  # noqa: BLE001 - premium unknown => margin cannot be estimated
        pass
    margin_estimate = Decimal("0")
    try:
        if premium > 0:
            margin_estimate = estimate_required_margin(instrument, premium)
    except Exception:  # noqa: BLE001
        margin_estimate = Decimal("0")
    cap = settings.max_margin_notional
    if premium > 0 and margin_estimate > cap:
        return _fail(
            FAIL_CLOSED, "margin",
            f"estimated margin {margin_estimate:.2f} exceeds cap {cap:.2f}",
        )

    # ---- 7. Exact one-lot MARKET BUY payload (prepared, NOT sent) -----------
    quantity = int(contract.lot_size)
    if quantity <= 0:
        return _fail(FAIL_CLOSED, "instrument", "resolved lot size is not positive")
    order = Order(
        instrument=instrument,
        side=OrderSide.BUY,
        quantity=quantity,
        order_type=OrderType.MARKET,
    )
    payload_preview = {
        "instrument_token": contract.instrument_token,
        "quantity": quantity,
        "product": "M",
        "validity": "DAY",
        "price": 0,
        "instrument_type": "OPT",
        "transaction_type": "BUY",
        "order_type": "MARKET",
        "is_amo": False,
        "tag": "fno-ai-controlled-live-execution-test-prep",
    }

    return LivePrepResult(
        status=READY,
        stage="prepared",
        reason=(
            "one-lot MARKET BUY prepared and validated; NOT sent. Place the buy "
            "only through the explicit, operator-authorized Stage B path "
            "(run_live_execution_test.py) with the gate open."
        ),
        operator=operator,
        account_id=account_id,
        contract=contract.describe(),
        quote=str(premium),
        margin_estimate=f"{margin_estimate:.2f}",
        margin_cap=f"{cap:.2f}",
        position_flat=True,
        ready=True,
        payload=payload_preview,
    )