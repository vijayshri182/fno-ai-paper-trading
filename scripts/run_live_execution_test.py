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
from fno_ai_paper_trading.data.upstox_instruments import resolve_from_upstox_master  # noqa: E402
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


def _real_round_trip_confirmed(
    real_upstox_send: bool,
    result: "ExecutionTestResult",
) -> bool:
    """Report-only decision: was a *confirmed* round trip proven by the result?

    ``real_upstox_send`` alone is intent (data-source + not dry-run) and can be
    true even when the run FAILed/ABORTed before any fill. A confirmed round trip
    requires the execution result to actually demonstrate it: PASS outcome, the
    COMPLETE terminal stage, a flat position, and *both* entry and exit fills.
    This is pure reporting -- it never writes, never gates, never contacts a
    broker.
    """
    return bool(
        real_upstox_send
        and result.outcome == "PASS"
        and result.stage is not None
        and result.stage.value == "COMPLETE"
        and result.position_flat
        and result.entry_fill_price is not None
        and result.exit_fill_price is not None
    )


def _execution_banner_note(
    real_upstox_send: bool,
    result: "ExecutionTestResult",
) -> str:
    """Pure reporting line for the execution-integration banner (Outcome A).

    Never prints the "REAL UPSTOX ORDER EXECUTED" wording from intent alone: it
    is produced only when :func:`_real_round_trip_confirmed` is true. Aborted /
    failed runs with unconfirmed fills explicitly report that no real order
    reached the broker, so the banner can never claim a real execution that the
    result does not prove.
    """
    if _real_round_trip_confirmed(real_upstox_send, result):
        return "REAL UPSTOX ORDER EXECUTED - verify broker positions manually"
    if real_upstox_send:
        return (
            "NO real Upstox order was confirmed at the broker: "
            f"outcome={result.outcome} "
            f"stage={result.stage.value if result.stage else 'UNKNOWN'} "
            "entry/exit fills not both confirmed / position not flat. "
            "Nothing real was written - verify none of the above."
        )
    return "Dry-run intent respected: no real broker write happened."


def _execution_banner_note(
    real_upstox_send: bool,
    result: "ExecutionTestResult",
) -> tuple[str, bool]:
    """Reporting-only banner decision for the live execution test.

    ``real_upstox_send`` is *intent* (data_source + not dry-run) and can be true
    even when the run FAILed/ABORTed before any fill, so it is never sufficient
    on its own to claim a real round trip. The REAL banner is produced only when
    the execution result actually demonstrates a confirmed round trip: PASS
    outcome, COMPLETE stage, position flat, and *both* entry and exit fills
    present. Pure reporting — never writes, never gates, never contacts a
    broker, reads nothing from the environment.

    Returns ``(note, confirmed_round_trip)``.
    """
    confirmed_round_trip = _real_round_trip_confirmed(real_upstox_send, result)
    if confirmed_round_trip:
        print("REAL UPSTOX ORDER EXECUTED - verify broker positions manually")
    elif real_upstox_send:
        note = (
            "NO real Upstox order was confirmed at the broker: "
            f"outcome={result.outcome} "
            f"stage={result.stage.value if result.stage else 'UNKNOWN'} "
            "entry/exit fills not both confirmed / position not flat. "
            "Nothing real was written - verify none of the above."
        )
    else:
        note = "Dry-run intent respected: no real broker write happened."
    return note, confirmed_round_trip


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
    provider = UpstoxHistoricalDataProvider(access_token=token, interval=args.interval)
    bars = _current_day_signal_bars(provider, underlying, args.interval, args.bars)
    return bars, provider


def _current_day_signal_bars(provider, underlying, interval: str, limit: int):
    """CURRENT-DAY signal bars: Intraday V3 for today + Historical V3 warm-up.

    * today's bars come exclusively from the Upstox Intraday Candle V3 endpoint
      (the dated Historical endpoint returns zero candles for the open day);
    * completed-day warm-up bars come from Historical V3 (previous session
      close and earlier);
    * the merged chronologically-ordered series is truncated to ``limit`` bars.

    Fails closed when the current trading day produced no Intraday V3 candles:
    the live signal must NEVER use historical data as today's data, fabricate
    the missing bars, or bypass freshness (the Watchdog is re-applied later in
    the preflight, unchanged).
    """
    now = datetime.now(NSE_TZ).replace(tzinfo=None)
    today = now.date()

    intraday = provider.get_intraday_ohlcv(underlying, interval)
    if not intraday:
        raise ValueError(
            "current trading day produced zero Intraday V3 candles; refusing to "
            "build a live signal from historical data (fail closed, NOT_READY)"
        )

    previous_close = datetime(today.year, today.month, today.day, 15, 30) - timedelta(days=1)
    history = provider.get_historical_ohlcv(underlying, interval, None, previous_close)

    merged = sorted(history + intraday, key=lambda bar: bar.timestamp)
    if not merged:
        raise ValueError("no signal bars available for the live test")
    if limit and limit > 0:
        return merged[-limit:]
    return merged


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
    parser.add_argument("--expiry", default="", metavar="YYYY-MM-DD",
                        help="option expiry (must be after today); empty = derive from --expiry-bucket")
    parser.add_argument("--expiry-bucket", default="", choices=["", "current_week", "next_week", "current_month", "next_month"],
                        help="calendar bucket for the expiry when --expiry is empty (default current_week)")
    parser.add_argument("--strike", default="", help="option strike; empty = ATM from the underlying spot")
    parser.add_argument("--side", choices=["CALL", "PUT", "auto"], default="auto",
                        help="CALL/PUT leg; auto derives the leg from the selected signal strategy")
    parser.add_argument("--signal-strategy", choices=["ma521", "composite-trend", "composite-mean-reversion", "composite-breakout"],
                        default="ma521",
                        help="signal strategy for the auto leg (default ma521 = the frozen champion; "
                             "composite-* = enhanced multi-indicator composite presets)")
    parser.add_argument("--instrument-key", default="", metavar="SEGMENT|SYMBOL",
                        help="explicit Upstox instrument key; when omitted the contract is resolved from the Upstox master file")
    parser.add_argument("--instruments-file", default="", metavar="PATH",
                        help="local NSE.json(.gz) master file to use instead of downloading it (offline/deterministic)")
    parser.add_argument("--lot-size", type=int, default=0,
                        help="F&O lot size override (0 = take the authoritative lot size from the master file)")
    parser.add_argument("--holder", default="operator",
                        help="consent-file 'operator' label for the --smoke helpers")
    parser.add_argument("--token", default="", help="Upstox analytics/data token for the signal feed (env: FNO_UPSTOX_ACCESS_TOKEN)")
    parser.add_argument("--interval", default="5m", help="underlying signal bar interval (default 5m)")
    parser.add_argument("--bars", type=int, default=60, help="underlying bars to fetch for the signal")
    parser.add_argument("--hold-seconds", type=float, default=0.0,
                        help="override hold; default from FNO_LIVE_TEST_HOLD_SECONDS (300s in live)")
    parser.add_argument("--out", default="reports/execution/last_run_summary.json",
                        help="where to write the run summary JSON")
    parser.add_argument("--force-one-lot-round-trip", "--force", dest="force_one_lot_round_trip",
                        action="store_true",
                        help="WS 7.24B OPTION B: pin an explicit --side CALL/PUT and do exactly one "
                             "one-lot BUY -> confirmed fill -> SELL the ACTUAL filled quantity -> flat. "
                             "The LIVE_EXECUTION_TEST gate stays mandatory: this only forces a single "
                             "lot round trip, it never re-opens/weakens the gate or consent.")
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
    option_type_pref = "PE" if args.side == "PUT" else "CE"

    # ------------------------------------------------------- signal bars
    try:
        bars, provider = _build_signal_bars(args, underlying)
    except Exception as exc:  # noqa: BLE001
        print(f"signal data unavailable: {exc}", file=sys.stderr)
        return 2

    instrument_tokens: dict[str, int] = {}
    resolved_contract = None
    if args.data_source == "upstox" and not args.instrument_key:
        # Resolve the real, currently-tradable contract from the authoritative
        # Upstox master file. Strike/expiry are not hard-coded: expiry comes
        # from --expiry or --expiry-bucket, and an empty strike resolves to the
        # ATM strike from the underlying spot (last signal bar).
        spot = None
        if not args.strike and bars:
            spot = Decimal(str(bars[-1].close))
        try:
            resolved_contract = resolve_from_upstox_master(
                underlying=underlying.symbol,
                expiry=_parse_date(args.expiry) if args.expiry else None,
                expiry_bucket=(
                    args.expiry_bucket if args.expiry_bucket
                    else ("current_week" if not args.expiry else None)
                ),
                strike=Decimal(args.strike) if args.strike else None,
                option_type=option_type_pref,
                spot_price=spot,
                instruments_file=(args.instruments_file or None),
            )
        except Exception as exc:  # noqa: BLE001 - invalid instrument means no run
            print(f"instrument resolution failed: {exc}", file=sys.stderr)
            return 2
        instrument = resolved_contract.to_instrument()
        instrument_tokens = resolved_contract.to_adapter_tokens()
        print("[instrument] resolved from the Upstox master file:")
        for field, value in resolved_contract.describe().items():
            print(f"  {field}: {value}")
        if args.expiry_bucket or not args.expiry:
            print(f"  expiry note: bucket '{args.expiry_bucket or 'current_week'}' "
                  f"selected expiry {resolved_contract.expiry.isoformat()}")
        if not args.strike:
            print(f"  strike note: ATM strike {resolved_contract.strike} "
                  f"selected from spot {spot}")
    else:
        if args.instrument_key:
            missing = [
                name for name, value in (("--expiry", args.expiry), ("--strike", args.strike))
                if not value
            ]
            if missing:
                print(
                    "--instrument-key requires " + " and ".join(missing) + "; omit "
                    "--instrument-key to auto-resolve the current contract from the "
                    "Upstox master file.",
                    file=sys.stderr,
                )
                return 2
        # Synthetic path (smoke) or a manually-pinned key: the expiry/strike are
        # explicit operator inputs here, not broker-resolved defaults. The smoke
        # defaults are synthetic fixtures for the offline crossing series only.
        option_expiry = _parse_date(args.expiry) if args.expiry else date(2026, 12, 24)
        option_strike = int(Decimal(args.strike)) if args.strike else (int(bars[-1].close) if bars else 25000)
        option_key = args.instrument_key or (
            f"NSE_FO|{args.underlying.upper()} {option_expiry:%d %b %Y} {option_strike} "
            f"{args.side if args.side != 'auto' else 'CE'}"
        )
        option_lot = args.lot_size if args.lot_size > 0 else (75 if args.data_source == "smoke" else 75)
        try:
            instrument = resolve_fno_instrument(
                underlying=underlying.symbol,
                expiry=option_expiry,
                strike=Decimal(option_strike),
                option_type=option_type_pref,
                exchange_token=option_key,
                lot_size=option_lot,
            )
        except Exception as exc:  # noqa: BLE001 - invalid instrument means no run
            print(f"instrument validation failed: {exc}", file=sys.stderr)
            return 2
        option_key = instrument.exchange_token or option_key

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
            instrument_tokens=instrument_tokens,
        )
        if resolved_contract is not None and dry_run:
            # Sanitized evidence: the exact order body a real send would POST,
            # reconstructed deterministically from the resolved contract. It
            # carries no credentials — only the public contract token. For an
            # auto leg the transaction_type is decided by the signal at runtime.
            if args.side == "auto":
                print("[payload] dry-run: order body for a real send (credentials removed; "
                      "transaction_type decided by the signal at runtime):")
            else:
                print("[payload] dry-run: order body a real send would POST to /v2/order/place (credentials removed):")
            print(json.dumps({
                "instrument_token": resolved_contract.instrument_token,
                "quantity": int(resolved_contract.lot_size),
                "product": "M",
                "validity": "DAY",
                "price": 0,
                "tag": "fno-ai-controlled-live-execution-test",
                "instrument_type": "OPT",
                "transaction_type": ("BUY" if option_type_pref == "CE" else "SELL") if args.side != "auto" else "<signal>",
                "order_type": "MARKET",
                "is_amo": False,
            }, indent=2))

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

    from fno_ai_paper_trading.strategies.composite import MultiIndicatorStrategy
    from fno_ai_paper_trading.strategies.moving_average_cross import MovingAverageCrossStrategy

    if args.signal_strategy == "ma521":
        signal_strategy = MovingAverageCrossStrategy(fast=5, slow=21)
    else:
        mode = {
            "composite-trend": "trend",
            "composite-mean-reversion": "mean_reversion",
            "composite-breakout": "breakout",
        }[args.signal_strategy]
        signal_strategy = MultiIndicatorStrategy(mode=mode)

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
        force_one_lot_round_trip=args.force_one_lot_round_trip,
        hold_seconds=hold_seconds,
        signal_strategy=signal_strategy,
    )

    side_pref = _side(args)
    if side_pref is not None:
        print(f"[plan] pinned leg: {side_pref.value}")
    else:
        print(f"[plan] auto leg from {signal_strategy.name} "
              f"({'MA(5,21)' if args.signal_strategy == 'ma521' else args.signal_strategy.replace('composite-', '')} preset)")

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
    banner_note, round_trip_confirmed = _execution_banner_note(real_upstox_send, result)
    print(banner_note)

    return 0 if result.outcome == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())