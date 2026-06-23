# Stage 5 — Paper Deployment & Ops Runbook (ETF Static Sleeve)

> Run the **Stage-4-validated static 40/40/20 (SPY/AGG/GLD)** sleeve on **Alpaca
> paper** with full observability and safety. Paper-first: real money needs the
> two-key tripwire **and** the [go-live readiness](go_live_readiness.md) checklist.
> The momentum modes stay off; this runs `selection.mode: static_allocation`.

---

## 1. Run it on Alpaca paper
The ETF bot is a single-replica background worker (`python -m src.etf.main`). For a
**real paper brokerage** run (orders actually placed on Alpaca's paper account, no
real money), set:

| Env var | Value | Why |
|---|---|---|
| `ETF_ENABLED` | `true` | enable the ETF bot |
| `ETF_SELECTION_MODE` | `static_allocation` | run the validated sleeve |
| `ETF_EXECUTION_MODE` | `live` | place real **paper** orders (gated below) |
| `ALPACA_PAPER` | `true` | route to the paper brokerage (no real money) |
| `ALPACA_API_KEY` / `ALPACA_API_SECRET` | *paper keys* | data needs keys too |
| `PAPER_TRADING` | `true` | keep the master tripwire safe |
| `LIVE_TRADING_ENABLED` | `false` | keep the master tripwire safe |
| `ETF_SLEEVE_USD` | e.g. `2000` | paper sleeve size (match intended real size) |
| `TELEGRAM_*` | *(optional)* | phone alerts |

With `ALPACA_PAPER=true`, the banner reads **`mode=PAPER-BROKER (Alpaca paper, no
money)`**. A real-money request *without* the two-key tripwire safely falls back to
sim — verified in `config_etf` and tests.

```bash
pip install -r requirements-etf.txt        # alpaca-py (live/paper path)
ETF_ENABLED=true ETF_SELECTION_MODE=static_allocation ETF_EXECUTION_MODE=live \
ALPACA_PAPER=true ALPACA_API_KEY=... ALPACA_API_SECRET=... python -m src.etf.main
```

## 2. Safety invariants (confirm before starting)
- **Two-key tripwire** intact: real money requires `PAPER_TRADING=false` **and**
  `LIVE_TRADING_ENABLED=true` **and** `ALPACA_PAPER=false` **and**
  `ETF_EXECUTION_MODE=live`. Paper needs none of the money keys.
- **API keys are read+trade only, withdrawals DISABLED.** Most important setting.
- **Single replica.** `railway.json` pins `numReplicas: 1`. Never run two — duplicate
  orders. Run the ETF bot as its **own** worker (separate service / start command).
- **Market-hours guard:** live orders are skipped when the equities market is closed
  (fail-closed if the clock can't be read).
- **PDT guard** on (`etf.pdt_guard`): no same-day round-trips (also structurally
  near-impossible at a quarterly cadence).

## 3. Monitoring
- **Telegram alerts** fire on every **TRIM** / **ADD** (with realized PnL / size),
  every **reconcile** anomaly (position gone / qty-drift = possible split), and every
  **cycle error**. With ~quarterly rebalancing + a 5% drift band, expect **very few**
  messages — silence is normal (the sleeve is buy-and-hold).
- **Logs** print the startup banner, the universe, and per-cycle status
  (`ETF static in-band (no trades)` on most polls, `ETF static rebalanced ...` on a
  rebalance) plus `daily_stats` (equity, held, paper_cash).
- **Weekly check:** reconcile the Alpaca paper account value vs the bot's reported
  equity — they should agree.

## 4. Kill switch & emergency procedures (know these BEFORE you need them)
- **Soft stop (no new orders):** set `ETF_EXECUTION_MODE=sim` (or `ETF_ENABLED=false`)
  and restart → drops to the internal paper ledger, places nothing.
- **Hard stop:** stop/pause the worker (Railway: pause deployment; local: Ctrl+C).
  Positions persist in SQLite and at the broker; no software action continues.
- **Emergency exit:** the sleeve is long-only ETFs — manually sell positions to cash
  in the Alpaca dashboard. The bot reconciles (sees them gone) next run.
- **Compromise/panic:** revoke the Alpaca API key — instantly stops all bot trading.
- Write down where each lives so you can do it in 60 seconds.

## 5. Paper acceptance criteria (must all hold before considering go-live)
- [ ] **≥ 6 weeks** of paper running (the cadence is slow — a quarter is better).
- [ ] **≥ 1 rebalance** observed (a drift-band breach producing TRIM/ADD), with the
      numbers sane in logs/Telegram. (May need a market move to trigger; otherwise the
      ~quarterly clock forces one.)
- [ ] **Holdings track 40/40/20** within the drift band between rebalances.
- [ ] **Zero unhandled errors / crashes** over the period.
- [ ] **Behaviour matches the backtest** within tolerance — the live loop and the
      `simulate_static` backtest share the pure `rebalance_deltas` decision, so the
      *only* expected differences are real fills vs modeled slippage.
- [ ] Paper account value vs bot equity agree on the weekly check.

**Go/No-Go before any paper→live step is the human's call** (Stage 6).
