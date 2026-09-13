"""WS 7.8 CLI: continuous paper-trading agent (PROJECT_PLAN §17h).

Always-running paper-only orchestrator over the injected market provider:

* ``MARKET_CLOSED``   -> booked offline jobs (learning loop / replay capture)
* ``MARKET_OPEN``     -> completed-candle **paper** trading via ``PaperSession``

PAPER TRADING ONLY - NO LIVE ORDER. Live market data never implies live broker
execution; the only execution path is ``PaperBroker``.

Modes
-----
* ``--smoke``           deterministic synthetic smoke run (no network, no creds)
* ``--csv <dataset.csv>`` offline replay of a saved dataset
* ``--upstox``          poll Upstox historical candles near-real-time
* ``--once`` / ``--cycles N`` / ``--forever`` control how long the agent runs

Every cycle checkpoints both the session snapshot and the agent record, so a
restart resumes exactly where the loop stopped. A heartbeat JSON lands under
``reports/algorithm_state/paper_agent.json`` for the dashboard.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from fno_ai_paper_trading.agent.agent import AgentConfig, ContinuousPaperAgent  # noqa: E402
from fno_ai_paper_trading.agent.jobs import (  # noqa: E402
    build_learning_cycle_job,
    build_replay_capture_job,
)
from fno_ai_paper_trading.alerting.alerts import PAPER_TRADING_LABEL  # noqa: E402
from fno_ai_paper_trading.alerting.engine import AlertEngine, FileAlertSink  # noqa: E402
from fno_ai_paper_trading.config.settings import Environment, load_settings  # noqa: E402
from fno_ai_paper_trading.data.dataset_store import load_dataset  # noqa: E402
from fno_ai_paper_trading.data.instrument_registry import get_research_instrument  # noqa: E402
from fno_ai_paper_trading.data.intervals import canonical_interval, interval_minutes  # noqa: E402
from fno_ai_paper_trading.data.mock_provider import (  # noqa: E402
    InMemoryMarketDataProvider,
    build_crossing_ohlcv,
)
from fno_ai_paper_trading.data.upstox_provider import UpstoxHistoricalDataProvider  # noqa: E402
from fno_ai_paper_trading.utils.logging import setup_logging  # noqa: E402


def _build_agent(args, settings) -> ContinuousPaperAgent:
    interval = canonical_interval(args.interval)
    if interval is None:
        raise ValueError(f"unknown interval {args.interval!r}")
    minutes = interval_minutes(interval)
    if minutes is None:
        raise ValueError("the agent interval must be a fixed-minute interval")

    instrument = get_research_instrument("NIFTY 50")
    provider = None
    data_source = ""

    if args.upstox:
        token = args.token or os.getenv("FNO_UPSTOX_ACCESS_TOKEN", "").strip()
        if not token:
            raise ValueError(
                "no Upstox analytics/data access token; set FNO_UPSTOX_ACCESS_TOKEN (or pass --token)"
            )
        provider = UpstoxHistoricalDataProvider(access_token=token)
        data_source = "Upstox historical data"
        clock = None
    else:
        bars = None
        if args.smoke:
            bars = build_crossing_ohlcv(instrument)
        elif args.csv:
            stored = load_dataset(args.csv)
            bars = stored.bars
            instrument = stored.instrument()
        else:
            raise ValueError("choose a data source: --smoke, --csv FILE, or --upstox")
        provider = InMemoryMarketDataProvider(
            instruments=[instrument], history={instrument.symbol: bars}
        )
        data_source = "Synthetic smoke data" if args.smoke else f"Offline CSV replay ({args.csv})"

        as_of = (
            datetime.fromisoformat(args.as_of)
            if args.as_of
            else (bars[-1].timestamp + timedelta(minutes=minutes))
        ) if bars else datetime.now()
        stepping = [as_of]

        def clock() -> datetime:
            current = stepping[0]
            stepping[0] = current + timedelta(minutes=minutes)
            return current

    sandbox = settings.environment in (Environment.TEST, Environment.DEVELOPMENT)
    if sandbox and not args.note_sandbox and not args.smoke:
        print("sandbox environment detected; the agent will paper-trade offline data only")
    if sandbox:
        print(f"[sandbox] environment={settings.environment.value} ({PAPER_TRADING_LABEL})")

    alert_engine = AlertEngine(
        sinks=[FileAlertSink(REPO_ROOT / "reports" / "agent" / "agent-alerts.jsonl")]
    )

    jobs = []
    if not args.no_jobs:
        jobs = [build_learning_cycle_job(), build_replay_capture_job()]

    config = AgentConfig(
        settings=settings,
        provider=provider,
        instrument=instrument,
        interval=interval,
        state_dir=args.state_dir,
        session_name=args.name,
        clock=clock,
        alert_engine=alert_engine,
        jobs=jobs,
        quantity=args.quantity,
        allow_sandbox=sandbox,
    )
    agent = ContinuousPaperAgent(config)
    return agent, data_source


def _print_heartbeat(agent: ContinuousPaperAgent) -> None:
    hb = agent.heartbeat
    print(
        f"  [{hb.cycle_at}] state={hb.state:<14} phase={hb.market_phase:<9} "
        f"consumed={hb.consumed_bars} orders={hb.orders_submitted} fills={hb.fills} "
        f"trades={hb.trades} equity={hb.equity} open={hb.open_quantity} "
        f"safety={hb.safety_decision}"
    )
    if hb.last_error:
        print(f"    error: {hb.last_error}")


def _write_heartbeat(agent: ContinuousPaperAgent) -> None:
    target = REPO_ROOT / "reports" / "algorithm_state" / "paper_agent.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(agent.heartbeat.to_dict(), indent=2), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="run_paper_agent",
        description="Continuous paper-trading agent (WS 7.8).",
        epilog=f"{PAPER_TRADING_LABEL} - live market data never implies live broker execution.",
    )
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--smoke", action="store_true", help="deterministic synthetic smoke run")
    source.add_argument("--csv", metavar="FILE", help="saved dataset CSV to replay offline")
    source.add_argument("--upstox", action="store_true", help="poll Upstox historical candles")
    runner = parser.add_mutually_exclusive_group()
    runner.add_argument("--once", action="store_true", help="run a single cycle and exit")
    runner.add_argument("--cycles", type=int, default=1, help="run N cycles then exit")
    parser.add_argument("--forever", action="store_true", help="loop until interrupted")
    parser.add_argument("--interval", default="5m", help="canonical bar interval (default 5m)")
    parser.add_argument("--quantity", type=int, default=1)
    parser.add_argument("--name", default=None, help="session name (default <symbol>_<interval>)")
    parser.add_argument("--state-dir", default="paper_state", help="checkpoint directory")
    parser.add_argument("--token", default="", help="Upstox analytics/data access token (env: FNO_UPSTOX_ACCESS_TOKEN)")
    parser.add_argument("--poll-seconds", type=float, default=60.0, help="seconds between cycles")
    parser.add_argument("--as-of", default=None, help="deterministic decision time ISO (offline modes)")
    parser.add_argument("--no-jobs", action="store_true", help="disable closed-market jobs")
    parser.add_argument("--note-sandbox", action="store_true", help="suppress sandbox notice")
    args = parser.parse_args(argv)

    setup_logging()
    load_dotenv()
    settings = load_settings()

    if args.forever:
        args.cycles = float("inf")

    print(f"CONTINUOUS PAPER-TRADING AGENT - {PAPER_TRADING_LABEL}")
    print("Live market data is used for PAPER execution only; no live broker orders.")

    try:
        agent, data_source = _build_agent(args, settings)
    except (ValueError, FileNotFoundError) as exc:
        print(f"startup failed: {exc}", file=sys.stderr)
        return 2

    print(f"agent booting: env={settings.environment.value} source={data_source} "
          f"state_dir={args.state_dir}")
    try:
        agent.start()
    except EnvironmentError as exc:
        print(f"refusing to start: {exc}", file=sys.stderr)
        return 3

    print(f"agent run id : {agent.run_id}")
    cycles = 1 if args.once else (args.cycles if not args.forever else None)
    count = 0
    try:
        while True:
            if count > 0 and args.poll_seconds > 0:
                time.sleep(args.poll_seconds)
            result = agent.cycle()
            count += 1
            _print_heartbeat(agent)
            _write_heartbeat(agent)
            if cycles is not None and count >= cycles:
                break
    except KeyboardInterrupt:
        print("\ninterrupted; checkpointing final state")
        print(f"agent state   : {agent.heartbeat.state} ({agent.heartbeat.safety_decision})")
        return 0
    except Exception as exc:  # noqa: BLE001 - report and exit cleanly
        print(f"agent stopped on error: {exc}", file=sys.stderr)
        _write_heartbeat(agent)
        return 1

    print(f"agent finished: {agent.cycles_ran} cycle(s); state={agent.heartbeat.state}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())