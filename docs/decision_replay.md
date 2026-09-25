# Decision Replay — RAW SIGNAL vs FINAL DECISION

## The core idea

Every 5-minute bar for every candidate is replayed so you can see **two decisions**:

- **raw_signal** — what the strategy alone concluded (BUY / SELL / HOLD).
- **final_decision** — what the *system* actually did after the full pipeline: risk manager
  admission, volatility/regime filters, protective stop rules, position/sizing guards,
  daily-loss limit, and mechanical reads (WARM_UP / HOLDING / TIME_NOT_OPEN). When the final
  decision is NO TRADE the attached canonical `no_trade_reasons` explain *which* condition
  blocked the trade.

The whole point: in this system **the raw signal is not the order**. The GUI makes that gap
visible candle-by-candle and aggregates it (NO TRADE analysis screen).

## Reconstruction, not invention

Historical journals were not saved during the discovery run, so the read model is
**reconstructed** from the immutable artifacts:

- Full-dataset signal replay using the frozen strategy definitions (framework version
  `walkforward-1.0`).
- Per-day fresh-₹1,00,000-portfolio replays for round trips.
- Validation against the report's scorecards — all 7 candidates matched exactly
  (`reconstruction_status = exact`; a bar missing any needed input is `unavailable`, never
  guessed).

Every decision row carries `decision_source=reconstructed` and
`reconstruction_status=exact|unavailable`. Missing/corrupt inputs produce `NOT AVAILABLE`,
never a fabricated value.

## Trade replay window

`GET /api/trades/{trade_id}/replay` returns the round trip plus the surrounding decision
window (same candidate) with per-bar raw/final/status/realized so you can audit why a trade
opened and why it closed.

## API surface (read-only)

| Route | What you get |
|---|---|
| `GET /api/health` | status + journal counts |
| `GET /api/runs` , `GET /api/runs/{run_id}` | run list / detail (artifacts, per-candidate journal) |
| `GET /api/decisions?runId&date&candidateId&finalDecision&pageSize&page` | replayed decisions + raw/final + provenance |
| `GET /api/decisions/{decision_id}` | full candle detail (features, risk checks, no-trade codes, position deltas) |
| `GET /api/no-trade` | canonical reason/category aggregates |
| `GET /api/trades` , `GET /api/trades/{trade_id}/replay` | round trips + decision window |
| `GET /api/pnl` | research vs paper, strictly separated |
| `GET /api/risk` | blocked pre-trade evaluations, daily-loss hits |
| `GET /api/algorithms` | the 7 frozen candidates |
| `GET /api/provenance` | datasets, runs, artifacts, hashes |
| `GET /api/system-health` | live gate CLOSED + reconstruction status |
| `GET /api/win-learning` | analytics-only regime/hour/duration buckets + honesty gate |

## Router screens

- **Decision Replay** (primary): browse by run/date/candidate/final, click a row for the full
  candle detail.
- **Trade Replay**: round trips + per-bar decision windows.
- **NO TRADE Analysis**: canonical codes and categories with share bars.
- **Win-Learning**: lift-vs-base by regime/hour/duration, guarded by an honesty band because
  the sample shows no net-profitable run (see below).

## Honesty guardrails

- P&L separates **research** (reconstructed, net of frictions) from **paper** (real sessions;
  currently `NO PAPER TRADES YET`, capital = `NOT AVAILABLE`).
- Win-learning is **analytics only** — not wired into any learning engine — and its honesty
  band states `all_runs_net_negative` when the persisted runs are net-negative, while still
  reporting individual winning trades.