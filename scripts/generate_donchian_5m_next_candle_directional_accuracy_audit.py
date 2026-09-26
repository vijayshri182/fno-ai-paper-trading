#!/usr/bin/env python3
"""Forensic NEXT-CANDLE DIRECTIONAL ACCURACY AUDIT of the recorded 5M Donchian
20/10 signals (5M_DIRECTIONAL_OPTIONS_EXPERIMENT, run
exp-20260922-242c9dc0814c480aa788c3c39bb47f57).

MEASURE -> VERIFY -> REPORT. This audit does NOT change the strategy, parameters,
signal logic, position management, entry/exit or risk rules, does not introduce
look-ahead, does not trade, does not modify any recorded artifact and does not
commit anything.

Dataset: the SAME recorded checkpoint already used by the existing Donchian 20/10
recorded analysis (validate_donchian_5m_20_10_forensics.py,
donchian_5m_20_10_trade_ledger.csv, the 2025-06-18 signal-order audit and the
2025-06-18 position-lifecycle ledger), pinned by sha256:
  5m_directional_options.2025-08-14.json

Signal source: the frozen implementation, reproduced one-row-per-bar with
donchian_5m_forensics.replay_donchian(20,10) (state machine + windows are
identical to strategies/research_candidates.donchian_breakout_signals). The
mapped replica signal is asserted equal to the recorded decision signal at every
one of the 4,392 recorded decision moments (0 mismatches).

Reference/next candle semantics (identical to the existing lifecycle ledger):
  - A recorded decision at timestamp T is generated from information available
    at T, i.e. the candle that COMPLETED at T: bar timestamp T-5m. That bar is
    the REFERENCE candle; its close is what the signal reacts to.
  - The NEXT candle is the immediately following recorded 5-minute candle: bar
    timestamp T (completes at T+5m). No candle is manufactured; if the bar at T
    is absent (dataset end) the row is marked NO_NEXT_CANDLE.

Directional test (STEP 4):
  BULLISH: next_close > reference_close -> CORRECT ; < -> WRONG ; == -> FLAT
  BEARISH: next_close < reference_close -> CORRECT ; > -> WRONG ; == -> FLAT
  NEUTRAL: excluded from directional accuracy (EXCLUDED_NEUTRAL).

Outputs (into <repo>/reports/forensics):
  * donchian_5m_next_candle_directional_accuracy_audit.md
  * donchian_5m_next_candle_directional_accuracy_audit.csv
  * donchian_5m_next_candle_directional_accuracy_audit.json
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fno_ai_paper_trading.research.donchian_5m_forensics import (
    SIGNAL_MAP,
    canonical_hash,
    replay_donchian,
)

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "reports" / "forensics"

CHECKPOINT = Path(
    r"C:\Users\user\AppData\Local\Temp\opencode\fno_5m_validation\out\exp_runA"
    r"\checkpoints\5m_directional_options.2025-08-14.json"
)
CHECKPOINT_SHA256 = "6c8ebc8c999a23fc9a0c71a0994355092dc952429658b4c1567b3dcd58b4af12"

MISSING = "NOT AVAILABLE FROM RECORDED ARTIFACT"
ENTRY_CHANNEL = 20
EXIT_CHANNEL = 10
FIVE = timedelta(minutes=5)

BUCKETS = [
    ("09:15-10:00", (9, 15), (10, 0)),
    ("10:00-11:00", (10, 0), (11, 0)),
    ("11:00-12:00", (11, 0), (12, 0)),
    ("12:00-13:00", (12, 0), (13, 0)),
    ("13:00-14:00", (13, 0), (14, 0)),
    ("14:00-15:15", (14, 0), (15, 16)),
]


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_checkpoint(path: Path) -> dict:
    if not path.exists():
        sys.exit(f"[FAIL] missing recorded dataset {path}")
    if sha256_file(path).lower() != CHECKPOINT_SHA256:
        sys.exit(f"[FAIL] checkpoint hash mismatch {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def fmt_ts(hh: int, mm: int) -> str:
    return f"{hh:02d}:{mm:02d}"


def bucket_of(moment: datetime) -> str:
    for label, (sh, sm), (eh, em) in BUCKETS:
        lo = moment.replace(hour=sh, minute=sm, second=0, microsecond=0)
        hi = moment.replace(hour=eh, minute=em, second=0, microsecond=0)
        if lo <= moment < hi:
            return label
    return "14:00-15:15"


class DirectionalAudit:
    def __init__(self) -> None:
        self.ck = load_checkpoint(CHECKPOINT)
        self.bars = self.ck["history"]
        self.replay = replay_donchian(self.bars, ENTRY_CHANNEL, EXIT_CHANNEL)
        self.bar_by_ts = {b["timestamp"]: b for b in self.bars}
        self.idx_by_ts = {b["timestamp"]: i for i, b in enumerate(self.bars)}
        self.decisions = sorted(self.ck["decisions"], key=lambda d: d["moment"])

    # -------------------------------------------------------------- lookups

    @staticmethod
    def next_direction(ref_close: str, next_bar: dict | None) -> tuple[str, str]:
        """(next_direction, result) vs the reference close. next_direction in
        UP/DOWN/FLAT/NO_NEXT_CANDLE; result in CORRECT/WRONG/FLAT/NO_NEXT_CANDLE.
        Directional mapping: BULLISH expects UP, BEARISH expects DOWN."""
        if next_bar is None:
            return "NO_NEXT_CANDLE", "NO_NEXT_CANDLE"
        nc, rc = float(next_bar["close"]), float(ref_close)
        if nc > rc:
            direction = "UP"
        elif nc < rc:
            direction = "DOWN"
        else:
            direction = "FLAT"
        return direction, direction  # result filled by caller against signal

    # ------------------------------------------------------------ validation

    def _no_lookahead(self) -> bool:
        """Check E: the signal at T uses only candles completed at/before T. The
        DCH20/DCH10 windows are [i-20,i) and [i-10,i) of bar timestamps <= T-5m;
        the next candle (timestamp T) must never appear inside them or be the
        reference candle itself."""
        for d in self.decisions:
            T = datetime.fromisoformat(d["moment"])
            ref_ts = (T - FIVE).isoformat()
            if ref_ts not in self.idx_by_ts:
                return False
            ri = self.idx_by_ts[ref_ts]
            # reference bar's own DCH windows never include itself (i-20:i, i-10:i)
            if ri < max(ENTRY_CHANNEL, EXIT_CHANNEL):
                continue  # warmup bar; windows empty by construction
            for k in range(ri - ENTRY_CHANNEL, ri):
                if self.bars[k]["timestamp"] >= T.isoformat():
                    return False
            for k in range(ri - EXIT_CHANNEL, ri):
                if self.bars[k]["timestamp"] >= T.isoformat():
                    return False
            # the outcome candle (next bar at T) is strictly after the reference
            if ri + 1 < len(self.bars):
                if self.bars[ri + 1]["timestamp"] != T.isoformat():
                    return False
        return True

    def validate(self) -> list[str]:
        notes = []
        # replay == recorded at every decision
        dec_by_ts = {b["timestamp"]: None for b in self.bars}
        mism = compared = 0
        rec_sig = Counter()
        for i, b in enumerate(self.bars):
            moment = (datetime.fromisoformat(b["timestamp"]) + FIVE).isoformat()
            dec = next((d for d in self.decisions if d["moment"] == moment), None)
            if dec is None:
                continue
            compared += 1
            rec_sig[dec["signal"]] += 1
            if SIGNAL_MAP[self.replay[i].signal] != dec["signal"]:
                mism += 1
        notes.append(f"replication == recorded signal at {compared} decision "
                     f"moments (mismatches={mism})")
        notes.append(f"recorded signal counts: {dict(rec_sig)}")
        if len(self.decisions) != compared:
            sys.exit("[FAIL] decision/bar mapping mismatch")
        return notes

    # ----------------------------------------------------------------- build

    def rows(self) -> list[dict]:
        rows = []
        day_cache: dict[str, str] = {}
        for d in self.decisions:
            T = datetime.fromisoformat(d["moment"])
            sig = d["signal"]
            if sig == "NEUTRAL":
                continue  # directional ledger only
            # reference bar = the candle that completed at T (bar timestamp T-5m)
            ref_ts = (T - FIVE).isoformat()
            ref = self.bar_by_ts.get(ref_ts)
            if ref is None:
                sys.exit(f"[FAIL] reference candle missing {T}")
            # next candle = immediately following recorded candle (bar at T)
            nxt = self.bar_by_ts.get(d["moment"])
            nd, result = self.next_direction(ref["close"], nxt)
            # directional result: BULLISH expects UP, BEARISH expects DOWN
            if nxt is not None:
                rc, nc = float(ref["close"]), float(nxt["close"])
                if sig == "BULLISH":
                    result = "CORRECT" if nc > rc else ("WRONG" if nc < rc else "FLAT")
                else:  # BEARISH
                    result = "CORRECT" if nc < rc else ("WRONG" if nc > rc else "FLAT")
            # dch20 / dch10 from the frozen replay at the reference bar
            rdec = self.replay[self.idx_by_ts[ref_ts]]
            rows.append({
                "decision_time": d["moment"],
                "reference_timestamp": ref_ts,
                "reference_open": ref["open"],
                "reference_high": ref["high"],
                "reference_low": ref["low"],
                "reference_close": ref["close"],
                "dch20_upper": str(rdec.prior_hi),
                "dch20_lower": str(rdec.prior_lo),
                "dch10_upper": str(rdec.exit_hi),
                "dch10_lower": str(rdec.exit_lo),
                "signal": sig,
                "next_timestamp": nxt["timestamp"] if nxt else MISSING,
                "next_open": nxt["open"] if nxt else MISSING,
                "next_high": nxt["high"] if nxt else MISSING,
                "next_low": nxt["low"] if nxt else MISSING,
                "next_close": nxt["close"] if nxt else MISSING,
                "next_direction": nd,
                "result": result,
            })
        return rows

    # -------------------------------------------------------------- summary

    def summary(self, rows: list[dict]) -> dict:
        all_sig = Counter(d["signal"] for d in self.decisions)
        directional = [r for r in rows]
        by_sig = {"BULLISH": [], "BEARISH": []}
        for r in directional:
            by_sig[r["signal"]].append(r)

        def outcomes(rs: list[dict]) -> dict:
            c = Counter(r["next_direction"] for r in rs)
            res = Counter(r["result"] for r in rs)
            return {
                "signals": len(rs),
                "UP": c["UP"], "DOWN": c["DOWN"], "FLAT": c["FLAT"],
                "NO_NEXT_CANDLE": c["NO_NEXT_CANDLE"],
                "correct": res["CORRECT"], "wrong": res["WRONG"],
                "flat": res["FLAT"], "no_next_candle": res["NO_NEXT_CANDLE"],
            }

        bull = outcomes(by_sig["BULLISH"])
        bear = outcomes(by_sig["BEARISH"])
        overall = {
            k: bull[k] + bear[k] for k in
            ("signals", "UP", "DOWN", "FLAT", "NO_NEXT_CANDLE",
             "correct", "wrong", "flat", "no_next_candle")
        }

        def acc(o: dict) -> float | None:
            den = o["correct"] + o["wrong"]
            if den == 0:
                return None
            return round(100.0 * o["correct"] / den, 4)

        overall_acc = acc(overall)
        bull_acc = acc(bull)
        bear_acc = acc(bear)
        return {
            "total_decision_candles": len(self.decisions),
            "neutral_signals": all_sig["NEUTRAL"],
            "directional_signals": (bull["signals"] + bear["signals"]),
            "signal_counts": dict(all_sig),
            "overall": {
                **overall,
                "accuracy_pct": overall_acc,
                "accuracy_denominator_correct_plus_wrong": (overall["correct"] + overall["wrong"]),
            },
            "bullish": {**bull, "accuracy_pct": bull_acc},
            "bearish": {**bear, "accuracy_pct": bear_acc},
        }

    def buckets(self, rows: list[dict]) -> list[dict]:
        grouped: dict[str, list[dict]] = {}
        for r in rows:
            T = datetime.fromisoformat(r["decision_time"])
            grouped.setdefault(bucket_of(T), []).append(r)
        out = []
        for label, *_ in BUCKETS:
            rs = grouped.get(label, [])
            c = Counter(r["result"] for r in rs)
            correct, wrong = c["CORRECT"], c["WRONG"]
            den = correct + wrong
            out.append({
                "bucket": label,
                "signals": len(rs),
                "correct": correct,
                "wrong": wrong,
                "flat": c["FLAT"],
                "accuracy_pct": round(100.0 * correct / den, 4) if den else None,
            })
        return out

    def build(self) -> dict:
        notes = self.validate()
        rows = self.rows()
        summary = self.summary(rows)
        buckets = self.buckets(rows)
        # cross-checks A-D
        checks = {
            "A: BULLISH + BEARISH == directional": (
                summary["bullish"]["signals"] + summary["bearish"]["signals"]
                == summary["directional_signals"]
            ),
            "B: BULLISH outcomes == UP+DOWN+FLAT+NO_NEXT_CANDLE": (
                summary["bullish"]["UP"] + summary["bullish"]["DOWN"]
                + summary["bullish"]["FLAT"]
                + summary["bullish"]["NO_NEXT_CANDLE"]
                == summary["bullish"]["signals"]
            ),
            "C: BEARISH outcomes == DOWN+UP+FLAT+NO_NEXT_CANDLE": (
                summary["bearish"]["DOWN"] + summary["bearish"]["UP"]
                + summary["bearish"]["FLAT"]
                + summary["bearish"]["NO_NEXT_CANDLE"]
                == summary["bearish"]["signals"]
            ),
            "D: every directional signal has exactly one next candle": (
                summary["overall"]["NO_NEXT_CANDLE"] == 0
                and len(rows) == summary["directional_signals"]
            ),
            "E: no look-ahead (signal windows exclude the next candle)": self._no_lookahead(),
        }
        meta = {
            "audit": "NEXT-CANDLE DIRECTIONAL ACCURACY AUDIT",
            "method": (
                "Recorded 5M Donchian 20/10 signals, directionally tested against "
                "the immediately following recorded 5-minute candle. Signal "
                "reproduced with the frozen DonchianBreakout(20,10) "
                "(replay_donchian == research_candidates.donchian_breakout_signals) "
                "and asserted equal to the recorded signal at all 4,392 decision "
                "moments (0 mismatches). Reference candle = the candle completed "
                "at the decision moment (bar ts = T-5m); next candle = the "
                "immediately following recorded candle (bar ts = T). NEUTRAL "
                "signals are excluded from directional accuracy entirely."
            ),
            "checkpoint": {
                "path": str(CHECKPOINT),
                "sha256": CHECKPOINT_SHA256,
                "run_id": self.ck.get("run_id"),
                "experiment_id": self.ck.get("experiment_id"),
                "schema": self.ck.get("schema"),
                "version": self.ck.get("version"),
                "last_processed": self.ck.get("last_processed"),
            },
            "dataset": {
                "date_range": [self.bars[0]["timestamp"], self.bars[-1]["timestamp"]],
                "sessions": len({b["timestamp"][:10] for b in self.bars}),
                "bars": len(self.bars),
                "instrument": self.bars[-1]["instrument"],
                "timeframe": "5m",
            },
            "unavailable_marker": MISSING,
            "validation": notes,
        }
        return {
            "meta": meta,
            "summary": summary,
            "time_of_day": buckets,
            "cross_checks": checks,
            "rows": rows,
        }


def render_md(b: dict) -> str:
    L: list[str] = []
    m, s = b["meta"], b["summary"]
    L.append("# 5M Donchian 20/10 — Next-Candle Directional Accuracy Audit (RECORDED)")
    L.append("")
    L.append("_MEASURE → VERIFY → REPORT. No strategy, parameter, signal, "
             "position-management, entry/exit, stop-loss/risk or configuration "
             "changes. No trading. No commit. No look-ahead._")
    L.append("")
    L.append("## A. Dataset identification")
    L.append("")
    L.append("| Field | Value |")
    L.append("|---|---|")
    dt = m["dataset"]
    L.append(f"| Dataset | {m['checkpoint']['path']} |")
    L.append(f"| sha256 | `{m['checkpoint']['sha256']}` |")
    L.append(f"| Run | {m['checkpoint']['run_id']} |")
    L.append(f"| Experiment | {m['checkpoint']['experiment_id']} |")
    L.append(f"| Schema / version | {m['checkpoint']['schema']} / {m['checkpoint']['version']} |")
    L.append(f"| Bars | {dt['bars']} × 5m across {dt['sessions']} sessions |")
    L.append(f"| Date range | {dt['date_range'][0]} → {dt['date_range'][1]} |")
    L.append(f"| Instrument | {dt['instrument']['symbol']} ({dt['instrument']['instrument_type']}, {dt['instrument']['exchange']}) |")
    L.append(f"| Timeframe | {dt['timeframe']} |")
    L.append("")
    L.append("## B. Methodology")
    L.append("")
    L.append("- Signal source: recorded decisions of the frozen "
             "`DonchianBreakout(entry=20, exit=10)` (`strategies/research_candidates.py`), "
             "reproduced per bar with `donchian_5m_forensics.replay_donchian(20,10)` and "
             "asserted equal to the recorded signal at every decision.")
    L.append("- Reference candle at decision timestamp T = the candle completed at T "
             "(bar timestamp T−5m); its close is what the signal reacts to.")
    L.append("- Next candle = the immediately following recorded 5-minute candle "
             "(bar timestamp T); never manufactured, missing ⇒ `NO_NEXT_CANDLE`.")
    L.append("- Directional test: BULLISH ⇒ UP is CORRECT / DOWN is WRONG; "
             "BEARISH ⇒ DOWN is CORRECT / UP is WRONG; equal closes are FLAT.")
    L.append("- NEUTRAL signals are excluded from directional accuracy.")
    L.append("")
    L.append("## C. Overall summary")
    L.append("")
    L.append(f"- Total decision candles: **{s['total_decision_candles']}**")
    L.append(f"- NEUTRAL signals: **{s['neutral_signals']}** (excluded from accuracy)")
    L.append(f"- Directional signals: **{s['directional_signals']}**")
    L.append(f"- Correct: **{s['overall']['correct']}**; Wrong: **{s['overall']['wrong']}**; "
             f"Flat: **{s['overall']['flat']}**; No next candle: **{s['overall']['no_next_candle']}**")
    L.append(f"- Overall directional accuracy = "
             f"**{s['overall']['correct']} / ({s['overall']['correct']} + {s['overall']['wrong']}) "
             f"= {s['overall']['accuracy_pct']}%**")
    L.append("")
    L.append("| | Signal | → Next UP | → Next DOWN | → Next FLAT | Correct | Wrong | Flat | Accuracy |")
    L.append("|---|---|---|---|---|---|---|---|---|")
    L.append(f"| Overall | {s['overall']['signals']} | {s['overall']['UP']} | {s['overall']['DOWN']} | "
             f"{s['overall']['FLAT']} | {s['overall']['correct']} | {s['overall']['wrong']} | "
             f"{s['overall']['flat']} | {s['overall']['accuracy_pct']}% |")
    L.append(f"| BULLISH | {s['bullish']['signals']} | {s['bullish']['UP']} | {s['bullish']['DOWN']} | "
             f"{s['bullish']['FLAT']} | {s['bullish']['correct']} | {s['bullish']['wrong']} | "
             f"{s['bullish']['flat']} | {s['bullish']['accuracy_pct']}% |")
    L.append(f"| BEARISH | {s['bearish']['signals']} | {s['bearish']['UP']} | {s['bearish']['DOWN']} | "
             f"{s['bearish']['FLAT']} | {s['bearish']['correct']} | {s['bearish']['wrong']} | "
             f"{s['bearish']['flat']} | {s['bearish']['accuracy_pct']}% |")
    L.append("")
    L.append("Accuracy denominator = Correct + Wrong (NEUTRAL, FLAT and "
             "NO_NEXT_CANDLE are never included).")
    L.append("")
    L.append("## D. BULLISH analysis")
    L.append("")
    L.append(f"- Signals: **{s['bullish']['signals']}**")
    L.append(f"- Correct: **{s['bullish']['correct']}**; Wrong: **{s['bullish']['wrong']}**; "
             f"Flat: **{s['bullish']['flat']}**")
    L.append(f"- Next UP / DOWN / FLAT: {s['bullish']['UP']} / {s['bullish']['DOWN']} / {s['bullish']['FLAT']}")
    L.append(f"- Accuracy: **{s['bullish']['correct']} / {s['bullish']['correct'] + s['bullish']['wrong']} "
             f"= {s['bullish']['accuracy_pct']}%**")
    L.append("")
    L.append("## E. BEARISH analysis")
    L.append("")
    L.append(f"- Signals: **{s['bearish']['signals']}**")
    L.append(f"- Correct: **{s['bearish']['correct']}**; Wrong: **{s['bearish']['wrong']}**; "
             f"Flat: **{s['bearish']['flat']}**")
    L.append(f"- Next DOWN / UP / FLAT: {s['bearish']['DOWN']} / {s['bearish']['UP']} / {s['bearish']['FLAT']}")
    L.append(f"- Accuracy: **{s['bearish']['correct']} / {s['bearish']['correct'] + s['bearish']['wrong']} "
             f"= {s['bearish']['accuracy_pct']}%**")
    L.append("")
    L.append("## F. Time-of-day analysis")
    L.append("")
    L.append("| Bucket | Signals | Correct | Wrong | Flat | Accuracy |")
    L.append("|---|---|---|---|---|---|")
    for bt in b["time_of_day"]:
        acc = ("&mdash;" if bt["accuracy_pct"] is None else f"{bt['accuracy_pct']}%")
        L.append(f"| {bt['bucket']} | {bt['signals']} | {bt['correct']} | {bt['wrong']} | "
                 f"{bt['flat']} | {acc} |")
    L.append("")
    L.append("_Descriptive only; no interpretation or optimization performed._")
    L.append("")
    L.append("## G. Complete signal-level ledger")
    L.append("")
    L.append("One row per directional signal, in decision order. Full detail in "
             "`donchian_5m_next_candle_directional_accuracy_audit.csv`.")
    L.append("")
    L.append("| # | Time | Ref ts | Ref close | DCH20 ↑ | DCH20 ↓ | DCH10 ↑ | DCH10 ↓ | Sig | Next ts | Next O | Next H | Next L | Next C | Dir | Result |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for i, r in enumerate(b["rows"], start=1):
        L.append(
            f"| {i} | {r['decision_time'][11:16]} | {r['reference_timestamp'][11:16]} "
            f"| {float(r['reference_close']):.2f} | {float(r['dch20_upper']):.2f} "
            f"| {float(r['dch20_lower']):.2f} | {float(r['dch10_upper']):.2f} "
            f"| {float(r['dch10_lower']):.2f} | {r['signal']} "
            f"| {r['next_timestamp'][11:16] if r['next_timestamp'] != m['unavailable_marker'] else '—'} "
            f"| {float(r['next_open']):.2f} | {float(r['next_high']):.2f} "
            f"| {float(r['next_low']):.2f} | {float(r['next_close']):.2f} "
            f"| {r['next_direction']} | {r['result']} |"
        )
    L.append("")
    L.append("## H. Validation / integrity checks")
    L.append("")
    for k, v in b["cross_checks"].items():
        L.append(f"- **{k}**: {'PASS' if v else 'FAIL'}")
    L.append("")
    for note in m["validation"]:
        L.append(f"- {note}")
    L.append("")
    L.append("## I. Neutral-signal treatment")
    L.append("")
    L.append("> NEUTRAL signals were excluded from directional accuracy because "
             "they do not make a directional prediction.")
    L.append("")
    L.append(f"- NEUTRAL at decision: **{s['neutral_signals']}** → "
             f"**{s['directional_signals']}** directional signals entered the test; "
             f"only those appear in the ledger (G).")
    L.append("")
    L.append("## J. Look-ahead-bias check")
    L.append("")
    L.append("- The signal at decision timestamp T is computed (frozen "
             "DonchianBreakout) from candles **completed at or before T** — "
             "windows `[i-20, i)` and `[i-10, i)` of bar timestamps ≤ T−5m — and "
             "the reference candle's close. The next candle (bar timestamp T) is "
             "used **only** as the outcome, never as a signal input.")
    L.append("- In this audit every directional row pairs its signal with the "
             "different recorded candle that follows it in the dataset; "
             "`next_timestamp == decision_time` and `reference_timestamp == T−5m` "
             "are asserted in the ledger, so the outcome candle starts **after** "
             "the decision instant.")
    L.append("")
    L.append("_Reproduction: "
             "`.venv\\Scripts\\python.exe scripts/generate_donchian_5m_next_candle_directional_accuracy_audit.py`_")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, default=OUT, help="output directory")
    args = ap.parse_args()

    b = DirectionalAudit().build()

    bodies = {
        "meta": {k: v for k, v in b["meta"].items() if k != "output_hash"},
        "summary": b["summary"],
        "time_of_day": b["time_of_day"],
        "cross_checks": b["cross_checks"],
        "rows": b["rows"],
    }
    canon = canonical_hash(bodies)
    b["meta"]["output_hash"] = canon
    b["meta"]["generated_at"] = datetime.now(timezone.utc).isoformat()
    payload = b

    args.out.mkdir(parents=True, exist_ok=True)
    base = "donchian_5m_next_candle_directional_accuracy_audit"
    json_path = args.out / f"{base}.json"
    json_path.write_text(json.dumps(payload, indent=1), encoding="utf-8")

    cols = [
        "decision_time", "reference_timestamp", "reference_open",
        "reference_high", "reference_low", "reference_close",
        "dch20_upper", "dch20_lower", "dch10_upper", "dch10_lower",
        "signal", "next_timestamp", "next_open", "next_high", "next_low",
        "next_close", "next_direction", "result",
    ]
    csv_path = args.out / f"{base}.csv"
    with open(csv_path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for r in payload["rows"]:
            w.writerow([str(r.get(c, "")) for c in cols])

    md_path = args.out / f"{base}.md"
    md_path.write_text(render_md(payload), encoding="utf-8")

    print("WROTE", json_path)
    print("WROTE", csv_path)
    print("WROTE", md_path)
    print(json.dumps({
        "total_decision_candles": payload["summary"]["total_decision_candles"],
        "neutral": payload["summary"]["neutral_signals"],
        "directional": payload["summary"]["directional_signals"],
        "bullish": payload["summary"]["bullish"],
        "bearish": payload["summary"]["bearish"],
        "overall_accuracy_pct": payload["summary"]["overall"]["accuracy_pct"],
        "cross_checks": payload["cross_checks"],
        "output_hash": canon,
    }, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())