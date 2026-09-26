#!/usr/bin/env python3
"""Read-only forensic reconstruction of the 5M Donchian 20/10 experiment for
the single out-of-sample trading day 2026-09-21.

Finding this audit records up-front: the recorded 5M experiment
(5M_DIRECTIONAL_OPTIONS_EXPERIMENT) covers 2025-05-22..2025-08-14 ONLY. A later
recorded 5m run (exp-2d-7) starts 2026-09-22. Day 2026-09-21 was acquired into
the fresh-OOS pool on 2026-09-22T09:19:48 with status NOOP and was never
consumed by any experiment. Therefore NOTHING for 2026-09-21 is RECORDED at the
experiment level: no checkpoint, no fills, no closures, no entry approvals, no
decisions.

What this script produces is a strict RECONSTRUCTION, using only:
  * recorded market bars (datasets + fresh-OOS pool, each hash-checked)
  * the validated Donchian state-machine replica (replay_donchian, validated
    4392/4392 against the recorded 2025 checkpoint decisions)
  * the frozen contract (decide() transition table, decision grid 09:20..15:15)

Every reconstructed field is labeled RECONSTRUCTED. Every order-lifecycle field
(fills, prices, quantity, positions, exits, P&L) is intentionally left as the
literal marker NOT AVAILABLE FROM RECORDED ARTIFACT - a substitute is NEVER
presented as recorded data.

Outputs (into <repo>/reports/forensics, nothing else in the repo is written):
  * donchian_5m_2026-09-21_signal_order_audit.md
  * donchian_5m_2026-09-21_signal_order_audit.csv
  * donchian_5m_2026-09-21_signal_order_audit.json
No strategy, parameter, contract or recorded artifact is modified. No commit.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from collections import Counter
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fno_ai_paper_trading.research.donchian_5m_forensics import (  # noqa: E402
    DONCHIAN_ENTRY_PERIOD,
    DONCHIAN_EXIT_PERIOD,
    SIGNAL_MAP,
    canonical_hash,
    jsonable,
    replay_donchian,
    ticks_repr,
)
from fno_ai_paper_trading.experiments.directional_5m.contract import (  # noqa: E402
    Action,
    Leg,
    Signal15m,
    decide,
    decision_moments,
    signal_to_15m,
)

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "reports" / "forensics"

TARGET_DAY = date(2026, 9, 21)

# Recorded bar sources, in chronological order. Each file is a recorded
# artifact; hashes are verified at runtime.
BAR_SOURCES = [
    {
        "path": REPO / "datasets" / "upstox_Nifty_50_5m_20220103_20260911.csv",
        "sha256": "81a6dffa13276cabe40aa2cbf6fef9fae5086448f6786373e3961685944e894e",
        "role": "recorded daily 5m history through 2026-09-11",
    },
    {
        "path": REPO / "datasets" / "upstox_Nifty_50_5m_20260915_20260915.csv",
        "sha256": "78202a545a586afe4b5be9d2228a96a66df99eca1f41cb8824d3a16d20a014b5",
        "role": "recorded session 2026-09-15 (fresh-OOS pool source)",
    },
    {
        "path": REPO / "datasets" / "upstox_Nifty_50_5m_20260916_20260916.csv",
        "sha256": "08da0b22dc1004b9e6686da998cda0aa60fa93e214aa9c98b7e706e06380b880",
        "role": "recorded session 2026-09-16 (fresh-OOS pool source)",
    },
    {
        "path": REPO / "data" / "fresh_oos" / "NIFTY_50_5m" / "2026-09-17" / "data.csv",
        "sha256": "7a2a9959f20ee19f02d546d3accf715fa984be7a508556282ea8672c93b40bf7",
        "role": "recorded session 2026-09-17 (fresh-OOS pool)",
    },
    {
        "path": REPO / "data" / "fresh_oos" / "NIFTY_50_5m" / "2026-09-18" / "data.csv",
        "sha256": "ea39a13ea80f8ca144a21849cfab412ca434c7760952f1affae82ce454fc2b68",
        "role": "recorded session 2026-09-18 (fresh-OOS pool)",
    },
    {
        "path": REPO / "data" / "fresh_oos" / "NIFTY_50_5m" / "2026-09-21" / "data.csv",
        "sha256": "3ac516b274a044f10a104c85c068107b6f8db8b31f89a2109770b675ef083fcb",
        "role": "the audited trading day 2026-09-21 (fresh-OOS pool)",
    },
]

DONCHIAN = "DonchianBreakout(20, 10)"
MISSING = "NOT AVAILABLE FROM RECORDED ARTIFACT"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_bars(path: Path) -> list[dict]:
    out = []
    with open(path, "r", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            out.append({
                "timestamp": row["timestamp"],
                "open": row["open"],
                "high": row["high"],
                "low": row["low"],
                "close": row["close"],
                "volume": row.get("volume", "0") or "0",
                "open_interest": row.get("open_interest", "0") or "0",
            })
    return out


def verify_sources() -> list[str]:
    notes = []
    for src in BAR_SOURCES:
        p = src["path"]
        if not p.exists():
            sys.exit(f"[FAIL] missing recorded bar source: {p}")
        h = sha256_file(p).lower()
        notes.append(f"{p.name}: recorded sha256 match = {h == src['sha256']}")
        if h != src["sha256"]:
            sys.exit(f"[FAIL] bar source hash mismatch {p}: {h[:16]} != {src['sha256'][:16]}")
    return notes


def closes(bar_by_ts: dict[str, dict], moment: datetime, kmin: int) -> Decimal | None:
    ts = (moment + timedelta(minutes=kmin - 5)).isoformat()
    b = bar_by_ts.get(ts)
    return Decimal(b["close"]) if b else None


def apply_actions(actions: tuple[Action, ...], leg_before: Leg) -> Leg:
    leg = leg_before
    for a in actions:
        if a is Action.ENTER_CALL:
            leg = Leg.CALL
        elif a is Action.ENTER_PUT:
            leg = Leg.PUT
        elif a is Action.EXIT_CALL or a is Action.EXIT_PUT:
            leg = Leg.FLAT
        elif a is Action.SWITCH_CALL_TO_PUT:
            leg = Leg.PUT
        elif a is Action.SWITCH_PUT_TO_CALL:
            leg = Leg.CALL
    return leg


def action_str(actions: tuple[Action, ...]) -> str:
    return ",".join(a.value for a in actions) if actions else "NO_ACTION"


def build_rows(session_notes: list[str]):
    src_notes = verify_sources()
    session_notes.extend(src_notes)

    stream: list[dict] = []
    session_counts: dict[str, int] = {}
    for src in BAR_SOURCES:
        bars = load_bars(src["path"])
        stream.extend(bars)
        session_counts[src["path"].name] = len(bars)

    ts_all = [b["timestamp"] for b in stream]
    if ts_all != sorted(ts_all):
        sys.exit("[FAIL] assembled bar stream is not chronological")
    if len(set(ts_all)) != len(ts_all):
        sys.exit("[FAIL] duplicate timestamps in assembled bar stream")
    if not any(t.startswith(TARGET_DAY.isoformat()) for t in ts_all):
        sys.exit("[FAIL] target day not present in recorded stream")

    # validated Donchian replica over the FULL recorded stream (state carries
    # across sessions exactly like the engine's aggregated history)
    replay = replay_donchian(stream, entry_period=DONCHIAN_ENTRY_PERIOD,
                             exit_period=DONCHIAN_EXIT_PERIOD)
    replay_by_ts = {stream[i]["timestamp"]: replay[i] for i in range(len(stream))}
    day_bars = [b for b in stream if b["timestamp"].startswith(TARGET_DAY.isoformat())]
    day_by_ts = {b["timestamp"]: b for b in day_bars}

    # decision grid 09:20..15:15 (72 instants); reference candle opens at T-5m
    moments = decision_moments(TARGET_DAY)
    rows: list[dict] = []
    leg: Leg = Leg.FLAT
    for k, moment in enumerate(moments):
        ref_ts = (moment - timedelta(minutes=5)).isoformat()
        ref = day_by_ts.get(ref_ts)
        if ref is None:
            sys.exit(f"[FAIL] missing reference candle for decision {moment}")
        rd = replay_by_ts[ref_ts]
        signal: Signal15m = signal_to_15m(rd.signal)
        dec = decide(leg, signal)
        leg_after = apply_actions(dec.actions, leg)

        fwd = {h: closes(day_by_ts, moment, h) for h in (5, 10, 15)}
        ref_close = Decimal(ref["close"])
        if signal is Signal15m.BULLISH:
            signed = {h: (fwd[h] - ref_close) if fwd[h] is not None else None for h in fwd}
        elif signal is Signal15m.BEARISH:
            signed = {h: (ref_close - fwd[h]) if fwd[h] is not None else None for h in fwd}
        else:
            signed = {h: None for h in fwd}

        next_signal = None
        if k + 1 < len(moments):
            nts = (moments[k + 1] - timedelta(minutes=5)).isoformat()
            next_signal = SIGNAL_MAP[replay_by_ts[nts].signal]

        rows.append({
            "moment": moment.isoformat(),
            "ref_candle": ref_ts,
            "ref_open": ref["open"],
            "ref_high": ref["high"],
            "ref_low": ref["low"],
            "ref_close": ref["close"],
            "replay_signal": rd.signal,               # RECONSTRUCTED (BUY/SELL/HOLD)
            "signal": signal.value,                    # RECONSTRUCTED intent
            "kind": rd.kind,                           # RECONSTRUCTED Donchian geometry
            "reason": dec.reason,                      # RECONSTRUCTED from frozen decide()
            "state_before": rd.state_before,           # RECONSTRUCTED Donchian 0/1/-1
            "state_after": rd.state_after,
            "prior_hi": ticks_repr(rd.prior_hi),
            "prior_lo": ticks_repr(rd.prior_lo),
            "exit_hi": ticks_repr(rd.exit_hi),
            "exit_lo": ticks_repr(rd.exit_lo),
            "option_action": MISSING,                  # nothing recorded -> must not fabricate
            "position": MISSING,
            "exit": MISSING,
            "prescribed_action": action_str(dec.actions),   # RECONSTRUCTED request (not a fill)
            "prescribed_reason": dec.reason,
            "leg_before": leg.value,
            "leg_after": leg_after.value,
            "next_moment": moments[k + 1].isoformat() if k + 1 < len(moments) else MISSING,
            "next_signal": next_signal if next_signal is not None else MISSING,
            "fwd_close_5": ticks_repr(fwd[5]),
            "fwd_close_10": ticks_repr(fwd[10]),
            "fwd_close_15": ticks_repr(fwd[15]),
            "fwd_move_5": ticks_repr(signed[5]),
            "fwd_move_10": ticks_repr(signed[10]),
            "fwd_move_15": ticks_repr(signed[15]),
        })
        leg = leg_after

    sig = {
        "decision_points": len(rows),
        "signal_counts": dict(Counter(r["signal"] for r in rows)),
        "prescribed_action_counts": dict(Counter(r["prescribed_action"] for r in rows)),
        "leg_after_snapshot": dict(Counter(r["leg_after"] for r in rows)),
    }
    return rows, session_counts, sig


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, default=OUT, help="output directory")
    args = ap.parse_args()

    session_notes: list[str] = []
    rows, session_counts, sig = build_rows(session_notes)

    meta = {
        "audit_day": TARGET_DAY.isoformat(),
        "method": ("RECONSTRUCTION. No recorded experiment/checkpoint/fills/decisions exist for "
                   "2026-09-21; signals/states/reasons are reconstructed with the validated "
                   f"{DONCHIAN} replica (validated 4392/4392 vs the recorded 2025 checkpoint) over "
                   "recorded bars; option-lifecycle fields are recorded-artifact markers."),
        "recorded_sources": [{"path": str(s["path"].relative_to(REPO)), "role": s["role"]}
                             for s in BAR_SOURCES],
        "source_sha256_verified": session_notes,
        "session_bar_counts": session_counts,
        "decision_grid": "09:20..15:15 (72/day) on completed 5m candles",
        "strategy": {"name": "DonchianBreakout", "entry_period": DONCHIAN_ENTRY_PERIOD,
                     "exit_period": DONCHIAN_EXIT_PERIOD,
                     "signal": "validated replica (replay_donchian)"},
        "contract": {"decide": "directional_5m.contract.decide (frozen, reuses directional_15m)",
                     "flat_start": "each trading day starts FLAT (EOD flatten rule)"},
        "unavailable_marker": MISSING,
        "generated_at": datetime.utcnow().isoformat() + "Z",
    }
    canon = canonical_hash({"meta": {k: v for k, v in meta.items() if k != "generated_at"},
                            "session": sig, "decisions": [jsonable(r) for r in rows]})
    meta["output_hash"] = canon
    payload = {"_meta": jsonable(meta), "session": jsonable(sig),
               "decisions": [jsonable(r) for r in rows]}

    args.out.mkdir(parents=True, exist_ok=True)
    csv_path = args.out / "donchian_5m_2026-09-21_signal_order_audit.csv"
    cols = [
        "moment", "ref_candle", "ref_open", "ref_high", "ref_low", "ref_close",
        "replay_signal", "signal", "kind", "reason", "state_before", "state_after",
        "prior_hi", "prior_lo", "exit_hi", "exit_lo",
        "option_action", "position", "exit",
        "prescribed_action", "prescribed_reason", "leg_before", "leg_after",
        "next_moment", "next_signal",
        "fwd_close_5", "fwd_close_10", "fwd_close_15",
        "fwd_move_5", "fwd_move_10", "fwd_move_15",
    ]
    with open(csv_path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for r in rows:
            w.writerow([str(r.get(c, "")) for c in cols])

    json_path = args.out / "donchian_5m_2026-09-21_signal_order_audit.json"
    json_path.write_text(json.dumps(payload, indent=1, sort_keys=False), encoding="utf-8")

    md_path = args.out / "donchian_5m_2026-09-21_signal_order_audit.md"
    md_path.write_text(render_md(meta, sig, rows), encoding="utf-8")

    print("WROTE", csv_path)
    print("WROTE", json_path)
    print("WROTE", md_path)
    print(json.dumps({
        "decision_points": sig["decision_points"],
        "signal_counts": sig["signal_counts"],
        "prescribed_action_counts": sig["prescribed_action_counts"],
        "leg_after_snapshot": sig["leg_after_snapshot"],
        "source_verified": len(session_notes),
        "output_hash": canon,
    }, indent=1))
    return 0


def _f(v) -> str:
    return "—" if v is None or v == "" else str(v)


def render_md(meta: dict, sig: dict, rows: list[dict]) -> str:
    L: list[str] = []
    L.append("# 5M Donchian 20/10 — 2026-09-21 Signal & Order Audit (RECONSTRUCTION)")
    L.append("")
    L.append(f"_Read-only forensic audit. **Nothing was recorded for {TARGET_DAY.isoformat()} at the "
             "experiment level.** Signals/states/reasons below are RECONSTRUCTED from recorded bars via "
             "the validated Donchian(20,10) replica + frozen contract. Option-lifecycle fields carry the "
             "literal marker `NOT AVAILABLE FROM RECORDED ARTIFACT` — a substitute is never presented as "
             "a record._")
    L.append("")
    L.append("## 1. Executive summary")
    L.append("")
    L.append(f"- Audit day: **{TARGET_DAY.isoformat()}** ({sig['decision_points']} decision points, "
             "09:20..15:15).")
    L.append(f"- Recorded experiment coverage: **2025-05-22..2025-08-14** only. A later recorded 5m run "
             "(exp-2d-7) starts **2026-09-22**. Day 2026-09-21 entered the fresh-OOS pool "
             "`data/fresh_oos/NIFTY_50_5m/2026-09-21/` with status **NOOP** and was never consumed.")
    L.append(f"- **No checkpoint / decisions / fills / closures / entry approvals / order lifecycle "
             f"exists for 2026-09-21.** Option lifecycle = `{meta['unavailable_marker']}`.")
    L.append(f"- Reconstructed signals: counts `{sig['signal_counts']}`; prescribed (frozen-contract) "
             f"actions `{sig['prescribed_action_counts']}`.")
    L.append("")
    L.append("## 2. Provenance (recorded inputs, hash-verified)")
    L.append("")
    L.append("| source | role | bars | sha256 verified |")
    L.append("|---|---|---:|---|")
    for s in meta["recorded_sources"]:
        name = Path(s["path"]).name
        note = next((n for n in meta["source_sha256_verified"] if n.startswith(name + ":")), "?")
        L.append(f"| `{s['path']}` | {s['role']} | {meta['session_bar_counts'].get(name, '?')} | {note} |")
    L.append("")
    L.append("## 3. Why there is no recorded artifact for 2026-09-21")
    L.append("")
    L.append("- The recorded experiment checkpoint `5m_directional_options.2025-08-14.json` covers "
             "2025-05-22..2025-08-14 (61 sessions, 4392 decisions, last_processed 2025-08-14T15:25:00).")
    L.append("- The fresh-OOS manifest `data/fresh_oos/fresh_oos_manifest.json` reports 2026-09-21 as "
             "`NOOP` (acquired 2026-09-22T09:19:48, `accepted: [\"2026-09-21\"]`), never used by an "
             "experiment. No `5m_directional_options.2026-09-21.json` checkpoint exists.")
    L.append("- 09-14 = `NO_DATA` in the pool; 09-15/09-16 = `NOOP_ESTABLISHED`; 09-17/09-18 = `NOOP`. "
             "The recorded sessions before 09-21 thus end 09-18 (main history ends 2026-09-11).")
    L.append("")
    L.append("## 4. Method")
    L.append("")
    L.append("1. **Recorded bars only** are loaded (hash-verified) in chronological order: "
             "`datasets/upstox_Nifty_50_5m_20220103_20260911.csv` + 09-15 + 09-16 + fresh-OOS "
             "09-17/09-18/{target-day}.")
    L.append("2. The **validated replica** `replay_donchian` (entry 20, exit 10) runs over the full "
             "stream; the same replica reproduces the recorded 2025 checkpoint decisions 4392/4392.")
    L.append("3. Each decision instant maps the replica BUY/SELL/HOLD through the frozen "
             "`signal_to_15m` (BULLISH/BEARISH/NEUTRAL), exactly like the signal adapter.")
    L.append("4. The **frozen contract** `decide()` maps (leg, signal) → prescribed action/reason. "
             "The day opens FLAT (EOD-flatten rule); the leg chain is RECONSTRUCTED, not a record.")
    L.append("5. Forward closes use **recorded {target-day} bars only** (never later sessions).")
    L.append("")
    L.append("## 5. Reconstructed per-decision chain")
    L.append("")
    L.append("SEQUENCE: SIGNAL → (Donchian state) → REASON → OPTION ACTION → POSITION → EXIT → "
             "NEXT SIGNAL → ACTUAL MARKET OUTCOME.")
    L.append("")
    L.append("| T | candle | close | signal | reason | opt action | position | exit | next sig | fwd5 | fwd10 | fwd15 |")
    L.append("|---|---|--:|--|--|--|--|--|--|--:|--:|--:|")
    for r in rows:
        L.append(
            f"| {r['moment'][11:16]} | {r['ref_candle'][11:16]} | {r['ref_close']} | {r['signal']} | "
            f"{r['reason']} | `{_f(r['option_action'])}` | `{_f(r['position'])}` | `{_f(r['exit'])}` | "
            f"{r['next_signal']} | {_f(r['fwd_move_5'])} | {_f(r['fwd_move_10'])} | {_f(r['fwd_move_15'])} |"
        )
    L.append("")
    L.append("`fwd5/10/15` = recorded underlying move (index pts, signed in the reconstructed signal "
             "direction) over the +5/+10/+15 minutes after the decision. `close` = close of the "
             "completed reference candle (opened at T−5m).")
    L.append("")
    L.append("### 5.1 Donchian reference bands (RECONSTRUCTED)")
    L.append("")
    L.append("| T | signal | state→state | entry hi(20) | entry lo(20) | exit hi(10) | exit lo(10) |")
    L.append("|---|---|--:|--:|--:|--:|--:|")
    for r in rows:
        L.append(f"| {r['moment'][11:16]} | {r['signal']} | {r['state_before']}→{r['state_after']} | "
                 f"{r['prior_hi']} | {r['prior_lo']} | {r['exit_hi']} | {r['exit_lo']} |")
    L.append("")
    L.append("## 6. Contract-prescribed actions (RECONSTRUCTED — requested, never a recorded fill)")
    L.append("")
    L.append("The frozen `decide()` table yields the following prescribed actions from the reconstructed "
             "signals (starting FLAT; a day always flattens at 15:20 EOD):")
    L.append("")
    L.append(f"- prescribed-action counts: `{sig['prescribed_action_counts']}`")
    L.append(f"- resulting-leg counts: `{sig['leg_after_snapshot']}`")
    L.append("")
    L.append("These were the strategy's **requests** under the frozen contract. **No fill, price, "
             "quantity, position or realized P&L is producible** — none was recorded. The markers "
             "`NOT AVAILABLE FROM RECORDED ARTIFACT` in `option_action`/`position`/`exit` are final.")
    L.append("")
    L.append("## 7. Direction transitions (reconstructed Donchian state 0/1/−1)")
    L.append("")
    L.append("Only rows where the replica Donchian **state changes** (all others are holds):")
    L.append("")
    trans = [r for r in rows if r["state_before"] != r["state_after"]]
    if trans:
        for r in trans:
            L.append(f"- {r['moment'][11:16]}: state {r['state_before']}→{r['state_after']} "
                     f"({r['kind']}); reconstructed signal {r['signal']}")
    else:
        L.append("- none (replica Donchian state never changed)")
    L.append("")
    L.append("## 8. Actual market outcome (recorded bars only)")
    L.append("")
    L.append("For each reconstructed **directional** decision point, the recorded underlying moves "
             "(index pts, signed in the signal direction) over the +5/+10/+15 minutes after the "
             "decision:")
    L.append("")
    sig_rows = [r for r in rows if r["signal"] != "NEUTRAL"]
    for r in sig_rows:
        L.append(f"- {r['moment'][11:16]} {r['signal']} ({r['reason']}): +5m = {_f(r['fwd_move_5'])}, "
                 f"+10m = {_f(r['fwd_move_10'])}, +15m = {_f(r['fwd_move_15'])} index pts in direction.")
    L.append("")
    L.append("## 9. Look-ahead check")
    L.append("")
    L.append("- Replica references use **only completed candles strictly before** each decision "
             "instant (entry 20 / exit 10 of prior bars).")
    L.append("- Forward closes after a decision use recorded bars **after** that instant only.")
    L.append("- No bar from a later session/day is used in any decision computation. None of the "
             "reconstructed fields read the 2026-09-22 run or any later artifact.")
    L.append("")
    L.append("## 10. Evidence-only conclusions")
    L.append("")
    L.append("1. **No recorded 5M Donchian 20/10 experiment ran on 2026-09-21.** Any BUY/SELL/CALL/PUT "
             "order on that day is unprovable — and **no such claim is made here**.")
    L.append("2. Reconstructed signal/state/reason values are a deterministic function of the recorded "
             "bars and reproduce the recorded 2025 decisions identically when replayed there "
             "(4392/4392).")
    L.append("3. Anything labeled `NOT AVAILABLE FROM RECORDED ARTIFACT` must be treated as not "
             "provable; a substitute is never presented as a record.")
    L.append("")
    L.append("## 11. Known limitations")
    L.append("")
    L.append("- Reconstruction is **not** a recorded trace: had an experiment run on 09-21, actual "
             "orders could differ (sizing approval, risk/stop gates, broker fills).")
    L.append("- Volume/open_interest are 0 in the recorded pool files; no order book or option quotes "
             "were recorded, so option prices/premia are unavailable.")
    L.append("- 09-14 is `NO_DATA` (not a recorded session) and is correctly absent from the stream; "
             "session continuity is exactly what the recorded pool provides.")
    L.append("")
    L.append("## 12. Reproduction")
    L.append("")
    L.append("- generator: `scripts/generate_donchian_5m_2026_09_21_signal_order_audit.py`")
    L.append(f"- output_hash: `{meta['output_hash']}`")
    L.append("- command: `.venv\\Scripts\\python.exe scripts/generate_donchian_5m_2026_09_21_signal_order_audit.py`")
    L.append("")
    return "\n".join(L)


if __name__ == "__main__":
    raise SystemExit(main())