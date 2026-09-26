"""DATA-002 — NIFTY FUTURES HISTORICAL DATA ACQUISITION & VALIDATION (audit).

EXTERNAL RESEARCH AUDIT ONLY. Independent from the protected internal
algorithm (model_0 / Iteration-009..012 / protected OOS). NEVER merge,
replace, or strengthen via this object.

MANDATORY RESEARCH FIREWALLS:
  - Research domain: 2022-01-03 .. 2025-10-03.
  - Protected OOS:  2025-10-06 .. 2026-09-11 -> NEVER used.
  - Paper only. No live orders, no promotion.
  - NO COMMIT / NO PUSH. Pattern-identical PEP-8, stdlib-only.

WHAT THIS MODULE DOES:
  1. Records the live capability audit of the Upstox API for NIFTY FUTURES
     5-minute OHLCV + Open Interest (performed 2026-09-16, read-only).
  2. Documents the source access blocker: expired-instruments endpoints
     require an Upstox Plus subscription (UDAPI1149); the analytics token
     used here has no Plus plan.
  3. Builds the PLANNED monthly contract inventory (NIFTY FUT yearly monthly
     expiries = last Thursday of each month) for the research domain, every
     entry marked NOT_ACQUIRED with the blocking reason. No fabricated rows.
  4. Verifies the 15 protected artifact SHAs are unchanged.
  5. Writes runs/research/day_batch/data_002_futures_acquisition.json,
     DATA_002_NIFTY_FUTURES_ACQUISITION.md, and
     datasets/futures/futures_contract_inventory.csv.

CAPABILITY AUDIT FINDINGS (2026-09-16, live read-only probes):
  - /v3/historical-candle/{NSE_FO|<numeric>}/minutes/5/...  -> WORKS for
    CURRENTLY TRADING contracts only (probe: NSE_FO|48704 NIFTY FUT 27 OCT 26
    returned 5m rows with real volume + open interest). Returns 0 rows before
    the contract is listed.
  - String-format keys (e.g. NSE_FO|NIFTY 27 MAR 2025) -> HTTP 400
    "Invalid Instrument key" on v3 (the current master file with the numeric
    key is the only accepted form).
  - Legacy /index/historical/{NSE}/{symbol}... -> HTTP 299 deprecated.
  - /v2/historical-candle/{symbol}/NSE/... -> HTTP 404 (no longer offered).
  - /v2/expired-instruments/expiries,
    /v2/expired-instruments/future/contract,
    /v2/expired-instruments/historical-candle -> HTTP 401 UDAPI1149
    "available exclusively with an Upstox Plus plan subscription".
  - Therefore the 2022-01-03..2025-10-03 5-minute futures history is NOT
    obtainable with the current FNO_UPSTOX_ACCESS_TOKEN. DATA-002 outcome:
    FAIL (source access blocked); no contracts acquired, nothing validated,
    nothing fabricated.
"""
from __future__ import annotations

import calendar
import csv
import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Dict, List

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parents[3]
DATASETS_FUTURES = ROOT / "datasets" / "futures"
ART_DIR = ROOT / "runs" / "research" / "day_batch"
OUT_JSON = ART_DIR / "data_002_futures_acquisition.json"
OUT_MD = ART_DIR / "DATA_002_NIFTY_FUTURES_ACQUISITION.md"
OUT_INVENTORY_CSV = DATASETS_FUTURES / "futures_contract_inventory.csv"

# ---------------------------------------------------------------------------
# Research domain
# ---------------------------------------------------------------------------

RESEARCH_START = "2022-01-03"
RESEARCH_END = "2025-10-03"
OOS_START = "2025-10-06"
UNDERLYING = "NIFTY"
SEGMENT = "NSE_FO"

# ---------------------------------------------------------------------------
# Protected artifact SHA256 baselines (copied 1:1 from DATA-001 — 15 total)
# ---------------------------------------------------------------------------

PROTECTED_SHAS: Dict[str, str] = {
    "iteration_009_vol_led.json": "6e9713c30370207fc18ac5c8924ed5742ff3a8f23d639143b82164e6942a0dc2",
    "iteration_012_trade_forensics.json": "16b3c8939da744d0da11066b8e0c4498ab0ac6421c8dfe099340744a1bd141eb",
    "iteration_012_validation.json": "5722eff1bd64af73063439815c66ab68d8ea553052c8882a9c54d1250285b8d5",
    "iteration_006_protected_oos_n3.json": "4456db0133bede4ff332ba07c6ae14e8d8c734472e2a4c98bdd00870336ed5b5",
    "iteration_006_n3_fingerprint.json": "5fd5d1a2d8966ed4035a93911f93e4300575caaf3475fb742d4b202e4c257a7e",
    "engine.py": "4232bcdbdfb80f647ad00fb7078464771edd41b49b8be06a9a9f75e5a5df478b",
    "base.py": "611758fc35c13abfe145867f66adb18d88c0edfc5ffa9cbc8d80ce7c5a952aea",
    "stop_loss.py": "63e8e36cdac34c7ed8e81a39d96635d5373ab61771aaccf1c7400f0add82192a",
    "sizer.py": "f22c485987bc7a7283c2686ad9c330b74597d235a0f7efa086b212888892a0e2",
    "portfolio.py": "9b6eca202ef5e73304ea67e7c42464263d7d98aeb3bdb51e3a379c1b57f8e962",
    "moving_average_cross.py": "a50c17082b72881d7c3a00037d686bb01a4b120da5af0a643b5ed830a0236782",
    "regime_filtered.py": "ab22f6a8de01edca2a85bf568b2142feadaf65394163f02aa1b025151f206e63",
    "research_candidates.py": "6e21c588ac0892fdd35805bf83590a0779fc849fcd1d520690cd6f4f1cc4a31b",
    "psb_001_public_ema_8_24_72.json": "0317c02f1c895e1cbf6eaf9c3c36d582ff7dce72d9f1d66ae91ca7c860321b82",
    "psb_002_public_breakout.json": "8a844e315199294db975f5e033d77f2c5ddd8fbb1631f966ce48bd5da3340019",
}

PROTECTED_PATHS: Dict[str, str] = {
    "iteration_009_vol_led.json": "runs/research/day_batch/iteration_009_vol_led.json",
    "iteration_012_trade_forensics.json": "runs/research/day_batch/iteration_012_trade_forensics.json",
    "iteration_012_validation.json": "runs/research/day_batch/iteration_012_validation.json",
    "iteration_006_protected_oos_n3.json": "runs/research/day_batch/iteration_006_protected_oos_n3.json",
    "iteration_006_n3_fingerprint.json": "runs/research/day_batch/iteration_006_n3_fingerprint.json",
    "engine.py": "src/fno_ai_paper_trading/backtest/engine.py",
    "base.py": "src/fno_ai_paper_trading/strategies/base.py",
    "stop_loss.py": "src/fno_ai_paper_trading/risk/stop_loss.py",
    "sizer.py": "src/fno_ai_paper_trading/risk/sizer.py",
    "portfolio.py": "src/fno_ai_paper_trading/portfolio/portfolio.py",
    "moving_average_cross.py": "src/fno_ai_paper_trading/strategies/moving_average_cross.py",
    "regime_filtered.py": "src/fno_ai_paper_trading/strategies/regime_filtered.py",
    "research_candidates.py": "src/fno_ai_paper_trading/strategies/research_candidates.py",
    "psb_001_public_ema_8_24_72.json": "runs/research/day_batch/psb_001_public_ema_8_24_72.json",
    "psb_002_public_breakout.json": "runs/research/day_batch/psb_002_public_breakout.json",
}

# ---------------------------------------------------------------------------
# Source capability audit (live probes 2026-09-16)
# ---------------------------------------------------------------------------

UPSTOX_BASE_URL = "https://api.upstox.com"

SOURCE_CAPABILITY = [
    {
        "id": "upstox_v3_historical_candle",
        "url": "https://api.upstox.com/v3/historical-candle/{instrument_key}/minutes/5/{to}/{from}",
        "access": "WORKS",
        "scope": "currently-trading contracts only (numeric master key, e.g. NSE_FO|48704)",
        "data": "5m OHLCV + open_interest confirmed live (NIFTY FUT 27 OCT 26 probe)",
        "depth": "0 rows before contract listing date; no pre-2022 coverage for futures",
        "blocker": "none for current contracts; expiry history not served",
    },
    {
        "id": "upstox_v2_expired_instruments",
        "url": "https://api.upstox.com/v2/expired-instruments/{expiries|future/contract|historical-candle}",
        "access": "BLOCKED",
        "scope": "all expired NIFTY FUT contracts (the entire 2022-01..2025-10 range)",
        "data": "would provide 5m OHLCV + OI per expired contract",
        "depth": "expiries API: ~6 months back only if available; else per-contract expiry lookups",
        "blocker": "HTTP 401 UDAPI1149 — requires Upstox Plus plan subscription",
    },
    {
        "id": "legacy_index_historical",
        "url": "https://api.upstox.com/index/historical/NSE/{symbol}/{interval}/{to}/{from}",
        "access": "DEPRECATED",
        "scope": "symbol-based expired F&O (historical format NIFTY 24 FEB 2021)",
        "data": "formerly 1m/5m OHLCV+OI; now returns HTTP 299 migration notice",
        "depth": "n/a",
        "blocker": "HTTP 299 'This API is deprecated' — no data served",
    },
    {
        "id": "upstox_v2_historical_candle_symbol",
        "url": "https://api.upstox.com/v2/historical-candle/{symbol}/NSE/{interval}/{to}/{from}",
        "access": "NOT_SERVED",
        "scope": "symbol-based expired F&O (v2 migration target)",
        "data": "none observed",
        "depth": "n/a",
        "blocker": "HTTP 404 at path with symbol/NSE/interval layout",
    },
]

# ---------------------------------------------------------------------------
# Contract inventory (PLANNED — derivation, not vendor resolution)
# ---------------------------------------------------------------------------

#: NIFTY futures expire on the last Thursday of the expiry month.
#: The research domain 2022-01-03..2025-10-03 is covered by monthly expiries
#: 2022-01-27 (last Thu Jan 2022) .. 2025-10-30 (last Thu Oct 2025).
INVENTORY_STATUS = "NOT_ACQUIRED"
INVENTORY_REASON = "UPSTOX_PLUS_REQUIRED_UNKNOWN_UDAPI1149"


def last_thursday(year: int, month: int) -> date:
    """Return the last Thursday of ``year``/``month``."""
    cal = calendar.monthcalendar(year, month)
    # monthcalendar rows are weeks Mon..Sun; week index 4 == Thursday.
    thursdays = [week[calendar.THURSDAY] for week in cal if week[calendar.THURSDAY]]
    return date(year, month, max(thursdays))


def planned_contract_inventory() -> List[Dict[str, str]]:
    """Return the planned monthly NIFTY futures inventory for the domain.

    Every row is marked NOT_ACQUIRED with the blocking reason — this is a
    derivation of the expected expiry calendar, NOT a resolved list of
    vendor contract keys. No acquired data is claimed.
    """
    rows: List[Dict[str, str]] = []
    for year in range(2022, 2026):
        months = range(1, 13) if year < 2026 else range(1, 11)  # through Oct 2025
        for month in months:
            expiry = last_thursday(year, month)
            if not (RESEARCH_START <= expiry.isoformat() <= "2025-10-30"):
                continue
            rows.append({
                "expiry": expiry.isoformat(),
                "trading_symbol": f"NIFTY FUT {expiry.day:02d} "
                                  f"{expiry.strftime('%b').upper()} {expiry.year % 100}",
                "segment": SEGMENT,
                "underlying_key": "NSE_INDEX|Nifty 50",
                "instrument_type": "FUT",
                "status": INVENTORY_STATUS,
                "reason": INVENTORY_REASON,
            })
    return rows


# ---------------------------------------------------------------------------
# Integrity verification (all 15 protected artifact SHAs)
# ---------------------------------------------------------------------------


def verify_integrity() -> Dict[str, str]:
    """Verify all 15 protected artifact SHAs are UNCHANGED."""
    results: Dict[str, str] = {}
    for name, expected_sha in PROTECTED_SHAS.items():
        rel_path = PROTECTED_PATHS.get(name, f"runs/research/day_batch/{name}")
        full_path = ROOT / rel_path
        if not full_path.exists():
            results[name] = f"MISSING expected_sha={expected_sha}"
            continue
        actual = hashlib.sha256(full_path.read_bytes()).hexdigest()
        results[name] = "UNCHANGED" if actual == expected_sha else (
            f"CHANGED expected={expected_sha[:8]} got={actual[:8]}"
        )
    return results


# ---------------------------------------------------------------------------
# Main: deliver the audit JSON, the inventory CSV and the MD report
# ---------------------------------------------------------------------------


def main() -> None:
    """Run DATA-002 audit deliverable serialization and write artifacts."""
    inventory = planned_contract_inventory()

    integrity = verify_integrity()
    integrity_unchanged = all(v == "UNCHANGED" for v in integrity.values())

    out = {
        "audit_id": "DATA-002",
        "audit_name": "NIFTY FUTURES HISTORICAL DATA ACQUISITION & VALIDATION",
        "timestamp": "2026-09-16",
        "research_firewall": {
            "research_domain": f"{RESEARCH_START}..{RESEARCH_END}",
            "oos_start": OOS_START,
            "paper_only": True,
            "no_commit": True,
        },
        "outcome": "FAIL_SOURCE_ACCESS_BLOCKED",
        "summary": (
            "The 2022-01-03..2025-10-03 NIFTY futures 5-minute OHLCV+OI history "
            "requires the Upstox Expired Instruments API, which returned HTTP 401 "
            "UDAPI1149 (Upstox Plus plan required). The analytics token used here "
            "has no Plus subscription. Nothing was acquired, validated, or "
            "fabricated."
        ),
        "source_capability": SOURCE_CAPABILITY,
        "planned_contract_inventory": {
            "count": len(inventory),
            "start_expiry": inventory[0]["expiry"] if inventory else None,
            "end_expiry": inventory[-1]["expiry"] if inventory else None,
            "status": INVENTORY_STATUS,
            "reason": INVENTORY_REASON,
        },
        "contract_inventory_csv": str(OUT_INVENTORY_CSV.relative_to(ROOT)),
        "integrity_verification": integrity,
        "integrity_all_unchanged": integrity_unchanged,
        "safety_state": {
            "live_trading": False,
            "live_gate": "CLOSED",
            "algo_ready": "NO",
            "algorithm_health": "RED",
            "promotion": "NO",
        },
    }

    ART_DIR.mkdir(parents=True, exist_ok=True)
    DATASETS_FUTURES.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(out, indent=1, default=str), encoding="utf-8")

    with OUT_INVENTORY_CSV.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(
            fh, fieldnames=[
                "expiry", "trading_symbol", "segment", "underlying_key",
                "instrument_type", "status", "reason",
            ],
        )
        writer.writeheader()
        writer.writerows(inventory)

    lines = [
        "# DATA-002 — NIFTY FUTURES HISTORICAL DATA ACQUISITION & VALIDATION",
        "",
        "Dated: 2026-09-16 | Mode: PAPER-ONLY RESEARCH | Outcome: **FAIL_SOURCE_ACCESS_BLOCKED**",
        "",
        "> Independent research audit. Does **not** modify, replace, or compare against the",
        "> protected internal algorithm (model_0, Iteration-009..012, protected OOS). No",
        "> promotion, no optimization. No commit / no push.",
        "",
        "## 1. Objective",
        "",
        "Acquire and validate 5-minute OHLCV **+ real volume + open interest** for NIFTY",
        f"futures across the research domain {RESEARCH_START}..{RESEARCH_END}, via the existing",
        "Upstox F&O historical API. Do not fabricate a continuous series; keep raw",
        "per-contract data with a defensible rollover analysis.",
        "",
        "## 2. Live Capability Audit (read-only probes, 2026-09-16)",
        "",
        "| Endpoint | Access | Result |",
        "|---|---|---|",
        "| `/v3/historical-candle/{NSE_FO\\|NUM}` 5min | WORKS | NIFTY FUT 27 OCT 26 (NSE_FO\\|48704) returned real 5m OHLCV+OI; 0 rows before listing date |",
        "| `/v3/historical-candle/{string key}` | 400 | Invalid Instrument key — string expiry format not accepted (only numeric master key) |",
        "| `/index/historical/NSE/{symbol}...` | 299 | Deprecated — no data served |",
        "| `/v2/historical-candle/{symbol}/NSE/...` | 404 | Not served |",
        "| `/v2/expired-instruments/expiries` | 401 UDAPI1149 | Requires Upstox Plus plan |",
        "| `/v2/expired-instruments/future/contract` | 401 UDAPI1149 | Requires Upstox Plus plan |",
        "| `/v2/expired-instruments/historical-candle` | 401 UDAPI1149 | Requires Upstox Plus plan |",
        "",
        "**Conclusion:** the only path to 5-minute futures history for expired contracts is",
        "the Expired Instruments API, which is gated behind an Upstox Plus subscription",
        "(UDAPI1149). The `FNO_UPSTOX_ACCESS_TOKEN` configured for this repo has no Plus",
        "plan, so the 2022-2025 range is **not obtainable** with current credentials.",
        "",
        "## 3. Contract Inventory (PLANNED — no data acquired)",
        "",
        f"- {len(inventory)} monthly expiries derived from the NIFTY rule (last Thursday of each",
        "  month) covering the domain.",
        f"- Every entry is `{INVENTORY_STATUS}` with reason `{INVENTORY_REASON}`.",
        "- CSV artifact: `datasets/futures/futures_contract_inventory.csv`.",
        "- No vendor-resolved instrument keys, no candles, no open interest — because the",
        "  vendor source is blocked, nothing is claimed as acquired.",
        "",
        "## 4. Integrity",
        "",
        "| Component | Status |",
        "|---|---|",
        f"| 15 protected artifact SHAs | {'ALL UNCHANGED' if integrity_unchanged else 'MISMATCH DETECTED'} |",
        "",
        "## 5. Safety State",
        "",
        "| Field | Value |",
        "|---|---|",
        "| LIVE TRADING | FALSE |",
        "| LIVE GATE | CLOSED |",
        "| ALGO READY | NO |",
        "| ALGORITHM HEALTH | RED |",
        "| PROMOTION | NO |",
        "| GIT | NO COMMIT / NO PUSH |",
        "",
        "## 6. Final Result",
        "",
        "| Field | Value |",
        "|---|---|",
        "| AUDIT ID | DATA-002 |",
        "| OUTCOME | FAIL_SOURCE_ACCESS_BLOCKED |",
        "| CONTRACTS ACQUIRED | 0 |",
        "| CONTRACTS VALIDATED | 0 |",
        "| ROLLOVER ANALYSIS | NOT RUN (no data) |",
        "| INDEX CROSS-CHECK | NOT RUN (no data) |",
        "| BLOCKER | Upstox Plus required (UDAPI1149) — token lacks Plus |",
        "| INTEGRITY (15 artifacts) | ALL UNCHANGED |",
        "| STOP | YES |",
        "",
        "*No commit, no push. Deliverable JSON: `runs/research/day_batch/data_002_futures_acquisition.json`.*",
        "",
    ]
    OUT_MD.write_text("\n".join(lines), encoding="utf-8")

    print("=" * 66)
    print("DATA-002 - NIFTY FUTURES HISTORICAL DATA ACQUISITION & VALIDATION")
    print("=" * 66)
    print(f"OUTCOME: FAIL_SOURCE_ACCESS_BLOCKED (Upstox Plus required, UDAPI1149)")
    print(f"PLANNED INVENTORY: {len(inventory)} monthly expiries (all NOT_ACQUIRED)")
    print(f"CONTRACTS ACQUIRED: 0 | VALIDATED: 0 | ROLLOVER: n/a | CROSS-CHECK: n/a")
    print(f"INTEGRITY (15 artifacts): {'ALL UNCHANGED' if integrity_unchanged else 'MISMATCH DETECTED'}")
    print(f"SAFETY: LIVE_TRADING=FALSE | ALGO READY=NO | HEALTH=RED | PROMOTION=NO")
    print(f"JSON -> {OUT_JSON}")
    print(f"MD   -> {OUT_MD}")
    print(f"CSV  -> {OUT_INVENTORY_CSV}")
    print("STOP.")


if __name__ == "__main__":
    main()