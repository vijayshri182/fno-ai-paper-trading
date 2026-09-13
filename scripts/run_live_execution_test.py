"""WS 7.9 CLI: controlled Upstox live F&O execution integration test (PROJECT_PLAN §17p).

**This session: preparation only.** The default is a deterministic dry run that
exercises the entire architectural chain with a local in-memory adapter
(``--smoke``) or with the real Upstox adapter in read-only/dry-run mode
(``--upstox``) — in both cases **no order is ever sent to a live broker**.

A real send requires all of the following, and the script refuses anything else:

1. ``--live`` on the command line,
2. ``--confirm-live-enablement`` (explicit human confirmation),
3. the LIVE_EXECUTION_TEST gate fully open for the current process:
   ``FNO_LIVE_EXECUTION_TEST_ENABLED=1``, an operator-authored consent file
   whose fingerprint matches the environment ``UPSTOX_ACCESS_TOKEN``, all within
   the consent expiry window.

Two independent outcomes are reported, never conflated:

* **Outcome A** — execution integration PASS/FAIL (broker plumbing only), and
* **Outcome B** — Algorithm Health (RED/ALGO READY=NO), which is **unchanged** by
  this test.

The manager additionally re-applies RiskManager + Watchdog + market-session +
margin + overnight guards before any order and enforces the single-entry /
single-exit / 5-minute-hold / FLAT-reconciliation safeguards.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta  # noqa: E402
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from fno_ai_paper_trading.alerting.alerts import PAPER_TRADING_LABEL  # noqa: E402
from fno_ai_paper_trading.alerting.engine import AlertEngine, CollectingAlertSink, FileAlertSink  # noqa: E402
from fno_ai_paper_trading.alerting.health import Watchdog  # noqa: E402
from fno_ai_paper_trading.config.settings import (  # noqa: E402
    load_live_test_settings,
    load_settings,
)
from fno_ai_paper_trading.data.instrument_registry import get_research_instrument  # noqa: E402
from fno_ai_paper_trading.data.market_hours import NSE_TZ  # noqa: E402
from fno_ai_paper_trading.data.mock_provider import build_crossing_ohlcv  # noqa: E402
from fno_ai_paper_trading.data.upstox_provider import UpstoxHistoricalDataProvider  # noqa: E402
from fno_ai_paper_trading.execution.audit import ExecutionAudit  # noqa: E402
from fno_ai_paper_trading.execution.gate import LiveExecutionTestGate  # noqa: E402
from fno_ai_paper_trading.execution.instrument import resolve_fno_instrument  # noqa: E402
from fno_ai_paper_trading.execution.manager import LiveExecutionTestManager  # noqa: E402
from fno_ai_paper_trading.execution.memory import MemoryExecutionAdapter  # noqa: E402
from fno_ai_paper_trading.execution.risk import RiskPreflight  # noqa: E402
from fno_ai_paper_trading.execution.signal import CallPutSignal  # noqa: E402
from fno_ai_paper_trading.execution.upstox import UpstoxCredentials, UpstoxExecutionAdapter  # noqa: E402
from fno_ai_paper_trading.risk.manager import RiskManager  # noqa: E402
from fno_ai_paper_trading.utils.logging import setup_logging  # noqa: E402


SMOKE_NOW = datetime(2026, 9, 14, 12, 0)  # a Monday 12:00 IST — session OPEN


def _env_data_token() -> str:
    """Analytics/data-layer Upstox token (signal feed), not the execution token."""
    return os.getenv("FNO_UPSTOX_ACCESS_TOKEN", "").strip()


def _parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def _side(args) -> CallPutSignal | None:
    if args.side in ("CALL", "PUT"):
        return CallPutSignal(args.side)
    return None


def _build_signal_bars(args, underlying):
    if args.data_source == "smoke":
        bars = build_crossing_ohlcv(underlying)
        # Slice so the last bar is the first bar of the up-move: the fast MA
        # crosses above the slow MA there, giving a deterministic BUY -> CALL.
        for index in range(1, len(bars)):
            if bars[index].close > bars[index - 1].close:
                bars = bars[: index + 1]
                break
        # Rebase the synthetic crossing series onto fresh 5-minute timestamps
        # ending just before "now" so the Watchdog freshness check passes.
        import dataclasses

        from fno_ai_paper_trading.models.market import MarketPrice

        total = len(bars)
        rebased: list[MarketPrice] = []
        for i, bar in enumerate(bars):
            rebased.append(
                dataclasses.replace(
                    bar,
                    timestamp=SMOKE_NOW - timedelta(minutes=5 * (total - i)),
                )
            )
        return rebased, None
    token = args.token or _env_data_token()
    if not token:
        raise ValueError(
            "no Upstox analytics/data token; set FNO_UPSTOX_ACCESS_TOKEN (or pass --token). "
            "For an offline smoke use --data-source smoke."
        )
    provider = UpstoxHistoricalDataProvider(access_token=token)
    bars = provider.get_ohlcv(underlying, limit=args.bars)
    return bars, provider


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="run_live_execution_test",
        description="Controlled Upstox live F&O execution integration test (WS 7.9).",
        epilog=f"{PAPER_TRADING_LABEL} - default is a strict dry run; a live order requires the gate + explicit confirmation.",
    )
    parser.add_argument("--data-source", choices=["smoke", "upstox"], default="smoke",
                        help="smoke = offline in-memory adapter; upstox = real read-only adapter (dry-run unless --live)")
    parser.add_argument("--live", action="store_true",
                        help="ATTEMPT A REAL ORDER — requires the LIVE_EXECUTION_TEST gate AND --confirm-live-enablement")
    parser.add_argument("--confirm-live-enablement", action="store_true",
                        help="explicit human confirmation that a real order is authorized")
    parser.add_argument("--underlying", default="NIFTY 50",
                        help="registered research underlying used for the trend signal (default NIFTY 50)")
    parser.add_argument("--expiry", default="2026-12-24", metavar="YYYY-MM-DD",
                        help="option expiry (must be after today)")
    parser.add_argument("--strike", default="24500", help="option strike")
    parser.add_argument("--side", choices=["CALL", "PUT", "auto"], default="auto",
                        help="CALL/PUT leg; auto derives from the MA(5,21) trend signal")
    parser.add_argument("--instrument-key", default="", metavar="SEGMENT|SYMBOL",
                        help="Upstox instrument key of the option to trade (required for --data-source upstox)")
    parser.add_argument("--lot-size", type=int, default=75, help="F&O lot size of the expiry (default 75)")
    parser.add_argument("--holder", default="operator",
                        help="consent-file 'operator' label for the --smoke helpers")
    parser.add_argument("--token", default="", help="Upstox analytics/data token for the signal feed (env: FNO_UPSTOX_ACCESS_TOKEN)")
    parser.add_argument("--interval", default="5m", help="underlying signal bar interval (default 5m)")
    parser.add_argument("--bars", type=int, default=60, help="underlying bars to fetch for the signal")
    parser.add_argument("--hold-seconds", type=float, default=0.0,
                        help="override hold; default from FNO_LIVE_TEST_HOLD_SECONDS (300s in live)")
    parser.add_argument("--out", default="reports/execution/last_run_summary.json",
                        help="where to write the run summary JSON")
    args = parser.parse_args(argv)

    setup_logging()
    load_dotenv()
    settings = load_settings()
    live_settings = load_live_test_settings()

    dry_run = not args.live
    explicit_live = args.live
    real_upstox_send = args.data_source == "upstox" and not dry_run

    print("LIVE EXECUTION INTEGRATION TEST (PREPARATION) - " + PAPER_TRADING_LABEL)
    print(f"mode intent : {'DRY RUN (no order will be sent)' if not explicit_live else 'LIVE SEND (attempt)'}")
    print(f"data source : {args.data_source} | side intent: {args.side}")
    print("Outcome A = execution integration PASS/FAIL only; Outcome B = Algorithm Health (RED/NO) is unchanged.")

    # ---------------------------------------------------------------- gate
    gate = LiveExecutionTestGate(live_settings)
    gate_decision = gate.decision()
    if explicit_live and not gate_decision.ok:
        print("REFUSING to start a live send: the LIVE_EXECUTION_TEST gate is closed.", file=sys.stderr)
        for reason in gate_decision.reasons:
            print(f"  - {reason}", file=sys.stderr)
        print("A real order requires: FNO_LIVE_EXECUTION_TEST_ENABLED=1, an operator-authored", file=sys.stderr)
        print("consent file whose fingerprint matches UPSTOX_ACCESS_TOKEN, all inside its expiry window.", file=sys.stderr)
        return 2
    if not explicit_live:
        print("[gate] dry-run: gate advisory only; no write path exists in dry-run.")

    # ------------------------------------------------------- instrument
    underlying = get_research_instrument(args.underlying)
    option_key = args.instrument_key or f"NSE_FO|{args.underlying.upper()} 24 DEC 2026 {int(Decimal(args.strike)):d} {args.side if args.side != 'auto' else 'CE'}"
    try:
        instrument = resolve_fno_instrument(
            underlying=underlying.symbol,
            expiry=_parse_date(args.expiry),
            strike=Decimal(args.strike),
            option_type="CE" if args.side in ("CALL", "auto") else "PE",
            exchange_token=option_key,
            lot_size=args.lot_size,
        )
    except Exception as exc:  # noqa: BLE001 - invalid instrument means no run
        print(f"instrument validation failed: {exc}", file=sys.stderr)
        return 2

    # ------------------------------------------------------- signal bars
    try:
        bars, provider = _build_signal_bars(args, underlying)
    except Exception as exc:  # noqa: BLE001
        print(f"signal data unavailable: {exc}", file=sys.stderr)
        return 2

    # ------------------------------------------------------- adapter
    if args.data_source == "smoke":
        last_close = Decimal(str(bars[-1].close)) if bars else Decimal("25000")
        premium = last_close / 100
        adapter = MemoryExecutionAdapter(
            prices={option_key: premium},
            account_id="smoke-account",
            slippage=Decimal("0"),
        )
    else:
        # Execution-layer credentials come from the UPSTOX_* environment only.
        # The analytics/data token (FNO_UPSTOX_ACCESS_TOKEN) is never used here.
        credentials = UpstoxCredentials.from_env()
        adapter = UpstoxExecutionAdapter(
            credentials=credentials,
            dry_run=dry_run,
            instrument_tokens={},
        )

    # ------------------------------------------------------- manager
    risk_manager = RiskManager(settings)
    watchdog = Watchdog(now=lambda: SMOKE_NOW if args.data_source == "smoke" else datetime.now(NSE_TZ).replace(tzinfo=None))
    preflight = RiskPreflight(
        settings,
        risk_manager,
        watchdog,
        live_settings.max_margin_notional,
    )
    alert_sink = FileAlertSink(REPO_ROOT / "reports" / "execution" / "alerts.jsonl")
    alert_engine = AlertEngine(sinks=[alert_sink])
    audit_dir = REPO_ROOT / "reports" / "execution" / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    audit = ExecutionAudit(
        "script",
        path=audit_dir / f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jsonl",
    )

    clock = lambda: SMOKE_NOW if args.data_source == "smoke" else datetime.now(NSE_TZ).replace(tzinfo=None)  # noqa: E731
    default_hold = 0.2 if args.data_source == "smoke" else None
    hold_seconds = args.hold_seconds if args.hold_seconds and args.hold_seconds > 0 else default_hold

    manager = LiveExecutionTestManager(
        settings=settings,
        live_settings=live_settings,
        gate=gate,
        adapter=adapter,
        preflight=preflight,
        clock=clock,
        audit=audit,
        alert_engine=alert_engine,
        confirm_live_enablement=args.confirm_live_enablement and args.live,
        hold_seconds=hold_seconds,
    )

    side_pref = _side(args)
    if side_pref is not None:
        print(f"[plan] pinned leg: {side_pref.value}")
    else:
        print("[plan] auto leg from frozen MA(5,21) trend signal")

    result = manager.run(instrument, signal_bars=bars, side_preference=side_pref)

    summary_path = REPO_ROOT / args.out
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(result.to_dict(), indent=2), encoding="utf-8")

    print()
    print("RESULT (Outcome A - execution integration):")
    print(f"  outcome       : {result.outcome}  stage={result.stage.value}")
    print(f"  mode / dry_run: {result.mode.value} / {result.dry_run}")
    print(f"  entry         : {result.entry_order_id} ({result.entry_provider_order_id}) "
          f"fill={result.entry_fill_price}")
    print(f"  exit          : {result.exit_order_id} ({result.exit_provider_order_id}) "
          f"fill={result.exit_fill_price}")
    print(f"  position flat : {result.position_flat}")
    if result.reasons:
        print("  reasons:")
        for reason in result.reasons:
            print(f"    - {reason}")
    print(f"  audit path    : reports/execution/audit/<run_id>.jsonl")
    print(f"  summary       : {summary_path.relative_to(REPO_ROOT)}")
    print(f"OUTCOME B (Algorithm Health) -> {result.algorithm_health_note}")
    if real_upstox_send:
        print("REAL UPSTOX ORDER EXECUTED - verify broker positions manually")
    else:
        print("Dry-run intent respected: no real broker write happened.")

    return 0 if result.outcome == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())