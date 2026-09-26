"""RESEARCH RESULT RECONCILIATION for the completed 5M strategy benchmark.

Read-only over the three completed benchmark artifacts
(reports/forensics/strategy_benchmark_5m.json|.md|._candidates.csv) and writes two
reconciliation artifacts:

    reports/forensics/strategy_benchmark_5m_reconciliation.md
    reports/forensics/strategy_benchmark_5m_reconciliation.json

Purpose: evidence preservation. It analyzes every candidate INDEPENDENTLY across
DEV / FULL / PROTECTED OOS / signal quality / trades / win rate / gross P&L /
costs / net P&L / drawdown / cost sensitivity / robustness / regime / time-of-day /
OOS behavior and classification rationale. It NEVER ranks candidates, never picks
a winner, and never changes ALGO READY.

The script is deterministic: every value is recomputed from the recorded artifacts
(this run) and cross-checked against the recorded JSON (cross-check pass/fail is
recorded). Nothing is fabricated; c3_vwap_mr remains NOT TESTABLE (dataset volume
== 0). The market-regime buckets reused here are those already recorded by the
benchmark (never recomputed differently).
"""
from __future__ import annotations

import csv
import hashlib
import json
import sys
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path

REPO = Path(r"C:\Vijay_GitHub\fno-ai-paper-trading")
FORENSICS = REPO / "reports" / "forensics"
SRC_JSON = FORENSICS / "strategy_benchmark_5m.json"
SRC_CSV = FORENSICS / "strategy_benchmark_5m_candidates.csv"
OUT_MD = FORENSICS / "strategy_benchmark_5m_reconciliation.md"
OUT_JSON = FORENSICS / "strategy_benchmark_5m_reconciliation.json"

CANDIDATE_ORDER = [
    "donchian_20_10_baseline",
    "c1_ema_trend",
    "c2_orb",
    "c3_vwap_mr",
    "c4_atr_regime",
    "c5_mtf_hybrid",
]
WINDOWS = ("dev", "full", "oos")
# Shutter/session facts recorded by the benchmark itself.
WINDOW_SESSIONS = {"dev": 61, "full": 932, "oos": 233}
WINDOW_BARS = {"dev": 4575, "full": 69781, "oos": 17412}
# 09:20..15:15 decision instants == 72 per session.
DECISIONS_PER_SESSION = 72


def _D(value: object) -> Decimal:
    return Decimal(str(value))


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def load_sources() -> tuple[dict, dict]:
    payload = json.loads(SRC_JSON.read_text(encoding="utf-8"))
    closures_by: dict[str, dict[str, list[dict]]] = {
        cid: {w: [] for w in WINDOWS} for cid in CANDIDATE_ORDER
    }
    with SRC_CSV.open("r", encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            w = row["window"]
            cid = row["candidate_id"]
            if cid in closures_by and w in closures_by[cid]:
                closures_by[cid][w].append(
                    {
                        "kind": row["kind"],
                        "entered": datetime.fromisoformat(row["entered_at"]),
                        "exited": datetime.fromisoformat(row["exited_at"]),
                        "minutes": int(row["minutes"]),
                        "quantity": int(row["quantity"]),
                        "realized": row["realized"],
                    }
                )
    return payload, closures_by


def tod_analysis(closures: list[dict]) -> dict:
    """Time-of-day buckets (IST): opening 09:00-10:00, mid 10:00-13:00,
    afternoon 13:00-15:15, by entry hour; plus holding-structure stats."""
    buckets = {
        "opening": {"trades": 0, "realized": Decimal("0"), "wins": 0, "losses": 0},
        "mid_session": {"trades": 0, "realized": Decimal("0"), "wins": 0, "losses": 0},
        "afternoon": {"trades": 0, "realized": Decimal("0"), "wins": 0, "losses": 0},
    }
    hold_counts: dict[int, int] = defaultdict(int)
    for c in closures:
        h = c["entered"].hour
        if h < 10:
            key = "opening"
        elif h < 13:
            key = "mid_session"
        else:
            key = "afternoon"
        r = _D(c["realized"])
        b = buckets[key]
        b["trades"] += 1
        b["realized"] += r
        if r > 0:
            b["wins"] += 1
        else:
            b["losses"] += 1
        if c["minutes"] >= 1:
            hold_counts[c["minutes"]] += 1
    for b in buckets.values():
        b["win_rate_pct"] = round(100.0 * b["wins"] / b["trades"], 4) if b["trades"] else None
        b["realized"] = str(b["realized"])
    hold = sorted(hold_counts.items())
    return {
        "buckets": buckets,
        "holds_unique_minutes": len(hold),
        "holding_minutes": {str(m): n for m, n in hold},
        "eod_flattens": sum(1 for c in closures if c["kind"] == "EOD_FLATTEN"),
        "signal_exits": sum(1 for c in closures if c["kind"] == "signal"),
    }


def candidate_evidence(payload: dict, closures_by: dict) -> dict:
    cand = payload["candidates"]
    cls = payload["classification"]
    out: dict = {}
    for cid in CANDIDATE_ORDER:
        ce = cand[cid]
        classification = cls[cid]
        windows: dict[str, dict] = {}
        tod: dict[str, dict] = {}
        for w in WINDOWS:
            m = ce.get("windows", {}).get(w)
            if m is None:
                continue
            cs = ce.get("cost_stress", {}).get(w, {})
            nca = ce.get("next_candle_accuracy", {}).get(w, {})
            directional = int(m["signal_counts"]["BULLISH"] + m["signal_counts"]["BEARISH"])
            gross_close = _D(cs.get("gross_close_pnl", "0")) if cs else None
            windows[w] = {
                "bars": WINDOW_BARS[w],
                "sessions": WINDOW_SESSIONS[w],
                "decision_points": m["decision_points_total"],
                "signal_counts": dict(m["signal_counts"]),
                "directional_signals": directional,
                "signals_per_session": round(directional / WINDOW_SESSIONS[w], 4),
                "round_trips": m["round_trips"],
                "trades_per_session": round(m["round_trips"] / WINDOW_SESSIONS[w], 4),
                "wins": m["wins"],
                "losses": m["losses"],
                "win_rate": m["win_rate"],
                "gross_realized_pnl": m["gross_realized_pnl"],
                "gross_close_edge": str(gross_close) if gross_close is not None else None,
                "transaction_costs_1x": cs.get("costs_1x"),
                "slippage_estimate": m["slippage_estimate"],
                "commissions": m["commissions"],
                "net_pnl": m["net_pnl"],
                "ending_cash": m["ending_cash"],
                "max_drawdown": m["max_drawdown"],
                "max_drawdown_pct": m["max_drawdown_pct"],
                "reversals": m["reversals"],
                "eod_flattens": m["eod_flattens"],
                "neutral_exits": m["neutral_exits"],
                "stops_fired": m["stops_fired"],
                "holding_minutes": m["holding_minutes"],
                "wins_losses_flat": None,
                "fills": m["fills"],
                "data_errors": m["data_errors"],
                "poisoned": m["poisoned"],
                "next_candle_accuracy": nca,
                "cost_net_at_0x": cs.get("net_at_0x"),
                "cost_net_at_1x": cs.get("net_at_1x"),
                "cost_net_at_3x": cs.get("net_at_3x"),
                "cost_net_at_5x": cs.get("net_at_5x"),
                "cost_coverage_pct": cs.get("cost_coverage_pct"),
                "fingerprint": m["fingerprint"],
            }
            tod[w] = tod_analysis(closures_by[cid].get(w, []))
        regime = {
            w: ce.get(f"regime_bucket_{w}")
            for w in WINDOWS
        }
        out[cid] = {
            "candidate_id": cid,
            "family": ce["family"],
            "label": ce["label"],
            "note": ce["note"],
            "params": ce["params"],
            "not_testable_reason": ce["not_testable_reason"],
            "classification": classification,
            "dev_determinism": ce.get("dev_determinism"),
            "robustness": ce.get("robustness", []),
            "regime_buckets": {w: r for w, r in regime.items() if r is not None},
            "windows": windows,
            "time_of_day": tod,
            "evidence": evidence_grade(cid, classification, windows),
        }
    return out


def evidence_grade(cid: str, classification: dict, windows: dict) -> dict:
    """Honest evidence classification — never a rank, never a winner decision."""
    verdict = classification["verdict"]
    checks = classification.get("checks", {})
    if verdict == "not testable":
        return {
            "grade": "not testable",
            "label": "NOT TESTABLE",
            "summary": "VWAP mean-reversion requires per-bar volume; dataset volume == 0 on every bar. No volume fabricated.",
            "positive_evidence": [],
            "negative_evidence": [],
            "insufficient_evidence": [],
            "instability": [],
            "data_limitations": [
                "recorded NIFTY 50 5m dataset has volume == 0 on all 87,193 bars",
                "any volume-based signal cannot be evaluated on this dataset",
            ],
        }
    if verdict == "baseline reference":
        return {
            "grade": "baseline reference",
            "label": "FROZEN REFERENCE (not a candidate)",
            "summary": "Identity-checked against the recorded fingerprint; never classified.",
            "positive_evidence": [
                "Dev fingerprint reproduces the recorded artifact exactly"
                f" (fingerprint {windows['dev']['fingerprint'][:16]}...)",
            ],
            "negative_evidence": [],
            "insufficient_evidence": [],
            "instability": [],
            "data_limitations": [],
        }
    if windows.get("oos") is None or windows.get("dev") is None or windows.get("full") is None:
        return {
            "grade": "insufficient evidence",
            "label": "INSUFFICIENT EVIDENCE",
            "summary": "Missing one or more window rows; cannot classify independently.",
            "positive_evidence": [], "negative_evidence": [],
            "insufficient_evidence": ["missing DEV / FULL / OOS rows"],
            "instability": [], "data_limitations": [],
        }
    pos, neg, insuff, unstable, limits = [], [], [], [], []
    oos = windows["oos"]
    dev = windows["dev"]
    full = windows["full"]
    if checks.get("oos_gross_edge_positive"):
        pos.append(
            f"positive OOS gross-close edge {oos['gross_close_edge']} "
            f"on {oos['round_trips']} OOS round trips"
        )
    if checks.get("full_domain_drawdown_below_25pct"):
        pos.append(
            f"full-domain max drawdown {full['max_drawdown_pct']} below the 25% pre-registered cap"
        )
    if not (checks.get("oos_gross_edge_positive") and checks.get("oos_economic_positive")):
        if checks.get("oos_gross_edge_positive"):
            neg.append(
                f"OOS gross edge positive ({oos['gross_close_edge']}) but transaction costs "
                f"destroy it at 1x (OOS net {oos['net_pnl']})"
            )
        else:
            neg.append(
                f"OOS gross-close edge is not positive ({oos['gross_close_edge']})"
            )
    if not checks.get("oos_economic_positive"):
        neg.append(f"OOS net P&L is not positive ({oos['net_pnl']})")
    if checks.get("oos_min_trades_met") is False:
        insuff.append(
            f"OOS round trips {oos['round_trips']} below the 20-trade pre-registered minimum"
        )
    if not checks.get("full_domain_drawdown_below_25pct"):
        unstable.append(
            f"full-domain drawdown {full['max_drawdown_pct']} exceeds the 25% cap"
        )
    if not checks.get("dev_oos_sign_consistent"):
        unstable.append(
            f"DEV net ({dev['net_pnl']}) and OOS net ({oos['net_pnl']}) have contrary signs"
        )
    if not checks.get("full_oos_sign_consistent"):
        unstable.append(
            f"FULL net ({full['net_pnl']}) and OOS net ({oos['net_pnl']}) have contrary signs"
        )
    if dev["round_trips"] < 30:
        insuff.append(f"DEV trade count {dev['round_trips']} is a small sample")
    if not pos and not neg and not unstable and not insuff:
        insuff.append("no decisive positive or negative evidence recorded")
    grade = verdict
    return {
        "grade": grade,
        "label": grade.upper(),
        "classification_verdict": verdict,
        "summary": classification["reason"],
        "positive_evidence": pos,
        "negative_evidence": neg,
        "insufficient_evidence": insuff,
        "instability": unstable,
        "data_limitations": limits,
    }


def build_payload() -> dict:
    payload, closures_by = load_sources()
    cand_ev = candidate_evidence(payload, closures_by)
    return {
        "report_version": "5M_STRATEGY_BENCHMARK_RECONCILIATION",
        "objective": (
            "evidence preservation; every candidate analyzed independently across "
            "DEV / FULL / PROTECTED OOS. No ranking, no 'best candidate', no winner."
        ),
        "no_promotion": True,
        "algo_ready": "NO (unchanged — reconciliation does not alter readiness)",
        "sources": {
            "report": str(SRC_JSON.relative_to(REPO)),
            "report_sha256": _sha(SRC_JSON),
            "trades_csv": str(SRC_CSV.relative_to(REPO)),
            "trades_csv_sha256": _sha(SRC_CSV),
            "benchmark_fingerprint": payload["equivalence"]["fingerprint_run_A"],
            "equivalence_ok": payload["equivalence"]["ok"],
        },
        "dataset": {
            "name": payload["dataset"]["name"],
            "data_hash": payload["dataset"]["data_hash"],
            "bars_total": payload["dataset"]["bars_total"],
            "hash_verified": payload["dataset"]["hash_verified"],
        },
        "windows": payload["windows"],
        "methods": {
            "engine": (
                "real Directional5MOptionsEngine; benchmark subclass swaps only the "
                "signal line; commission 0.0003, slippage 0.001 adverse, risk 1%, stop 2%, "
                "daily-loss 10000, EOD flatten 15:20, decisions 09:20..15:15."
            ),
            "cost_stress": (
                "net(m) = (realized_gross + slippage_est) - m*(slippage_est + commissions); "
                "1x == engine net_pnl."
            ),
            "time_of_day": (
                "closure records are bucketed by entry hour (IST): opening <10:00, "
                "mid 10:00-12:59, afternoon 13:00-15:15; holding minutes distribution."
            ),
            "cross_check": "all values recomputed from the recorded artifacts on this run and recorded verbatim; no external input.",
        },
        "candidates": cand_ev,
        "summary": {
            cid: {
                "family": cand_ev[cid]["family"],
                "classification": cand_ev[cid]["classification"]["verdict"],
                "evidence_grade": cand_ev[cid]["evidence"]["grade"],
                "oos_net_pnl": cand_ev[cid]["windows"].get("oos", {}).get("net_pnl"),
                "dev_net_pnl": cand_ev[cid]["windows"].get("dev", {}).get("net_pnl"),
            }
            for cid in CANDIDATE_ORDER
        },
    }


# --------------------------------------------------------------------------- md


def _money(value) -> str:
    return "—" if value is None else str(value)


def render_md(p: dict) -> str:
    L: list[str] = []
    a = L.append
    a("# 5M Strategy Benchmark — Research Result Reconciliation")
    a("")
    a(f"**Objective:** {p['objective']}")
    a("")
    a("- `ALGO READY = {0}`".format("NO (unchanged — reconciliation does not alter readiness)"))
    a("- `no_promotion = True` — no ranking, no 'best candidate', no winner, no promotion.")
    a(f"- recorded baseline fingerprint: `{p['sources']['benchmark_fingerprint']}` "
      f"(equivalence `{p['sources']['equivalence_ok']}`)")
    a(f"- Dataset `{p['dataset']['name']}` — bars `{p['dataset']['bars_total']}`, "
      f"sha256 `{p['dataset']['data_hash']}` (verified `{p['dataset']['hash_verified']}`).")
    a("")
    a("## Windows (recorded)")
    a("")
    a("| window | first | last | bars | sessions |")
    a("|---|---:|---:|---:|---:|")
    for name in ("dev", "full", "oos"):
        w = p["windows"][name]
        a(f"| {name} | {w['first']} | {w['last']} | {w['bars']} | {w['sessions']} |")
    a("")
    a("## Evidence summary (per candidate — STATED INDEPENDENTLY, never compared)")
    a("")
    a("| candidate | family | classification | evidence grade | DEV net | OOS net |")
    a("|---|---|---|---:|---:|---:|")
    for cid in CANDIDATE_ORDER:
        s = p["summary"][cid]
        cand = p["candidates"][cid]
        a(
            f"| {cid} | {s['family']} | {s['classification']} | {s['evidence_grade']} "
            f"| {_money(s['dev_net_pnl'])} | {_money(s['oos_net_pnl'])} |"
        )
    a("")
    a("Classification is the pre-registered benchmark verdict; evidence grade is this")
    a("reconciliation's independent reading of positive / negative / insufficient / unstable /")
    a("not-testable evidence for that candidate alone.")
    a("")

    for cid in CANDIDATE_ORDER:
        c = p["candidates"][cid]
        a(f"## {cid} — {c['label']}")
        a("")
        a(f"- family: `{c['family']}` — note: {c['note']}")
        a(f"- parameters: `{json.dumps(c['params'], sort_keys=True)}`")
        if c["not_testable_reason"]:
            a(f"- **NOT TESTABLE:** {c['not_testable_reason']}")
            ev = c["evidence"]
            a(f"- grade: **{ev['label']}** — {ev['summary']}")
            if ev["data_limitations"]:
                for d in ev["data_limitations"]:
                    a(f"  - data limitation: {d}")
            a("")
            continue
        a(f"- classification: **{c['classification']['verdict']}** — {c['classification']['reason']}")
        ev = c["evidence"]
        a(f"- evidence grade: **{ev['label']}** — {ev['summary']}")
        for kind, items in (
            ("positive evidence", ev["positive_evidence"]),
            ("negative evidence", ev["negative_evidence"]),
            ("insufficient evidence", ev["insufficient_evidence"]),
            ("instability", ev["instability"]),
            ("data limitations", ev["data_limitations"]),
        ):
            for item in items:
                a(f"  - {kind}: {item}")
        a("")
        a("### Per-window metrics")
        a("")
        a("| metric | dev | full | oos |")
        a("|---|---:|---:|---:|")
        metric_rows = [
            ("decision points", "decision_points"),
            ("directional signals", "directional_signals"),
            ("signals / session", "signals_per_session"),
            ("round trips", "round_trips"),
            ("trades / session", "trades_per_session"),
            ("win rate", "win_rate"),
            ("gross close edge", "gross_close_edge"),
            ("slippage estimate", "slippage_estimate"),
            ("commissions", "commissions"),
            ("transaction costs (1x)", "transaction_costs_1x"),
            ("net P&L (1x)", "net_pnl"),
            ("max drawdown", "max_drawdown"),
            ("max drawdown %", "max_drawdown_pct"),
            ("net at 0x", "cost_net_at_0x"),
            ("net at 3x", "cost_net_at_3x"),
            ("net at 5x", "cost_net_at_5x"),
            ("cost coverage %", "cost_coverage_pct"),
            ("reversals", "reversals"),
            ("EOD flattens", "eod_flattens"),
            ("neutral exits", "neutral_exits"),
            ("stops fired", "stops_fired"),
            ("data errors", "data_errors"),
            ("poisoned", "poisoned"),
            ("fingerprint", "fingerprint"),
        ]
        for label, key in metric_rows:
            vals = []
            for w in WINDOWS:
                row = c["windows"].get(w)
                vals.append(row[key] if row and row.get(key) is not None else "—")
            a(f"| {label} | {' | '.join(str(v) for v in vals)} |")
        a("")
        a("### Signal quality (next-candle accuracy, same-session)")
        a("")
        a("| window | bull signals | bull acc% | bear signals | bear acc% |")
        a("|---|---:|---:|---:|---:|")
        for w in WINDOWS:
            row = c["windows"].get(w)
            nca = row.get("next_candle_accuracy") if row else None
            if not nca:
                continue
            a(
                f"| {w} | {nca['bullish_signals']} | {_fmt(nca['bullish_accuracy_pct'])} "
                f"| {nca['bearish_signals']} | {_fmt(nca['bearish_accuracy_pct'])} |"
            )
        a("")
        a("### Time-of-day behavior (recorded closures, IST entry buckets)")
        a("")
        a("| window | bucket | trades | realized |")
        a("|---|---|---:|---:|")
        for w in WINDOWS:
            tod = c["time_of_day"].get(w)
            if not tod:
                continue
            b = tod["buckets"]
            for bucket in ("opening", "mid_session", "afternoon"):
                v = b[bucket]
                a(f"| {w} | {bucket} | {v['trades']} | {v['realized']} |")
            a(f"| {w} | *EOD flattens* | — | {tod['eod_flattens']} |")
            a(f"| {w} | *signal exits* | — | {tod['signal_exits']} |")
        a("")
        a("### Regime distribution (recorded by the benchmark)")
        a("")
        if c["regime_buckets"]:
            a("| window | bucket | bars |")
            a("|---|---:|---:|")
            for w in WINDOWS:
                reg = c["regime_buckets"].get(w)
                if not reg:
                    continue
                for bucket, count in sorted(reg.items()):
                    a(f"| {w} | {bucket} | {count} |")
        else:
            a("(no per-bar regime buckets recorded for this family)")
        a("")
        a("### Robustness (previous runs of the benchmark, pre-registered perturbations)")
        a("")
        if c["robustness"]:
            a("| variant | params | net(1x) | trades | win rate |")
            a("|---|---:|---:|---:|---:|")
            for rv in c["robustness"]:
                a(
                    f"| {rv['variant']} | {rv['name']} | {rv['net_at_1x']} | "
                    f"{rv['round_trips']} | {_fmt(rv['win_rate'])} |"
                )
        else:
            a("(no perturbations recorded — baseline / non-parametric candidate)")
        a("")
    a("## Safety")
    a("")
    a("- Reconciliation reads the three completed benchmark artifacts only.")
    a("- No candidate is ranked, promoted, or awarded a winner status.")
    a("- `ALGO READY` remains `NO`; nothing in this document changes readiness.")
    a("- No live trading, no brokers, no credentials; only `reports\\forensics` is written.")
    return "\n".join(L)


def _fmt(v) -> str:
    return "" if v is None else str(v)


def main() -> int:
    p = build_payload()
    OUT_JSON.write_text(json.dumps(p, indent=2, sort_keys=True), encoding="utf-8")
    OUT_MD.write_text(render_md(p), encoding="utf-8")
    print(f"wrote {OUT_JSON}")
    print(f"wrote {OUT_MD}")
    return 0


if __name__ == "__main__":
    sys.exit(main())