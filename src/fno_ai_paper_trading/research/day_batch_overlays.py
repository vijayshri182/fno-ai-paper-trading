"""Iteration-003 exit-quality overlay and its state-machine proof.

Architecture note
-----------------
The overlay is a *signal-layer transform* plus an engine-faithful *shadow
replica* of the state the existing ``BacktestEngine`` derives from the signals.
A pure signal transform cannot, on its own, know the engine's position after a
protective stop or a risk rejection fires, so it would keep believing a position
is open and could later emit a stale exit that *reverses* structure under
quantity 1. That violates the Iteration-003 contract ("never reverse directly",
"never invent an entry the original did not make").

The shadow replica therefore tracks, per bar: signed position, slippage-adjusted
entry price, the LONG-only protective stop (anchored below the entry fill, entry
candle excluded), and the per-date realized P&L used by the risk manager's daily
loss limit. The overlay makes its ALLOW / DEFER / HOLD decisions on this replica
state exactly as a live overlay would on its own position. The single produced
signal series is then executed by the unchanged engine, and
:func:`verify_execution_sync` requires the engine's per-bar journal (positions,
fills, stops, risk rejections) to agree with the replica bar-for-bar. If the
replica ever disagreed, the experiment would be declared invalid - the real
engine is the ground truth.

The overlay never creates an entry the original did not make, never reverses a
position directly, never blocks an original entry decision, and never touches
the engine, the protective stop, or the risk manager. Differences from the
baseline are limited to exit bars being deferred (or allowed later), carried
entry holds (the overlay already holds the identical side), plus the documented
downstream consequences of holding a position longer.
"""
from __future__ import annotations

from decimal import Decimal

from fno_ai_paper_trading.models.enums import Signal
from fno_ai_paper_trading.strategies.base import SignalResult
from fno_ai_paper_trading.strategies.composite import (
    MultiIndicatorStrategy,
    bulk_signals,
    latched_signals,
)

_EXIT_ADX = Decimal("28")
_SLIPPAGE = Decimal("0.001")
_STOP_PCT = Decimal("0.02")
_MAX_DAILY_LOSS = Decimal("10000")
_MULTIPLIER = Decimal("1")


def _feat_decimal(meta: dict | None, key: str) -> Decimal | None:
    if not meta:
        return None
    raw = meta.get(key)
    if raw is None:
        return None
    try:
        return Decimal(str(raw))
    except (ValueError, TypeError, ArithmeticError):
        return None


def _allow_exit(adx: Decimal | None, exit_adx: Decimal) -> bool:
    """Exit-quality gate: ALLOW when exit-bar ADX is unavailable or below threshold."""
    if adx is None:
        return True
    return adx < exit_adx


def _orig_trajectory(raw: list[SignalResult]) -> tuple[list[int], list[int]]:
    """Original (baseline) position trajectory implied by the latched series."""
    n = len(raw)
    before = [0] * n
    after = [0] * n
    op = 0
    for i, r in enumerate(raw):
        b = op
        s = r.signal
        if s is Signal.BUY:
            op = 1 if op == 0 else 0
        elif s is Signal.SELL:
            op = -1 if op == 0 else 0
        before[i] = b
        after[i] = op
    return before, after


class _Shadow:
    """Engine-faithful replica of the engine's position state machine.

    Mirrors, in the research layer, what the unchanged ``BacktestEngine`` builds
    from the signal series: slippage-adjusted fills, the LONG-only protective
    stop anchored to the slippage-adjusted entry price (entry candle excluded),
    and the risk daily-loss gate on realized P&L. The real engine validates every
    bar afterwards (see :func:`verify_execution_sync`).
    """

    def __init__(
        self,
        slippage: Decimal = _SLIPPAGE,
        stop_pct: Decimal = _STOP_PCT,
        max_daily_loss: Decimal = _MAX_DAILY_LOSS,
    ) -> None:
        self.slippage = slippage
        self.stop_pct = stop_pct
        self.max_daily_loss = max_daily_loss
        self.pos = 0  # +1 LONG, -1 SHORT, 0 FLAT
        self.entry: Decimal | None = None
        self.opened_idx = -1
        self.day_realized: dict[str, Decimal] = {}

    def _fill_price(self, close: Decimal, side: str) -> Decimal:
        factor = Decimal("1") + self.slippage if side == "BUY" else Decimal("1") - self.slippage
        return close * factor

    def apply_fill(self, *, bar, side: str, idx: int) -> dict:
        """Execute one engine-style fill (flat opens, opposite side closes)."""
        fill_price = self._fill_price(bar.close, side)
        realized: Decimal | None = None
        if self.pos == 0:
            self.pos = 1 if side == "BUY" else -1
            self.entry = fill_price
            self.opened_idx = idx
            action = "entry"
        else:
            if side == "SELL" and self.pos == 1:
                realized = (fill_price - self.entry) * _MULTIPLIER
            elif side == "BUY" and self.pos == -1:
                realized = (self.entry - fill_price) * _MULTIPLIER
            else:
                # Same-direction signal while positioned: the engine would stack
                # (qty 1 -> 2). The overlay never emits such orders (entries the
                # overlay already holds are carried to HOLD), so this is a bug.
                raise AssertionError(
                    f"unexpected same-direction fill: pos={self.pos} side={side}"
                )
            key = bar.timestamp.date().isoformat()
            self.day_realized[key] = self.day_realized.get(key, Decimal("0")) + realized
            self.pos = 0
            self.entry = None
            self.opened_idx = -1
            action = "exit"
        return {"action": action, "fill_price": fill_price, "realized": realized, "idx": idx}

    def apply_stop(self, *, bar, idx: int) -> dict | None:
        """Replicate the LONG-only protective stop (entry candle excluded)."""
        if self.pos != 1 or self.entry is None:
            return None
        if idx <= self.opened_idx:
            return None  # the entry candle itself can never trigger the stop
        stop = self.entry * (Decimal("1") - self.stop_pct)
        if bar.open <= stop:
            price = bar.open * (Decimal("1") - self.slippage)
        elif bar.low <= stop:
            price = stop * (Decimal("1") - self.slippage)
        else:
            return None
        realized = (price - self.entry) * _MULTIPLIER
        key = bar.timestamp.date().isoformat()
        self.day_realized[key] = self.day_realized.get(key, Decimal("0")) + realized
        self.pos = 0
        self.entry = None
        self.opened_idx = -1
        return {"action": "stop", "fill_price": price, "realized": realized, "idx": idx}

    def risk_blocked(self, bar) -> bool:
        key = bar.timestamp.date().isoformat()
        return self.day_realized.get(key, Decimal("0")) <= -self.max_daily_loss


def confirm_exits(
    bars,
    strategy: MultiIndicatorStrategy,
    exit_adx: Decimal = _EXIT_ADX,
    *,
    slippage: Decimal = _SLIPPAGE,
    stop_pct: Decimal = _STOP_PCT,
    max_daily_loss: Decimal = _MAX_DAILY_LOSS,
    shadow: _Shadow | None = None,
) -> tuple[list[SignalResult], list[dict]]:
    """Apply the I-003 exit-quality overlay; returns ``(series, shadow_rows)``.

    ``series`` has exactly one :class:`SignalResult` per bar. Every original
    entry decision (bar, side, price) is preserved unless the overlay already
    holds the identical side (a disclosed carry, emitted as HOLD so the engine
    never stacks). Original exit bars are ALLOWED or DEFERRED by the
    pre-registered exit-bar-ADX<28 gate. The protective stop and the risk
    daily-loss gate are untouched (the engine still enforces them) and mirrored
    by the shadow replica so the overlay's decisions never disagree with the
    engine's actual position.
    """
    raw = bulk_signals(bars, strategy, latch=True)
    if len(raw) != len(bars):
        raise ValueError("signal series length must equal bars length")
    orig_before, orig_after = _orig_trajectory(raw)
    shadow_replica = shadow if shadow is not None else _Shadow(
        slippage=slippage, stop_pct=stop_pct, max_daily_loss=max_daily_loss
    )

    series: list[SignalResult] = []
    shadow_rows: list[dict] = []
    for i, r in enumerate(raw):
        s = r.signal
        bar = bars[i]
        adx = _feat_decimal(r.meta, "adx")
        tb, target = orig_before[i], orig_after[i]
        pos = shadow_replica.pos
        pos_before = pos

        action = "hold"
        reason = ""
        emit: Signal | None = None

        if pos != 0 and s in (Signal.BUY, Signal.SELL):
            # Positioned in the identical side the original would enter: carry.
            if (pos == 1 and s is Signal.BUY) or (pos == -1 and s is Signal.SELL):
                action, reason = "hold", (
                    "original entry carried: already "
                    + ("LONG" if pos == 1 else "SHORT")
                )
            elif target == pos:
                action = "hold"
                reason = "aligned; position already matches original (no fill needed)"
            elif tb == 0 and target == 0:
                emit = s
                action = "exit"
                reason = "original flips to flat -> exit"
            elif _allow_exit(adx, exit_adx):
                emit = Signal.BUY if pos == -1 else Signal.SELL
                action = "exit"
                reason = (
                    "exit allowed (exit-bar ADX "
                    + (
                        "unavailable; no trap"
                        if adx is None
                        else f"{adx:.2f} < {exit_adx}"
                    )
                )
            else:
                action = "deferexit"
                reason = (
                    f"exit DEFERRED; exit-bar ADX {adx:.2f} >= {exit_adx} "
                    "(strong-trend momentum held)"
                )
        elif tb == 0 and target != 0:
            emit = s
            action = "entry"
            reason = "original entry preserved (unchanged timestamp/side)"
        elif target == pos:
            if s is Signal.HOLD:
                action, reason = "hold", "aligned (position matches original)"
            else:
                action, reason = "hold", "original transition lands on flat"
        else:
            action, reason = "hold", "flat; no invented entry/reversal"

        fill = None
        risk_blocked = False
        if emit is not None:
            if shadow_replica.risk_blocked(bar):
                risk_blocked = True
                action = "risk_blocked"
                reason = reason + " ; order blocked by daily loss limit (replicated)"
            else:
                fill = shadow_replica.apply_fill(bar=bar, side=emit.value, idx=i)

        stop_fill = None
        if shadow_replica.pos == 1 and i > shadow_replica.opened_idx:
            stop_fill = shadow_replica.apply_stop(bar=bar, idx=i)

        series.append(
            SignalResult(
                signal=emit if emit is not None else Signal.HOLD,
                instrument=r.instrument,
                timestamp=r.timestamp,
                reason=reason or r.reason,
                meta=dict(r.meta or {}),
            )
        )

        shadow_rows.append(
            {
                "ts": bar.timestamp.isoformat() if bar.timestamp is not None else "",
                "raw_signal": s.value,
                "signal": emit.value if emit is not None else "HOLD",
                "bias": "LONG" if target == 1 else ("SHORT" if target == -1 else ""),
                "adx": str(adx) if adx is not None else "",
                "orig_before": tb,
                "orig_after": target,
                "pos_before": pos_before,
                "pos_after": shadow_replica.pos,
                "action": action,
                "reason": reason,
                "emit": emit.value if emit is not None else "",
                "risk_blocked": risk_blocked,
                "stop_fill": bool(stop_fill),
                "entry_price": (
                    str(shadow_replica.entry) if shadow_replica.entry is not None else ""
                ),
                "realized": (
                    str(fill["realized"])
                    if fill and fill.get("realized") is not None
                    else ""
                ),
            }
        )
    return series, shadow_rows
def _entry_fills(journal) -> list[dict]:
    return [
        {"ts": e["timestamp"], "side": e.get("order_side")}
        for e in journal
        if e.get("entry_trade_id") and e.get("fill_price") is not None
    ]


def verify_execution_sync(var_journal: list[dict], shadow_rows: list[dict]) -> dict:
    """Compare the shadow replica with the real engine journal bar-by-bar."""
    shadow_by_ts = {row["ts"]: row for row in shadow_rows}
    mismatches = []
    fills_shadow = 0
    stops_shadow = 0
    risk_shadow = 0
    for row in shadow_rows:
        if row["emit"] and not row["risk_blocked"]:
            fills_shadow += 1
        if row["stop_fill"]:
            stops_shadow += 1
        if row["risk_blocked"]:
            risk_shadow += 1

    fills_engine = sum(1 for e in var_journal if e.get("fill_price") is not None)
    stops_engine = sum(1 for e in var_journal if e.get("stop_fill"))
    risk_engine = sum(
        1 for e in var_journal if e.get("order_side") and e.get("risk_approved") is False
    )

    for e in var_journal:
        ts = e["timestamp"]
        row = shadow_by_ts.get(ts)
        if row is None:
            mismatches.append({"ts": ts, "detail": "no shadow row"})
            continue
        pa = e.get("position_after") or {}
        expected = row["pos_after"]
        qty = pa.get("quantity", 0) or 0
        side = pa.get("side", "FLAT")
        got = qty if side == "LONG" else (-qty if side == "SHORT" else 0)
        if got != expected:
            mismatches.append(
                {
                    "ts": ts,
                    "detail": "position_after mismatch",
                    "shadow": expected,
                    "engine": got,
                }
            )
        if bool(e.get("stop_fill")) != row["stop_fill"]:
            mismatches.append(
                {"ts": ts, "detail": "stop_fill mismatch", "shadow": row["stop_fill"], "engine": bool(e.get("stop_fill"))}
            )

    return {
        "fills_shadow": fills_shadow,
        "fills_engine": fills_engine,
        "stops_shadow": stops_shadow,
        "stops_engine": stops_engine,
        "risk_shadow": risk_shadow,
        "risk_engine": risk_engine,
        "mismatches": mismatches,
        "ok": (
            not mismatches
            and fills_shadow == fills_engine
            and stops_shadow == stops_engine
            and risk_shadow == risk_engine
        ),
    }


def run_exit_quality_variant(
    bars,
    strategy: MultiIndicatorStrategy,
    config,
    engine,
    exit_adx: Decimal = _EXIT_ADX,
) -> dict:
    """Run the variant: overlay -> unchanged engine -> sync + phase-F proof."""
    slippage = getattr(config, "slippage_rate", None)
    exec_conf = getattr(config, "execution", None)
    if exec_conf is not None:
        slippage = getattr(exec_conf, "total_adverse_rate", slippage)
    if slippage is None:
        slippage = _SLIPPAGE
    series, shadow_rows = confirm_exits(
        bars,
        strategy,
        exit_adx,
        slippage=slippage,
        stop_pct=getattr(config, "stop_loss_pct", _STOP_PCT),
        max_daily_loss=getattr(config, "max_daily_loss", _MAX_DAILY_LOSS),
    )
    journal = []
    result = engine.run(bars, strategy, config, signals=series, journal=journal)
    sync = verify_execution_sync(journal, shadow_rows)
    return {
        "series": series,
        "shadow": shadow_rows,
        "journal": journal,
        "result": result,
        "sync": sync,
    }


def _closing(journal) -> list[dict]:
    """Closing fills from a journal, including protective-stop exits.

    The engine journals signal fills under ``fill_price`` and stop fills under
    ``stop_price`` (the actual stop fill price); both are closing fills of a
    round trip and count as exits/entries for the machine proof. Side defaults
    to the signal side, or SELL for the LONG-only protective stop.
    """
    rows = []
    for e in journal:
        is_stop = bool(e.get("stop_fill"))
        price = e.get("fill_price")
        if price is None:
            price = e.get("stop_price")
        if not (e.get("exit_trade_id") and price is not None):
            continue
        side = e.get("order_side")
        if side is None:
            side = "SELL" if is_stop else ""
        rows.append(
            {
                "ts": e["timestamp"],
                "side": side,
                "realized": e.get("realized_pnl", ""),
                "stop": is_stop,
            }
        )
    return rows


def verify_state_machine(
    bars,
    strategy: MultiIndicatorStrategy,
    base_signals: list[SignalResult],
    var_signals: list[SignalResult],
    base_journal: list[dict],
    var_journal: list[dict],
    var_shadow: list[dict],
) -> dict:
    """Machine-checkable state-machine preservation proof (Phase F).

    Divergence classes: raw (0 required), latch (0 outside intended exits),
    entry (added must be 0; omitted must all be carry-explained), exit
    (deferred / allowed-later are the intended change), stop, risk, fills and
    bar-level position state (downstream consequences, reported).
    ``outside_intended_exits`` lists any divergence outside exit handling;
    a non-empty list invalidates the experiment.
    """
    raw = bulk_signals(bars, strategy, latch=False)
    latch = latched_signals(raw, initial=None)
    latch_vals = [r.signal.value for r in latch]
    base_vals = [r.signal.value for r in base_signals]
    var_vals = [r.signal.value for r in var_signals]

    raw_div = sum(1 for a, b in zip(base_vals, latch_vals) if a != b)
    if len(base_vals) != len(latch_vals):
        raw_div += abs(len(base_vals) - len(latch_vals))

    signal_diffs: list[int] = [
        i for i in range(len(bars)) if base_vals[i] != var_vals[i]
    ]
    latch_div, outside_intended = 0, []
    for i in signal_diffs:
        act = var_shadow[i]["action"] if i < len(var_shadow) else ""
        if act in ("exit", "deferexit"):
            latch_div += 1
        elif act == "hold" and base_vals[i] in ("BUY", "SELL") and var_vals[i] == "HOLD":
            pass  # carry-aligned: validated below via entry_explained
        else:
            outside_intended.append(
                {"index": i, "base": base_vals[i], "variant": var_vals[i]}
            )

    entry_base = _entry_fills(base_journal)
    entry_var = _entry_fills(var_journal)
    var_pos_after = {e["timestamp"]: e.get("position_after") for e in var_journal}
    entry_added = [
        r for r in entry_var if r["ts"] not in {x["ts"] for x in entry_base}
    ]
    entry_omitted, entry_explained = [], []
    for r in entry_base:
        if r["ts"] in {x["ts"] for x in entry_var}:
            continue
        after = var_pos_after.get(r["ts"]) or {}
        side_ok = (after.get("side") == "LONG") if r["side"] == "BUY" else (
            after.get("side") == "SHORT"
        )
        if side_ok:
            entry_explained.append(r)
        else:
            entry_omitted.append(r)

    close_base = _closing(base_journal)
    close_var = _closing(var_journal)
    base_ts = {(x["ts"], x["side"], bool(x["stop"])) for x in close_base}
    var_ts = {(x["ts"], x["side"], bool(x["stop"])) for x in close_var}
    exit_deferred = [
        x for x in close_base if (x["ts"], x["side"], bool(x["stop"])) not in var_ts
    ]
    exit_allowed_later = [
        x for x in close_var if (x["ts"], x["side"], bool(x["stop"])) not in base_ts
    ]

    stops_base = sum(1 for e in base_journal if e.get("stop_fill"))
    stops_var = sum(1 for e in var_journal if e.get("stop_fill"))
    risk_base = sum(
        1 for e in base_journal if e.get("order_side") and e.get("risk_approved") is False
    )
    risk_var = sum(
        1 for e in var_journal if e.get("order_side") and e.get("risk_approved") is False
    )
    fills_base = sum(1 for e in base_journal if e.get("fill_price") is not None)
    fills_var = sum(1 for e in var_journal if e.get("fill_price") is not None)

    pos_div = 0
    var_by_ts = {e["timestamp"]: e for e in var_journal}
    for e in base_journal:
        v = var_by_ts.get(e["timestamp"])
        if v is None:
            continue
        a = (e.get("position_after") or {}).get("side")
        b = (v.get("position_after") or {}).get("side")
        if a != b:
            pos_div += 1

    ok = (
        raw_div == 0
        and not entry_added
        and not entry_omitted
        and not outside_intended
    )
    return {
        "raw_divergences": raw_div,
        "latch_divergences": latch_div,
        "entry_divergences_added": len(entry_added),
        "entry_divergences_omitted": len(entry_omitted),
        "entry_divergences_explained_carry": len(entry_explained),
        "exit_divergences_deferred": len(exit_deferred),
        "exit_divergences_allowed_later": len(exit_allowed_later),
        "stop_divergences": {"baseline": stops_base, "variant": stops_var},
        "risk_divergences": {"baseline": risk_base, "variant": risk_var},
        "fill_divergences": {"baseline": fills_base, "variant": fills_var},
        "position_state_divergences": pos_div,
        "outside_intended_exits": outside_intended,
        "ok": ok,
    }


def exit_attribution(
    base_journal: list[dict],
    var_journal: list[dict],
    var_shadow: list[dict],
) -> dict:
    """Per-changed-exit attribution (Phase H). No cherry-picking: every baseline
    exit fill the variant did not reproduce at the same timestamp is one row,
    plus aggregate outcome categories.
    """
    shadow_by_ts = {row["ts"]: row for row in var_shadow}
    close_base = _closing(base_journal)
    close_var = _closing(var_journal)
    var_by_ts = {(x["ts"], x["side"], bool(x["stop"])): x for x in close_var}

    base_pos_by_ts = {}
    for e in base_journal:
        if e.get("position_before") is not None:
            base_pos_by_ts[e["timestamp"]] = (e["position_before"] or {}).get("side", "")

    rows = []
    categories = {
        "prevented_loss": 0,
        "delayed_profitable_exit": 0,
        "additional_loss": 0,
        "additional_churn": 0,
        "still_open_at_end": 0,
        "eventually_equal": 0,
        "improved_or_other": 0,
    }
    for b in close_base:
        key = (b["ts"], b["side"], bool(b["stop"]))
        if key in var_by_ts:
            continue  # unchanged exit (allowed at the original bar)
        sh = shadow_by_ts.get(b["ts"]) or {}
        base_pos = base_pos_by_ts.get(b["ts"], "LONG")
        close_side = "SELL" if base_pos == "LONG" else "BUY"
        eventual = next(
            (x for x in close_var if x["ts"] >= b["ts"] and x["side"] == close_side),
            None,
        )
        try:
            var_real = (
                Decimal(eventual["realized"])
                if eventual and eventual["realized"]
                else None
            )
        except (ValueError, TypeError, ArithmeticError):
            var_real = None
        try:
            base_real = Decimal(b["realized"]) if b["realized"] else None
        except (ValueError, TypeError, ArithmeticError):
            base_real = None

        if var_real is None:
            category = "still_open_at_end"
        elif base_real is not None and base_real < 0 and var_real >= 0:
            category = "prevented_loss"
        elif base_real is not None and base_real > 0 and var_real < base_real:
            category = "delayed_profitable_exit"
        elif var_real < 0 and (base_real is None or base_real >= 0):
            category = "additional_loss"
        elif var_real == base_real:
            category = "eventually_equal"
        else:
            category = "improved_or_other"
        categories[category] = categories.get(category, 0) + 1
        rows.append(
            {
                "original_exit_ts": b["ts"],
                "side": b["side"],
                "position_before": base_pos,
                "original_signal_reason": sh.get("reason", ""),
                "exit_adx": sh.get("adx", ""),
                "decision": sh.get("action", ""),
                "eventual_exit_ts": eventual["ts"] if eventual else "",
                "original_exit_pnl": str(base_real) if base_real is not None else "",
                "variant_exit_pnl": str(var_real) if var_real is not None else "",
                "category": category,
            }
        )

    e_base = {x["ts"] for x in _entry_fills(base_journal)}
    e_var = {x["ts"] for x in _entry_fills(var_journal)}
    downs_entries_changed = len(e_base - e_var)

    return {
        "changed_exits": len(rows),
        "entries_changed_downstream": downs_entries_changed,
        "categories": categories,
        "rows": rows,
    }


_ENTRY_ADX = Decimal("25")


def confirm_entries_exits(
    bars,
    strategy: MultiIndicatorStrategy,
    *,
    entry_adx: Decimal = _ENTRY_ADX,
    exit_adx: Decimal = _EXIT_ADX,
    slippage: Decimal = _SLIPPAGE,
    stop_pct: Decimal = _STOP_PCT,
    max_daily_loss: Decimal = _MAX_DAILY_LOSS,
    shadow: _Shadow | None = None,
) -> tuple[list[SignalResult], list[dict]]:
    """Iteration-004 combined ADX entry + exit confirmation overlay.

    ONE single series drives ONE unchanged engine run, fusing the two proven
    overlays on a single shadow replica:

    * Entry confirmation (Iteration-002 semantics): a flat->position transition
      opens only when the entry-bar ADX is >= ``entry_adx``. Otherwise the entry
      is DEFERRED (no order placed) and re-attempted on later bars while the
      original keeps the same bias, or DROPPED if the bias flips first.
    * Exit confirmation (Iteration-003 semantics): an original exit bar executes
      only when the exit-bar ADX is < ``exit_adx`` or unavailable; otherwise the
      position is HELD (the carry is disclosed as ``hold_carry`` /
      ``hold_aligned`` / ``hold_deferred`` so the engine never stacks).

    A deferred exit may therefore carry a position across the original's later
    entries/exits; when the original rotates into the opposite bias while the
    overlay still holds, the overlay closes on the exit gate (never reverses in
    one bar) and the original's new bias becomes a pending entry candidate.
    The protective stop and risk daily-loss gate are untouched (the engine still
    enforces them) and mirrored by the shadow so the overlay never disagrees
    with the engine; ``verify_execution_sync`` validates this bar-for-bar.
    """
    raw = bulk_signals(bars, strategy, latch=True)
    if len(raw) != len(bars):
        raise ValueError("signal series length must equal bars length")
    orig_before, orig_after = _orig_trajectory(raw)
    shadow_replica = shadow if shadow is not None else _Shadow(
        slippage=slippage, stop_pct=stop_pct, max_daily_loss=max_daily_loss
    )

    series: list[SignalResult] = []
    shadow_rows: list[dict] = []
    pending: Signal | None = None
    for i, r in enumerate(raw):
        s = r.signal
        bar = bars[i]
        adx = _feat_decimal(r.meta, "adx")
        tb, target = orig_before[i], orig_after[i]
        pos = shadow_replica.pos
        pos_before = pos

        action = "hold_flat"
        reason = ""
        emit: Signal | None = None
        rotated = False
        pending_side = 1 if pending is Signal.BUY else (-1 if pending is Signal.SELL else 0)

        if pos != 0:
            if target == pos:
                if tb == 0 and target != 0:
                    action = "hold_carry"
                    reason = "original entry carried: already " + (
                        "LONG" if pos == 1 else "SHORT"
                    )
                else:
                    action = "hold_aligned"
                    reason = "aligned (position matches original)"
            elif tb == 0 and target == 0:
                action = "hold_deferred"
                reason = "holding deferred exit while original sits flat"
            elif tb != 0 and target == 0:
                if _allow_exit(adx, exit_adx):
                    emit = Signal.BUY if pos == -1 else Signal.SELL
                    action = "exit"
                    reason = (
                        "exit allowed (exit-bar ADX "
                        + (
                            "unavailable; no trap"
                            if adx is None
                            else f"{adx:.2f} < {exit_adx}"
                        )
                        + ")"
                    )
                else:
                    action = "exit_deferred"
                    reason = (
                        f"exit DEFERRED; exit-bar ADX {adx:.2f} >= {exit_adx} "
                        "(strong-trend momentum held)"
                    )
            else:
                rotated = True
                if _allow_exit(adx, exit_adx):
                    emit = Signal.BUY if pos == -1 else Signal.SELL
                    action = "exit"
                    reason = "exit allowed (original rotated to opposite bias)"
                else:
                    action = "exit_deferred"
                    reason = (
                        f"exit DEFERRED (original rotated opposite; exit-bar ADX "
                        f"{adx:.2f} >= {exit_adx})"
                    )
        elif pending is not None and target == pending_side:
            if adx is not None and adx >= entry_adx:
                emit = pending
                action = "entry_reconfirmed"
                reason = f"ADX entry reconfirmed ({adx:.2f} >= {entry_adx:.2f})"
            else:
                action = "entry_still_deferred"
                reason = (
                    "ADX unavailable; entry still deferred"
                    if adx is None
                    else f"ADX {adx:.2f} < {entry_adx:.2f}; entry still deferred"
                )
        elif tb == 0 and target != 0:
            if adx is not None and adx >= entry_adx:
                emit = Signal.BUY if target == 1 else Signal.SELL
                action = "entry"
                reason = f"ADX entry confirmed ({adx:.2f} >= {entry_adx:.2f})"
            else:
                action = "entry_deferred"
                pending = Signal.BUY if target == 1 else Signal.SELL
                reason = (
                    "ADX unavailable; entry deferred"
                    if adx is None
                    else f"ADX {adx:.2f} < {entry_adx:.2f}; entry deferred"
                )
        elif pending is not None:
            action = "dropped_pending"
            reason = "entry dropped: original bias flipped before ADX confirmation"
            pending = None
        elif tb != 0 and target == 0:
            action = "noop_exit_ignored"
            reason = "original exit while flat: nothing to close"
        else:
            action = "hold_flat"
            reason = "no directional bias yet"

        fill = None
        risk_blocked = False
        if emit is not None:
            if shadow_replica.risk_blocked(bar):
                risk_blocked = True
                action = "risk_blocked"
                reason = reason + " ; order blocked by daily loss limit (replicated)"
            else:
                fill = shadow_replica.apply_fill(bar=bar, side=emit.value, idx=i)
                if action in ("entry", "entry_reconfirmed"):
                    pending = None
                elif action == "exit" and rotated:
                    pending = Signal.BUY if target == 1 else Signal.SELL

        stop_fill = None
        if shadow_replica.pos == 1 and i > shadow_replica.opened_idx:
            stop_fill = shadow_replica.apply_stop(bar=bar, idx=i)

        series.append(
            SignalResult(
                signal=emit if emit is not None else Signal.HOLD,
                instrument=r.instrument,
                timestamp=r.timestamp,
                reason=reason or r.reason,
                meta=dict(r.meta or {}),
            )
        )

        shadow_rows.append(
            {
                "ts": bar.timestamp.isoformat() if bar.timestamp is not None else "",
                "raw_signal": s.value,
                "signal": emit.value if emit is not None else "HOLD",
                "bias": "LONG" if target == 1 else ("SHORT" if target == -1 else ""),
                "adx": str(adx) if adx is not None else "",
                "orig_before": tb,
                "orig_after": target,
                "pos_before": pos_before,
                "pos_after": shadow_replica.pos,
                "pending": pending_side,
                "action": action,
                "reason": reason,
                "emit": emit.value if emit is not None else "",
                "risk_blocked": risk_blocked,
                "stop_fill": bool(stop_fill),
                "entry_price": (
                    str(shadow_replica.entry) if shadow_replica.entry is not None else ""
                ),
                "realized": (
                    str(fill["realized"])
                    if fill and fill.get("realized") is not None
                    else ""
                ),
            }
        )
    return series, shadow_rows


def run_combined_variant(
    bars,
    strategy: MultiIndicatorStrategy,
    config,
    engine,
    *,
    entry_adx: Decimal = _ENTRY_ADX,
    exit_adx: Decimal = _EXIT_ADX,
) -> dict:
    """Run the combined variant: overlay -> unchanged engine -> sync + proof."""
    slippage = getattr(config, "slippage_rate", None)
    exec_conf = getattr(config, "execution", None)
    if exec_conf is not None:
        slippage = getattr(exec_conf, "total_adverse_rate", slippage)
    if slippage is None:
        slippage = _SLIPPAGE
    series, shadow_rows = confirm_entries_exits(
        bars,
        strategy,
        entry_adx=entry_adx,
        exit_adx=exit_adx,
        slippage=slippage,
        stop_pct=getattr(config, "stop_loss_pct", _STOP_PCT),
        max_daily_loss=getattr(config, "max_daily_loss", _MAX_DAILY_LOSS),
    )
    journal = []
    result = engine.run(bars, strategy, config, signals=series, journal=journal)
    sync = verify_execution_sync(journal, shadow_rows)
    return {
        "series": series,
        "shadow": shadow_rows,
        "journal": journal,
        "result": result,
        "sync": sync,
    }


_INTENDED_COMBINED_ACTIONS = {
    "entry",
    "entry_reconfirmed",
    "entry_deferred",
    "entry_still_deferred",
    "dropped_pending",
    "exit",
    "exit_deferred",
    "hold_carry",
    "hold_aligned",
    "hold_deferred",
    "hold_flat",
    "noop_exit_ignored",
    "risk_blocked",
}


def _combined_entry_coverage(
    base_journal: list[dict],
    var_journal: list[dict],
    var_shadow: list[dict],
) -> dict:
    """Classify every baseline entry under the combined variant.

    Categories: ``exact`` (same bar, same side), ``carried`` (the overlay
    already holds that side), ``reconfirmed`` (opened on a later bar while the
    same original bias persisted) and ``dropped`` (the entry gate never
    confirmed before the original bias flipped). Every entry must land in one of
    these; ``dropped`` is acceptable only for gate-deferred bars.
    """
    shadow_by_ts = {row["ts"]: row for row in var_shadow}
    entry_base = _entry_fills(base_journal)
    entry_var = _entry_fills(var_journal)
    var_entries_by_ts = {x["ts"]: x for x in entry_var}
    var_pos_before_by_ts = {
        e["timestamp"]: (e.get("position_before") or {}) for e in var_journal
    }

    dropped = []
    classes = {"exact": 0, "carried": 0, "reconfirmed": 0, "dropped": 0}
    rows = []
    for b in entry_base:
        ts = b["ts"]
        side = b["side"]
        same_ts = var_entries_by_ts.get(ts)
        if same_ts is not None and same_ts["side"] == side:
            classes["exact"] += 1
            rows.append({"ts": ts, "side": side, "class": "exact"})
            continue
        pb = var_pos_before_by_ts.get(ts) or {}
        side_ok = (pb.get("side") == "LONG") if side == "BUY" else (
            pb.get("side") == "SHORT"
        )
        if side_ok:
            classes["carried"] += 1
            rows.append({"ts": ts, "side": side, "class": "carried"})
            continue
        sh = shadow_by_ts.get(ts) or {}
        if sh.get("action") in ("entry_deferred", "entry_still_deferred"):
            # Follow this pending's lifecycle to its terminator.
            out_cls = "dropped"
            detail = "never confirmed before end of series"
            for row in var_shadow:
                if row["ts"] <= ts:
                    continue
                act = row["action"]
                if act == "entry_reconfirmed" and row["emit"] == side:
                    out_cls = "reconfirmed"
                    detail = row["ts"]
                    break
                if act in ("dropped_pending", "entry") or (
                    act == "entry_reconfirmed" and row["emit"] != side
                ):
                    detail = "bias flipped before ADX confirmation"
                    break
            if out_cls == "reconfirmed":
                classes["reconfirmed"] += 1
                rows.append(
                    {"ts": ts, "side": side, "class": "reconfirmed",
                     "reconfirmed_ts": detail}
                )
            else:
                classes["dropped"] += 1
                rows.append({"ts": ts, "side": side, "class": "dropped"})
            continue
        dropped.append(
            {"ts": ts, "side": side, "class": "dropped_unexplained",
             "action": sh.get("action", "")}
        )
    return {
        "classes": classes,
        "rows": rows,
        "dropped": dropped,
        "ok": not dropped,
    }


def _combined_exit_coverage(
    base_journal: list[dict],
    var_journal: list[dict],
    entry_coverage: dict,
) -> dict:
    """Classify every baseline closing fill under the combined variant."""
    coverage_by_ts = {r["ts"]: r for r in entry_coverage["rows"]}
    close_base = _closing(base_journal)
    close_var = _closing(var_journal)
    var_by_ts = {(x["ts"], x["side"], bool(x["stop"])): x for x in close_var}

    def _var_pos_after(ts: str) -> int:
        row = [e for e in var_journal if e["timestamp"] == ts]
        pa = (row[0].get("position_after") or {}) if row else {}
        if pa.get("side") == "LONG":
            return pa.get("quantity", 0) or 0
        if pa.get("side") == "SHORT" and pa.get("quantity", 0):
            return -pa["quantity"]
        return 0

    # Pair each baseline close to its round-trip entry (journal order).
    open_side_by_ts: dict[str, str] = {}
    prev_entry: dict | None = None
    for e in base_journal:
        if e.get("entry_trade_id") and e.get("fill_price") is not None:
            prev_entry = e
        if e.get("exit_trade_id") and prev_entry is not None:
            open_side_by_ts[e["timestamp"]] = prev_entry["timestamp"]

    cats = {
        "unchanged": 0,
        "deferred_allowed_later": 0,
        "still_open_at_end": 0,
        "noop_exit_ignored": 0,
    }
    rows = []
    invalid = []
    for b in close_base:
        key = (b["ts"], b["side"], bool(b["stop"]))
        if key in var_by_ts:
            cats["unchanged"] += 1
            rows.append({"ts": b["ts"], "side": b["side"], "class": "unchanged"})
            continue
        entry_ts = open_side_by_ts.get(b["ts"], "")
        entry_cls = coverage_by_ts.get(entry_ts, {}).get("class", "")
        if _var_pos_after(b["ts"]) == 0 and entry_cls in ("dropped",):
            cats["noop_exit_ignored"] += 1
            rows.append(
                {"ts": b["ts"], "side": b["side"], "class": "noop_exit_ignored"}
            )
            continue
        close_side = b["side"]
        later = next(
            (x for x in close_var if x["ts"] > b["ts"] and x["side"] == close_side),
            None,
        )
        if later:
            cats["deferred_allowed_later"] += 1
            rows.append(
                {"ts": b["ts"], "side": b["side"], "class": "deferred_allowed_later",
                 "later_ts": later["ts"]}
            )
        elif _var_pos_after(b["ts"]) != 0:
            cats["still_open_at_end"] += 1
            rows.append(
                {"ts": b["ts"], "side": b["side"], "class": "still_open_at_end"}
            )
        else:
            invalid.append(
                {"ts": b["ts"], "side": b["side"],
                 "detail": "exit not reproduced and no explained reason"}
            )
    return {"categories": cats, "rows": rows, "invalid": invalid, "ok": not invalid}


def verify_combined_state_machine(
    bars,
    strategy: MultiIndicatorStrategy,
    base_signals: list[SignalResult],
    var_signals: list[SignalResult],
    base_journal: list[dict],
    var_journal: list[dict],
    var_shadow: list[dict],
) -> dict:
    """Machine-checkable state-machine proof for the combined variant (I-004).

    Divergence classes: action vocabulary, per-bar position transitions (no
    direct reversals), entry sanction (no invented entries), baseline entry
    coverage (exact/carried/reconfirmed/dropped), baseline exit coverage
    (unchanged / deferred-allowed-later / still-open / noop), and bar-level
    position state. ``ok`` requires no violation in any class.
    """
    raw = bulk_signals(bars, strategy, latch=False)
    latch = latched_signals(raw, initial=None)
    latch_vals = [r.signal.value for r in latch]
    base_vals = [r.signal.value for r in base_signals]
    var_vals = [r.signal.value for r in var_signals]

    raw_div = sum(1 for a, b in zip(base_vals, latch_vals) if a != b)
    if len(base_vals) != len(latch_vals):
        raw_div += abs(len(base_vals) - len(latch_vals))

    entry_coverage = _combined_entry_coverage(base_journal, var_journal, var_shadow)
    exit_coverage = _combined_exit_coverage(
        base_journal, var_journal, entry_coverage
    )

    shadow_by_ts = {row["ts"]: row for row in var_shadow}
    bad_actions = []
    entry_sanction = []
    reversals = []
    pos_div = 0
    var_by_ts = {e["timestamp"]: e for e in var_journal}

    for row in var_shadow:
        if row["action"] not in _INTENDED_COMBINED_ACTIONS:
            bad_actions.append(
                {"ts": row["ts"], "action": row["action"]}
            )
        if row["emit"] and not row["risk_blocked"]:
            pa, pb = row["pos_after"], row["pos_before"]
            if pb * pa == -1:
                reversals.append({"ts": row["ts"], "before": pb, "after": pa})
            if row["action"] not in ("entry", "entry_reconfirmed", "exit"):
                entry_sanction.append(
                    {"ts": row["ts"], "action": row["action"], "emit": row["emit"]}
                )

    for e in var_journal:
        if e.get("entry_trade_id") and e.get("fill_price") is not None:
            row = shadow_by_ts.get(e["timestamp"]) or {}
            side = 1 if e.get("order_side") == "BUY" else -1
            if row.get("action") not in ("entry", "entry_reconfirmed"):
                entry_sanction.append(
                    {"ts": e["timestamp"], "kind": "unverified_entry",
                     "action": row.get("action", ""), "side": e.get("order_side")}
                )
            elif row.get("orig_after") != side:
                entry_sanction.append(
                    {"ts": e["timestamp"], "kind": "entry_bias_mismatch",
                     "orig_after": row.get("orig_after"), "side": e.get("order_side")}
                )

    for e in base_journal:
        v = var_by_ts.get(e["timestamp"])
        if v is None:
            continue
        a = (e.get("position_after") or {}).get("side")
        b = (v.get("position_after") or {}).get("side")
        if a != b:
            pos_div += 1

    ok = (
        raw_div == 0
        and not bad_actions
        and not entry_sanction
        and not reversals
        and entry_coverage["ok"]
        and exit_coverage["ok"]
    )
    return {
        "raw_divergences": raw_div,
        "bad_actions": bad_actions,
        "entry_sanction_violations": entry_sanction,
        "direct_reversals": reversals,
        "entry_coverage": entry_coverage,
        "exit_coverage": exit_coverage,
        "position_state_divergences": pos_div,
        "ok": ok,
    }
