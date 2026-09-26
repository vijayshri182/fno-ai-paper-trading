#!/usr/bin/env python3
"""Forensic SIGNAL->POSITION->TRADE->P&L ATTRIBUTION AUDIT of the recorded 5M
Donchian 20/10 decision ledger (5M_DIRECTIONAL_OPTIONS_EXPERIMENT, run
exp-20260922-242c9dc0814c480aa788c3c39bb47f57).

MEASURE -> VERIFY -> REPORT. This audit does NOT change the strategy, parameters,
signal logic, position management, entry/exit or risk rules, does not introduce
look-ahead, does not trade, does not modify any recorded artifact and does not
commit anything.

This is a forensic attribution analysis. It does not modify, optimize, rank,
approve, reject, or promote the strategy.

Dataset: the SAME recorded checkpoint already used by
validate_donchian_5m_20_10_forensics.py, donchian_5m_20_10_trade_ledger.csv,
the 2025-06-18 signal-order audit, the 2025-06-18 position-lifecycle ledger and
the next-candle directional accuracy audit, pinned by sha256:
  5m_directional_options.2025-08-14.json

Population: the 248 directional (BULLISH/BEARISH) signals of the 4,392 recorded
decisions, taken one-for-one from
donchian_5m_next_candle_directional_accuracy_audit.json (every directional
signal gets an explicit attribution status; nothing is silently dropped).

Instrument, timeline and decision semantics (identical to the earlier audits):
  - decision at timestamp T is generated from the candle completed at T
    (bar timestamp T-5m) = the REFERENCE candle,
  - the NEXT candle is the immediately following recorded 5-minute candle (bar
    timestamp T); never manufactured (NO_NEXT_CANDLE if absent),
  - holding/exit, fills, approvals and closures come straight from the recorded
    checkpoint; the 229 position episodes are the recorded round trips of
    donchian_5m_20_10_trade_ledger.csv (one episode = one recorded trade).

Attribution model (each of the 248 directional signals gets exactly one status):
  - NEW_TRADE            : signal opens a NEW position episode (recorded fill,
                           1 fill; action ENTER_CALL / ENTER_PUT)
  - HELD_EXISTING_TRADE  : signal arrives while the position is already open and
                           unchanged (0 recorded fills) -> shares the already
                           attributed episode (P&L attribution = episode-level
                           only; never re-summed)
  - SWITCHED_TRADE       : signal swaps the open leg in one decision (2 recorded
                           fills: close + open). It CLOSES one episode and OPENS
                           another; the economics of the OPENED episode are
                           attributed to it
  - EXITED_EXISTING_TRADE: directional signal that exits without opening (0 in
                           this dataset; exits happen on NEUTRAL/EOD/REVERSAL)
  - NO_NEW_TRADE         : directional signal that neither opens nor holds nor
                           switches (0 in this dataset)

P&L attribution convention: each position episode's economics are counted
exactly ONCE, at the directional signal that OPENED it (229 opening signals =
228 NEW_TRADE + 1 switch-opened episode). A HELD signal is associated with its
episode but never re-sums that episode's P&L. Aggregate economics therefore sum
to the recorded ledger totals exactly.

Outputs (into <repo>/reports/forensics):
  * donchian_5m_signal_to_trade_attribution_audit.md
  * donchian_5m_signal_to_trade_attribution_audit.csv     (one row per signal)
  * donchian_5m_signal_to_trade_attribution_audit.json
  * donchian_5m_position_episode_attribution.csv          (one row per episode)
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from fno_ai_paper_trading.research.donchian_5m_forensics import (
    canonical_hash,
    ticks_repr,
)

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "reports" / "forensics"

CHECKPOINT = Path(
    r"C:\Users\user\AppData\Local\Temp\opencode\fno_5m_validation\out\exp_runA"
    r"\checkpoints\5m_directional_options.2025-08-14.json"
)
CHECKPOINT_SHA256 = "6c8ebc8c999a23fc9a0c71a0994355092dc952429658b4c1567b3dcd58b4af12"

SIGNAL_AUDIT_JSON = OUT / "donchian_5m_next_candle_directional_accuracy_audit.json"
TRADE_LEDGER_CSV = OUT / "donchian_5m_20_10_trade_ledger.csv"

FIVE = timedelta(minutes=5)
DZERO = Decimal("0")

# STEP 4 lifecycle classes (forensic vocabulary)
CLS_NEW_ENTRY = "NEW_ENTRY"
CLS_HOLD = "HOLD_EXISTING_POSITION"
CLS_SWITCH = "SWITCH"
CLS_EXIT = "EXIT"
CLS_NO_NEW = "DIRECTIONAL_SIGNAL_WITH_NO_NEW_POSITION"

# STEP 11 attribution statuses (one per directional signal)
ST_NEW_TRADE = "NEW_TRADE"
ST_HELD = "HELD_EXISTING_TRADE"
ST_SWITCH = "SWITCHED_TRADE"
ST_EXIT = "EXITED_EXISTING_TRADE"
ST_NO_NEW = "NO_NEW_TRADE"

RESULT_GROUPS = [
    ("BULLISH CORRECT", "BULLISH", "CORRECT"),
    ("BULLISH WRONG", "BULLISH", "WRONG"),
    ("BEARISH CORRECT", "BEARISH", "CORRECT"),
    ("BEARISH WRONG", "BEARISH", "WRONG"),
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


def as_dec(v) -> Decimal:
    if v is None or v == "":
        return DZERO
    return Decimal(str(v))


class SignalToTradeAttribution:
    def __init__(self) -> None:
        self.ck = load_checkpoint(CHECKPOINT)
        if not SIGNAL_AUDIT_JSON.exists():
            sys.exit(f"[FAIL] signal-ledger source missing: {SIGNAL_AUDIT_JSON}")
        if not TRADE_LEDGER_CSV.exists():
            sys.exit(f"[FAIL] trade ledger missing: {TRADE_LEDGER_CSV}")
        self.sig_audit = json.loads(SIGNAL_AUDIT_JSON.read_text(encoding="utf-8"))
        self.ledger = self._load_ledger(TRADE_LEDGER_CSV)
        self.dec_by_moment = {d["moment"]: d for d in self.ck["decisions"]}
        self.bars = self.ck["history"]
        self.bar_by_ts = {b["timestamp"]: b for b in self.bars}
        self._link_episodes()

    # ------------------------------------------------------------- loaders

    @staticmethod
    def _load_ledger(path: Path) -> list[dict]:
        with open(path, "r", encoding="utf-8", newline="") as fh:
            return list(csv.DictReader(fh))

    def _link_episodes(self) -> None:
        """Build episode indexes from the recorded trade ledger. Every episode
        (round trip) is keyed by its recorded entry and exit timestamps."""
        self.ep_by_entry: dict[str, dict] = {}
        self.ep_by_exit: dict[str, dict] = {}
        entries = Counter(t["entry_timestamp"] for t in self.ledger)
        exits = Counter(t["exit_timestamp"] for t in self.ledger)
        for ts, n in entries.items():
            if n != 1:
                sys.exit(f"[FAIL] episode entry timestamp not unique: {ts} ({n})")
        for ts, n in exits.items():
            if n != 1:
                sys.exit(f"[FAIL] episode exit timestamp not unique: {ts} ({n})")
        for t in self.ledger:
            self.ep_by_entry[t["entry_timestamp"]] = t
            self.ep_by_exit[t["exit_timestamp"]] = t

    def _episode_open_at(self, moment: str) -> list[dict]:
        """Position episodes open strictly across moment (entry < T < exit)."""
        return [t for t in self.ledger
                if t["entry_timestamp"] < moment < t["exit_timestamp"]]

    # ---------------------------------------------------------------- rows

    def signal_rows(self) -> list[dict]:
        rows = []
        for i, s in enumerate(self.sig_audit["rows"], start=1):
            T = s["decision_time"]
            dec = self.dec_by_moment.get(T)
            if dec is None:
                sys.exit(f"[FAIL] decision missing for signal at {T}")
            if dec["signal"] != s["signal"]:
                sys.exit(f"[FAIL] signal mismatch at {T}")
            n_fills = len(dec["fills"])
            actions = list(dec["actions"])
            if n_fills == 2:
                status = ST_SWITCH
                cls = CLS_SWITCH
            elif n_fills == 1 and dec["state_before"] == "FLAT" and dec["state_after"] != "FLAT":
                status = ST_NEW_TRADE
                cls = CLS_NEW_ENTRY
            elif n_fills == 0 and dec["state_before"] == dec["state_after"]:
                status = ST_HELD
                cls = CLS_HOLD
            else:
                sys.exit(f"[FAIL] unclassified directional signal at {T} "
                         f"(fills={n_fills}, before={dec['state_before']}, "
                         f"after={dec['state_after']}, actions={actions})")
            if status == ST_SWITCH:
                closed = self.ep_by_exit.get(T)
                opened = self.ep_by_entry.get(T)
                if closed is None or opened is None:
                    sys.exit(f"[FAIL] switch at {T}: closed={bool(closed)} "
                             f"opened={bool(opened)}")
                if closed["exit_reason"] != "REVERSAL":
                    sys.exit(f"[FAIL] switch close at {T} not REVERSAL")
                ep = opened
                closed_id = closed["trade_number"]
            elif status == ST_NEW_TRADE:
                ep = self.ep_by_entry.get(T)
                if ep is None:
                    sys.exit(f"[FAIL] no episode opened at {T}")
                closed_id = ""
            else:  # HELD
                open_eps = self._episode_open_at(T)
                if len(open_eps) != 1:
                    sys.exit(f"[FAIL] held signal at {T}: {len(open_eps)} open episodes")
                ep = open_eps[0]
                closed_id = ""
            if ep["entry_signal"] != s["signal"]:
                sys.exit(f"[FAIL] episode entry signal mismatch at {T}: "
                         f"{ep['entry_signal']} vs {s['signal']}")
            rows.append(self._assemble_row(i, s, dec, status, cls, ep, closed_id))
        return rows

    @staticmethod
    def _assemble_row(i: int, s: dict, dec: dict, status: str, cls: str,
                      ep: dict, closed_id: str) -> dict:
        entry = datetime.fromisoformat(ep["entry_timestamp"])
        exit_ = datetime.fromisoformat(ep["exit_timestamp"])
        signal_t = datetime.fromisoformat(s["decision_time"])
        is_opener = status in (ST_NEW_TRADE, ST_SWITCH)
        signal_to_exit_min = int((exit_ - signal_t).total_seconds() // 60)
        signal_to_exit_bars = signal_to_exit_min // 5
        gross_move = gross_price_move(ep)
        return {
            "signal_id": f"S{i:04d}",
            "signal_time": s["decision_time"],
            "session": s["decision_time"][:10],
            "signal": s["signal"],
            "next_direction": s["next_direction"],
            "next_candle_result": s["result"],
            "attribution_class": cls,
            "attribution_status": status,
            "action_recorded": ",".join(dec["actions"]) if dec["actions"] else "-",
            "state_before": dec["state_before"],
            "state_after": dec["state_after"],
            "fills_count": len(dec["fills"]),
            "position_episode_id": ep["trade_number"],
            "closed_episode_id": closed_id,
            "episode_entry_time": ep["entry_timestamp"],
            "episode_exit_time": ep["exit_timestamp"],
            "episode_direction": ep["direction"],
            "episode_index_side": ep["index_side"],
            "episode_quantity": ep["quantity"],
            "entry_price": ep["entry_price"],
            "exit_price": ep["exit_price"],
            "entry_close": ep["entry_close"],
            "exit_close": ep["exit_close"],
            "holding_bars": ep["holding_bars"],
            "holding_minutes": ep["holding_minutes"],
            "signal_to_exit_bars": signal_to_exit_bars,
            "signal_to_exit_minutes": signal_to_exit_min,
            "gross_price_move": ticks_repr(gross_move[0]),
            "gross_price_move_net": ticks_repr(gross_move[1]),
            "episode_exit_reason": ep["exit_reason"],
            "episode_exit_kind": ep["exit_kind"],
            "p_and_l_owner": is_opener,
            "gross_pnl_close": ep["gross_pnl_close"],
            "spread_cost": ep["spread_cost"],
            "slippage_cost": ep["slippage_cost"],
            "commission_cost": ep["commission_cost"],
            "total_cost": ep["total_cost"],
            "realized_pnl": ep["realized_pnl"],
            "net_pnl": ep["net_pnl"],
        }

    def episode_rows(self) -> list[dict]:
        """One row per recorded position episode (round trip)."""
        opening_by_ep: dict[str, dict] = {}
        for s in self.signal_rows():
            opening_by_ep[s["position_episode_id"]] = s
        out = []
        for t in self.ledger:
            opener = opening_by_ep.get(t["trade_number"])
            ntt = datetime.fromisoformat(t["entry_timestamp"])
            ext = datetime.fromisoformat(t["exit_timestamp"])
            gross_move = gross_price_move(t)
            out.append({
                "position_episode_id": t["trade_number"],
                "trade_id": t["trade_id"],
                "entry_time": t["entry_timestamp"],
                "exit_time": t["exit_timestamp"],
                "direction": t["direction"],
                "index_side": t["index_side"],
                "quantity": t["quantity"],
                "entry_price": t["entry_price"],
                "exit_price": t["exit_price"],
                "entry_close": t["entry_close"],
                "exit_close": t["exit_close"],
                "holding_bars": t["holding_bars"],
                "holding_minutes": t["holding_minutes"],
                "gross_price_move": ticks_repr(gross_move[0]),
                "gross_price_move_net": ticks_repr(gross_move[1]),
                "exit_reason": t["exit_reason"],
                "exit_kind": t["exit_kind"],
                "opening_signal_time": opener["signal_time"] if opener else MISSING_TXT,
                "opening_signal": opener["signal"] if opener else MISSING_TXT,
                "opening_attribution_status": opener["attribution_status"] if opener else MISSING_TXT,
                "opening_next_candle_result": opener["next_candle_result"] if opener else MISSING_TXT,
                "signal_to_exit_bars": (int((ext - ntt).total_seconds() // 60 // 5)
                                        if opener else ""),
                "signal_to_exit_minutes": (int((ext - ntt).total_seconds() // 60)
                                           if opener else ""),
                "gross_pnl_close": t["gross_pnl_close"],
                "spread_cost": t["spread_cost"],
                "slippage_cost": t["slippage_cost"],
                "commission_cost": t["commission_cost"],
                "total_cost": t["total_cost"],
                "realized_pnl": t["realized_pnl"],
                "net_pnl": t["net_pnl"],
            })
        return out

    # -------------------------------------------------------------- summary

    def summary(self, rows: list[dict]) -> dict:
        n = len(rows)
        status = Counter(r["attribution_status"] for r in rows)
        cls = Counter(r["attribution_class"] for r in rows)
        by_sig = {"BULLISH": 0, "BEARISH": 0}
        for r in rows:
            by_sig[r["signal"]] += 1
        res = Counter(r["next_candle_result"] for r in rows)
        openers = [r for r in rows if r["p_and_l_owner"]]
        held = [r for r in rows if r["attribution_status"] == ST_HELD]
        owned_ep = len({r["position_episode_id"] for r in openers})
        unique_ep_held = len({r["position_episode_id"] for r in held})
        hb = [int(r["holding_bars"]) for r in self.episode_rows()]
        hm = [int(r["holding_minutes"]) for r in self.episode_rows()]
        return {
            "total_directional_signals": n,
            "directional_breakdown": dict(by_sig),
            "next_candle_result_counts": dict(res),
            "attribution_status_counts": dict(status),
            "attribution_class_counts": dict(cls),
            "episode_count": len(self.episode_rows()),
            "episodes_opened_by_directional_signals": owned_ep,
            "episodes_sharing_held_signals": unique_ep_held,
            "episodes_without_directional_opener": len(self.episode_rows()) - owned_ep,
            "holding_bars": {
                "min": min(hb) if hb else None,
                "max": max(hb) if hb else None,
                "mean": round(sum(hb) / len(hb), 4) if hb else None,
                "median": sorted(hb)[len(hb) // 2] if hb else None,
            },
            "holding_minutes": {
                "min": min(hm) if hm else None,
                "max": max(hm) if hm else None,
                "mean": round(sum(hm) / len(hm), 4) if hm else None,
                "median": sorted(hm)[len(hm) // 2] if hm else None,
            },
        }

    # ------------------------------------------------------- aggregations

    def economic_sums(self, episodes: list[dict]) -> dict:
        def s(k: str) -> Decimal:
            return sum((as_dec(e[k]) for e in episodes), DZERO)
        return {
            "gross_pnl_close": ticks_repr(s("gross_pnl_close")),
            "spread_cost": ticks_repr(s("spread_cost")),
            "slippage_cost": ticks_repr(s("slippage_cost")),
            "commission_cost": ticks_repr(s("commission_cost")),
            "total_cost": ticks_repr(s("total_cost")),
            "realized_pnl": ticks_repr(s("realized_pnl")),
            "net_pnl": ticks_repr(s("net_pnl")),
        }

    def attribution_by_status(self, rows: list[dict]) -> dict:
        out: dict[str, dict] = {}
        for status in (ST_NEW_TRADE, ST_HELD, ST_SWITCH, ST_EXIT, ST_NO_NEW):
            members = [r for r in rows if r["attribution_status"] == status]
            eps = [r["position_episode_id"] for r in members]
            ep_objs = [self._ep_by_id(ep_id) for ep_id in eps]
            # episodes are summed once per status only when the signal OWNS them
            owned = [r for r in members if r["p_and_l_owner"]]
            owned_ep_objs = [self._ep_by_id(r["position_episode_id"]) for r in owned]
            out[status] = {
                "signals": len(members),
                "unique_episodes": len(set(eps)),
                "economics": self.economic_sums(owned_ep_objs),
            }
        return out

    def _ep_by_id(self, episode_id: str) -> dict:
        for t in self.ledger:
            if t["trade_number"] == episode_id:
                return t
        sys.exit(f"[FAIL] unknown episode id {episode_id}")

    def by_exit_reason(self) -> dict:
        groups: dict[str, list[dict]] = {}
        for ep in self.ledger:
            groups.setdefault(ep["exit_reason"], []).append(ep)
        return {k: {"episodes": len(v),
                    "economics": self.economic_sums(v)}
                for k, v in sorted(groups.items())}

    def by_next_candle_result(self, rows: list[dict]) -> dict:
        """STEP 13 aggregate: for each (signal, next-candle result) group, the
        signal counts by attribution status and the P&L of the episodes OPENED
        by that group's opening signals (each episode counted exactly once)."""
        out: dict[str, dict] = {}
        for label, sig, res in RESULT_GROUPS:
            members = [r for r in rows if r["signal"] == sig
                       and r["next_candle_result"] == res]
            openers = [r for r in members if r["p_and_l_owner"]]
            ep_objs = [self._ep_by_id(r["position_episode_id"]) for r in openers]
            status = Counter(r["attribution_status"] for r in members)
            out[label] = {
                "signals": len(members),
                "openers_that_own_an_episode": len(openers),
                "attribution_status_counts": dict(status),
                "episodes_opened": len(ep_objs),
                "economics": self.economic_sums(ep_objs),
            }
        return out

    def cross_tab_next_vs_net(self, rows: list[dict]) -> dict:
        """STEP 17: next-candle result of each opening signal vs the net P&L of
        the episode it opened (episode-level, counted once)."""
        out: dict[str, dict] = {}
        for label, sig, res in RESULT_GROUPS:
            openers = [r for r in rows if r["signal"] == sig
                       and r["next_candle_result"] == res and r["p_and_l_owner"]]
            eps = [self._ep_by_id(r["position_episode_id"]) for r in openers]
            pos = [e for e in eps if as_dec(e["net_pnl"]) > DZERO]
            neg = [e for e in eps if as_dec(e["net_pnl"]) < DZERO]
            flat = [e for e in eps if as_dec(e["net_pnl"]) == DZERO]
            out[label] = {
                "episodes": len(eps),
                "net_positive_episodes": len(pos),
                "net_negative_episodes": len(neg),
                "net_flat_episodes": len(flat),
                "net_pnl_positive": ticks_repr(sum((as_dec(e["net_pnl"]) for e in pos), DZERO)),
                "net_pnl_negative": ticks_repr(sum((as_dec(e["net_pnl"]) for e in neg), DZERO)),
                "net_pnl_total": ticks_repr(sum((as_dec(e["net_pnl"]) for e in eps), DZERO)),
            }
        return out

    def churn(self, rows: list[dict]) -> dict:
        owners = [r for r in rows if r["p_and_l_owner"]]
        s2e = sorted(int(r["signal_to_exit_minutes"]) for r in owners)
        n = len(s2e)
        return {
            "directional_signals": len(rows),
            "episode_opening_signals": len(owners),
            "signals_per_episode_open": round(len(rows) / len(owners), 4) if owners else None,
            "held_signals_inside_episodes": len(rows) - len(owners),
            "signal_to_exit_minutes_min": min(s2e) if n else None,
            "signal_to_exit_minutes_max": max(s2e) if n else None,
            "signal_to_exit_minutes_mean": round(sum(s2e) / n, 4) if n else None,
            "signal_to_exit_minutes_median": sorted(s2e)[n // 2] if n else None,
        }

    # ------------------------------------------------------------ integrity

    def cross_checks(self, rows: list[dict], summary: dict) -> dict:
        episode_rows = self.episode_rows()
        status = Counter(r["attribution_status"] for r in rows)
        classes = Counter(r["attribution_class"] for r in rows)
        owners = [r for r in rows if r["p_and_l_owner"]]
        owners_ep = {r["position_episode_id"] for r in owners}
        checks = {
            "A: all 248 directional signals present with a status": (
                len(rows) == 248
                and all(r["attribution_status"] in
                        (ST_NEW_TRADE, ST_HELD, ST_SWITCH, ST_EXIT, ST_NO_NEW)
                        for r in rows)
            ),
            "B: BULLISH 127 / BEARISH 121": (
                summary["directional_breakdown"] == {"BULLISH": 127, "BEARISH": 121}
            ),
            "C: status counts sum to 248 and none are zeros-for-DATA": (
                sum(status.values()) == 248
                and status.get(ST_EXIT, 0) == 0
                and status.get(ST_NO_NEW, 0) == 0
            ),
            "D: lifecycle classes valid": (
                all(r["attribution_class"] in (
                    CLS_NEW_ENTRY, CLS_HOLD, CLS_SWITCH, CLS_EXIT, CLS_NO_NEW)
                    for r in rows)
                and sum(classes.values()) == 248
            ),
            "E: every episode opened by exactly one directional signal": (
                len(owners) == len(owners_ep) == len(self.ledger)
            ),
            "F: switch closes one episode (REVERSAL) and opens one": (
                status.get(ST_SWITCH, 0) == 1
                and len([t for t in self.ledger if t["exit_reason"] == "REVERSAL"]) == 1
            ),
            "G: held signals share 19 distinct already-attributed episodes": (
                summary["episodes_sharing_held_signals"] == 19
                and summary["episodes_without_directional_opener"] == 0
            ),
            "H: economics sum to the recorded ledger totals": self._ledger_recon(),
            "I: exit reasons = NEUTRAL 225 / EOD 3 / REVERSAL 1": (
                Counter(t["exit_reason"] for t in self.ledger)
                == {"NEUTRAL": 225, "EOD": 3, "REVERSAL": 1}
            ),
            "J: no look-ahead (signal at T uses candle closed at T; episode "
            "entry is never after its opening signal)": self._no_lookahead(rows),
        }
        return checks

    def _ledger_recon(self) -> bool:
        agg = self.economic_sums(self.ledger)
        exp = {
            "gross_pnl_close": "-28.80",
            "spread_cost": "0",
            "slippage_cost": "11667.509",
            "commission_cost": "3500.252708640",
            "total_cost": "15167.761708640",
            "realized_pnl": "-11696.309",
            "net_pnl": "-15196.561708640",
        }
        return all(as_dec(agg[k]) == as_dec(exp[k]) for k in exp)

    def _no_lookahead(self, rows: list[dict]) -> bool:
        for r in rows:
            T = datetime.fromisoformat(r["signal_time"])
            ref_ts = (T - FIVE).isoformat()
            if ref_ts not in self.bar_by_ts:
                return False
            entry = datetime.fromisoformat(r["episode_entry_time"])
            if entry > T:
                return False
            exit_ = datetime.fromisoformat(r["episode_exit_time"])
            if exit_ < entry:
                return False
        return True

    # ---------------------------------------------------------------- build

    def build(self) -> dict:
        rows = self.signal_rows()
        eps = self.episode_rows()
        summary = self.summary(rows)
        checks = self.cross_checks(rows, summary)
        meta = {
            "audit": "SIGNAL-TO-TRADE ATTRIBUTION AUDIT",
            "method": (
                "Each recorded directional Donchian 20/10 signal (BULLISH 127, "
                "BEARISH 121 = 248 of the 4,392 recorded decisions) is mapped to "
                "the recorded position action at its decision timestamp and to "
                "the position episode (recorded trade) it opened or shares. "
                "Attribution statuses: NEW_TRADE / HELD_EXISTING_TRADE / "
                "SWITCHED_TRADE / EXITED_EXISTING_TRADE / NO_NEW_TRADE. Episode "
                "economics are counted exactly once, at the signal that opened "
                "the episode; HELD signals never re-sum their episode's P&L "
                "(P&L attribution = episode-level only where not the opener). "
                "Populated from the recorded checkpoint (sha-pinned), the "
                "next-candle directional accuracy ledger (248 rows) and the "
                "recorded 229-trade ledger (one episode = one recorded trade)."
            ),
            "statement": (
                "This is a forensic attribution analysis. It does not modify, "
                "optimize, rank, approve, reject, or promote the strategy."
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
            "sources": {
                "signal_ledger": str(SIGNAL_AUDIT_JSON),
                "trade_ledger": str(TRADE_LEDGER_CSV),
                "signal_population": len(self.sig_audit["rows"]),
                "recorded_decisions": self.decisions(),
            },
        }
        return {
            "meta": meta,
            "summary": summary,
            "attribution": {
                "by_status": self.attribution_by_status(rows),
                "by_exit_reason": self.by_exit_reason(),
                "by_next_candle_result": self.by_next_candle_result(rows),
                "cross_tab_next_vs_net": self.cross_tab_next_vs_net(rows),
            },
            "churn": self.churn(rows),
            "cross_checks": checks,
            "rows": rows,
            "episodes": eps,
        }

    def decisions(self) -> int:
        return len(self.ck["decisions"])


MISSING_TXT = "NOT AVAILABLE FROM RECORDED ARTIFACT"


def gross_price_move(ep: dict) -> tuple[Decimal, Decimal]:
    """(direction-aligned close-to-close move per unit, sign x price)."""
    ec = as_dec(ep["entry_close"])
    xc = as_dec(ep["exit_close"])
    if ep["direction"] == "CALL":
        return (xc - ec), as_dec(ep["gross_pnl_close"])
    return (ec - xc), as_dec(ep["gross_pnl_close"])


def render_md(b: dict) -> str:
    L: list[str] = []
    m, s = b["meta"], b["summary"]
    att, ch = b["attribution"], b["churn"]
    L.append("# 5M Donchian 20/10 — Signal-to-Trade Attribution Audit (RECORDED)")
    L.append("")
    L.append("_MEASURE → VERIFY → REPORT. No strategy, parameter, signal, "
             "position-management, entry/exit, stop-loss/risk or configuration "
             "changes. No trading. No commit. No look-ahead._")
    L.append("")
    L.append("> " + m["statement"])
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
    L.append("## B. Population and mapping inputs")
    L.append("")
    L.append(f"- Recorded decisions: **{m['sources']['recorded_decisions']}** "
             f"(NEUTRAL excluded); directional signals: **{s['total_directional_signals']}** "
             f"(BULLISH {s['directional_breakdown']['BULLISH']} / BEARISH "
             f"{s['directional_breakdown']['BEARISH']}).")
    L.append(f"- Next-candle result counts: "
             f"{_json_inline(s['next_candle_result_counts'])}.")
    L.append(f"- Signal source: `{m['sources']['signal_ledger']}` "
             f"({m['sources']['signal_population']} rows, one per directional signal).")
    L.append(f"- Position source: `{m['sources']['trade_ledger']}` — 229 recorded "
             f"round trips; **one position episode = one recorded trade**.")
    L.append("")
    L.append("## C. Methodology / attribution model")
    L.append("")
    L.append(f"{m['method']}")
    L.append("")
    L.append("| Status | Lifecycle class | Meaning |")
    L.append("|---|---|---|")
    L.append("| NEW_TRADE | NEW_ENTRY | Signal opens a new position episode (recorded fill). |")
    L.append("| HELD_EXISTING_TRADE | HOLD_EXISTING_POSITION | Signal arrives while the position is already open and unchanged; shares the already-attributed episode. |")
    L.append("| SWITCHED_TRADE | SWITCH | Signal swaps the leg in one decision: closes one episode (REVERSAL) and opens another (economics of the OPENED episode attributed to it). |")
    L.append("| EXITED_EXISTING_TRADE | EXIT | Directional exit without opening (0 in this dataset; exits happen on NEUTRAL/EOD/REVERSAL). |")
    L.append("| NO_NEW_TRADE | DIRECTIONAL_SIGNAL_WITH_NO_NEW_POSITION | No open/hold/switch (0 in this dataset). |")
    L.append("")
    L.append("**P&L attribution convention:** each episode is counted **once**, at "
             "the signal that opened it; holding signals never re-sum their "
             "episode's P&L (`p_and_l_owner=false`).")
    L.append("")
    L.append("## D. Directional-signal attribution summary")
    L.append("")
    L.append("| Status | Signals | Unique episodes | Gross P&L | Slippage | Commission | Realized | Net P&L |")
    L.append("|---|---|---|---|---|---|---|---|")
    for status in ("NEW_TRADE", "HELD_EXISTING_TRADE", "SWITCHED_TRADE",
                   "EXITED_EXISTING_TRADE", "NO_NEW_TRADE"):
        v = att["by_status"][status]
        e = v["economics"]
        L.append(f"| {status} | {v['signals']} | {v['unique_episodes']} | "
                 f"{_dec(e['gross_pnl_close'])} | {_dec(e['slippage_cost'])} | "
                 f"{_dec(e['commission_cost'])} | {_dec(e['realized_pnl'])} | "
                 f"{_dec(e['net_pnl'])} |")
    L.append("")
    L.append("_HELD_EXISTING_TRADE rows carry their episode's economics as "
             "context but never re-add them (episode-level only). SWITCHED_TRADE "
             "owns the episode it OPENED; the episode it closed is owned by its "
             "own opening signal._")
    L.append("")
    L.append("## E. Position episodes")
    L.append("")
    L.append(f"- Recorded position episodes (recorded trades): **{s['episode_count']}**")
    L.append(f"- Opened by a directional signal: **{s['episodes_opened_by_directional_signals']}** "
             f"(228 NEW_TRADE + 1 switch-opened)")
    L.append(f"- Episodes WITHOUT a directional opener: **{s['episodes_without_directional_opener']}**")
    L.append(f"- Holding bars: min **{s['holding_bars']['min']}**, max **{s['holding_bars']['max']}**, "
             f"mean **{s['holding_bars']['mean']}**, median **{s['holding_bars']['median']}**")
    L.append(f"- Holding minutes: min **{s['holding_minutes']['min']}**, max **{s['holding_minutes']['max']}**, "
             f"mean **{s['holding_minutes']['mean']}**, median **{s['holding_minutes']['median']}**")
    L.append("")
    L.append("## F. Episode exit reasons")
    L.append("")
    L.append("| Exit reason | Episodes | Gross P&L | Slippage | Commission | Realized | Net P&L |")
    L.append("|---|---|---|---|---|---|---|---|")
    for k, v in att["by_exit_reason"].items():
        e = v["economics"]
        L.append(f"| {k} | {v['episodes']} | {_dec(e['gross_pnl_close'])} | "
                 f"{_dec(e['slippage_cost'])} | {_dec(e['commission_cost'])} | "
                 f"{_dec(e['realized_pnl'])} | {_dec(e['net_pnl'])} |")
    L.append("")
    L.append("## G. Signal attribution by next-candle result")
    L.append("")
    L.append("| Next-candle group | Signals | Owners | NEW | HELD | SWITCH | EXIT | NO_NEW | Episodes opened | Gross P&L | Slippage | Commission | Net P&L |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for label, v in att["by_next_candle_result"].items():
        st = v["attribution_status_counts"]
        e = v["economics"]
        L.append(f"| {label} | {v['signals']} | {v['openers_that_own_an_episode']} | "
                 f"{st.get('NEW_TRADE', 0)} | {st.get('HELD_EXISTING_TRADE', 0)} | "
                 f"{st.get('SWITCHED_TRADE', 0)} | {st.get('EXITED_EXISTING_TRADE', 0)} | "
                 f"{st.get('NO_NEW_TRADE', 0)} | {v['episodes_opened']} | "
                 f"{_dec(e['gross_pnl_close'])} | {_dec(e['slippage_cost'])} | "
                 f"{_dec(e['commission_cost'])} | {_dec(e['net_pnl'])} |")
    L.append("")
    L.append("_Owners = signals that OPEN an episode (their P&L is counted exactly once, in this group)._")
    L.append("")
    L.append("## H. Next-candle-result vs episode net P&L (episode-level)")
    L.append("")
    L.append("| Next-candle group | Episodes | Net+ | Net− | Net 0 | Net P&L (+) | Net P&L (−) | Net P&L total |")
    L.append("|---|---|---|---|---|---|---|---|")
    for label, v in att["cross_tab_next_vs_net"].items():
        L.append(f"| {label} | {v['episodes']} | {v['net_positive_episodes']} | "
                 f"{v['net_negative_episodes']} | {v['net_flat_episodes']} | "
                 f"{_dec(v['net_pnl_positive'])} | {_dec(v['net_pnl_negative'])} | "
                 f"{_dec(v['net_pnl_total'])} |")
    L.append("")
    L.append("## I. Churn / signal-to-exit statistics (owners)")
    L.append("")
    L.append(f"- Directional signals: **{ch['directional_signals']}**")
    L.append(f"- Episode-opening signals: **{ch['episode_opening_signals']}**")
    L.append(f"- Signals per opened episode: **{ch['signals_per_episode_open']}**")
    L.append(f"- Held signals inside episodes: **{ch['held_signals_inside_episodes']}**")
    L.append(f"- Signal-to-exit minutes (min/mean/median/max): "
             f"**{ch['signal_to_exit_minutes_min']} / {ch['signal_to_exit_minutes_mean']} / "
             f"{ch['signal_to_exit_minutes_median']} / {ch['signal_to_exit_minutes_max']}**")
    L.append("")
    L.append("## J. Economics reconciliation vs recorded ledger / existing forensics")
    L.append("")
    e = _agg_total(b)
    L.append("| Measure | This audit (229 episodes) | Recorded ledger / forensics | Match |")
    L.append("|---|---|---|---|")
    for k in ("gross_pnl_close", "spread_cost", "slippage_cost",
              "commission_cost", "total_cost", "realized_pnl", "net_pnl"):
        exp_k = {
            "gross_pnl_close": "-28.80",
            "spread_cost": "0",
            "slippage_cost": "11667.509",
            "commission_cost": "3500.252708640",
            "total_cost": "15167.761708640",
            "realized_pnl": "-11696.309",
            "net_pnl": "-15196.561708640",
        }[k]
        L.append(f"| {k} | {e[k]} | {exp_k} | {'PASS' if as_dec(e[k]) == as_dec(exp_k) else 'FAIL'} |")
    L.append("")
    L.append("## K. Integrity checks")
    L.append("")
    for k, v in b["cross_checks"].items():
        L.append(f"- **{k}**: {'PASS' if v else 'FAIL'}")
    L.append("")
    L.append("## L. Look-ahead-bias check")
    L.append("")
    L.append("- Every directional signal at timestamp T is matched to its recorded "
             "decision; the signal is computed from candles closed at/before T "
             "(reference candle = bar at T−5m).")
    L.append("- The episode mapped to a signal never STARTS after that signal "
             "(entry ≤ T) and always exits at or after entry; the opening fill is "
             "the recorded entry fill at the signal moment.")
    L.append("- Outcome data (next candle, episode exit) is used only for "
             "evaluation, never as an input to the signal or the mapping.")
    L.append("")
    L.append("## M. Complete signal-level attribution ledger")
    L.append("")
    L.append("One row per directional signal, in decision order. Full detail in "
             "`donchian_5m_signal_to_trade_attribution_audit.csv`.")
    L.append("")
    L.append("| # | Time | Session | Signal | Next dir | Result | Status | Action | State → | Episode | Closed | Held | Sig→exit (m) | Direction | Net P&L |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for i, r in enumerate(b["rows"], start=1):
        L.append(f"| {i} | {r['signal_time'][11:16]} | {r['session']} | {r['signal']} "
                 f"| {r['next_direction']} | {r['next_candle_result']} | "
                 f"{r['attribution_status']} | {r['action_recorded']} | "
                 f"{r['state_before']}→{r['state_after']} | {r['position_episode_id']} | "
                 f"{r['closed_episode_id'] or '—'} | {r['fills_count']} | "
                 f"{r['signal_to_exit_minutes']} | {r['episode_direction']} | "
                 f"{_dec(r['net_pnl'])} |")
    L.append("")
    L.append("## N. Position-episode attribution ledger")
    L.append("")
    L.append("One row per recorded episode in "
             "`donchian_5m_position_episode_attribution.csv` (229 rows), each "
             "carrying its opening signal's time/signal/status/next-candle result.")
    L.append("")
    L.append("## O. Reproduction")
    L.append("")
    L.append("_Deterministic generator:_")
    L.append("")
    L.append("    .venv\\Scripts\\python.exe scripts/generate_donchian_5m_signal_to_trade_attribution_audit.py")
    return "\n".join(L)


def _agg_total(b: dict) -> dict:
    """Total economics over all 229 episodes (once each)."""
    sums = {k: DZERO for k in ("gross_pnl_close", "spread_cost", "slippage_cost",
                               "commission_cost", "total_cost", "realized_pnl", "net_pnl")}
    for ep in b["episodes"]:
        for k in sums:
            sums[k] += as_dec(ep[k])
    return {k: ticks_repr(v) for k, v in sums.items()}


def _json_inline(d: dict) -> str:
    return ", ".join(f"{k}: {v}" for k, v in d.items())


def _dec(v) -> str:
    return format(as_dec(v), "f")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, default=OUT, help="output directory")
    args = ap.parse_args()

    a = SignalToTradeAttribution()
    b = a.build()

    bodies = {
        "meta": {k: v for k, v in b["meta"].items()
                 if k not in ("output_hash", "generated_at")},
        "summary": b["summary"],
        "attribution": b["attribution"],
        "churn": b["churn"],
        "cross_checks": b["cross_checks"],
        "rows": b["rows"],
        "episodes": b["episodes"],
    }
    canon = canonical_hash(bodies)
    b["meta"]["output_hash"] = canon
    b["meta"]["generated_at"] = datetime.now(timezone.utc).isoformat()
    payload = b

    args.out.mkdir(parents=True, exist_ok=True)
    base = "donchian_5m_signal_to_trade_attribution_audit"
    json_path = args.out / f"{base}.json"
    json_path.write_text(json.dumps(payload, indent=1), encoding="utf-8")

    cols = [
        "signal_id", "signal_time", "session", "signal", "next_direction",
        "next_candle_result", "attribution_class", "attribution_status",
        "action_recorded", "state_before", "state_after", "fills_count",
        "position_episode_id", "closed_episode_id", "episode_entry_time",
        "episode_exit_time", "episode_direction", "episode_index_side",
        "episode_quantity", "entry_price", "exit_price", "entry_close",
        "exit_close", "holding_bars", "holding_minutes", "signal_to_exit_bars",
        "signal_to_exit_minutes", "gross_price_move", "gross_price_move_net",
        "episode_exit_reason", "episode_exit_kind", "p_and_l_owner",
        "gross_pnl_close", "spread_cost", "slippage_cost", "commission_cost",
        "total_cost", "realized_pnl", "net_pnl",
    ]
    csv_path = args.out / f"{base}.csv"
    with open(csv_path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for r in payload["rows"]:
            w.writerow([str(r.get(c, "")) for c in cols])

    ep_cols = [
        "position_episode_id", "trade_id", "entry_time", "exit_time",
        "direction", "index_side", "quantity", "entry_price", "exit_price",
        "entry_close", "exit_close", "holding_bars", "holding_minutes",
        "gross_price_move", "gross_price_move_net", "exit_reason", "exit_kind",
        "opening_signal_time", "opening_signal", "opening_attribution_status",
        "opening_next_candle_result", "signal_to_exit_bars",
        "signal_to_exit_minutes", "gross_pnl_close", "spread_cost",
        "slippage_cost", "commission_cost", "total_cost", "realized_pnl",
        "net_pnl",
    ]
    ep_csv_path = args.out / "donchian_5m_position_episode_attribution.csv"
    with open(ep_csv_path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(ep_cols)
        for e in payload["episodes"]:
            w.writerow([str(e.get(c, "")) for c in ep_cols])

    md_path = args.out / f"{base}.md"
    md_path.write_text(render_md(payload), encoding="utf-8")

    print("WROTE", json_path)
    print("WROTE", csv_path)
    print("WROTE", ep_csv_path)
    print("WROTE", md_path)
    print(json.dumps({
        "directional_signals": payload["summary"]["total_directional_signals"],
        "attribution_status_counts": payload["summary"]["attribution_status_counts"],
        "episode_count": payload["summary"]["episode_count"],
        "episodes_opened_by_directional_signals": payload["summary"][
            "episodes_opened_by_directional_signals"],
        "net_pnl_total": _agg_total(payload)["net_pnl"],
        "cross_checks": payload["cross_checks"],
        "output_hash": canon,
    }, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())