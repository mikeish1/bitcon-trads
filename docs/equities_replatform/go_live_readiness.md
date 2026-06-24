# Stage 6 — Go-Live Readiness (ETF Static Sleeve)

> What a human needs to **consider** before risking real money on the validated
> **static 40/40/20 (SPY/AGG/GLD)** ETF sleeve. **This document does not enable live
> trading — the agent never flips the switches. Going live is your decision and your
> risk.** Companion: [runbook.md](runbook.md), [validation_report.md](validation_report.md).

> ⚠️ This sleeve aims for **lower drawdown and better risk-adjusted, after-tax return
> than buy-and-hold** — it will still **lose money in a broad market downturn**, just
> less (backtest max drawdown ~21%, and ~12.5% in 2022). It is **not** designed to
> beat a raging bull market (SPY out-returned it on raw CAGR). **No profit is
> guaranteed**; the software may contain bugs and trades autonomously.

---

## 1. Know what you're deploying (review the validation)
- [ ] Read [validation_report.md](validation_report.md) §8. Understand: net-of-cost
      **Sharpe ~0.90 (OOS 1.09), max drawdown ~21%, CAGR ~8%**, ~11 trades in 18
      years, **long-term-gains-only** tax.
- [ ] Understand it **beat** Dual Momentum, SPY, and 60/40 on risk-adjusted, after-tax
      OOS terms — but **lagged SPY on raw return** (lower drawdown is the point).
- [ ] Accept the honest caveat: the strong OOS window (2017–26) was favorable for
      diversified portfolios apart from 2022; future Sharpe will likely be lower.
- [ ] Accept that it is **quiet** — it holds and rebalances ~quarterly. Silence ≠ broken.

## 2. Paper-trade first (non-negotiable)
- [ ] Meet **every** Stage-5 paper acceptance criterion ([runbook.md §5](runbook.md)):
      ≥ 6 weeks, ≥ 1 rebalance, holdings track 40/40/20, zero errors, live matches
      backtest, account/equity agree.

## 3. Alpaca permissions (real money)
- [ ] Use your **live** Alpaca keys (not paper); set `ALPACA_PAPER=false`.
- [ ] **No transfer/withdrawal scopes** beyond trading. The bot never moves funds out.
- [ ] Fund with an amount you are 100% willing to lose.

## 4. Start very small (first month)
- [ ] **$100–$250** sleeve, not your savings. Set `ETF_SLEEVE_USD` to match.
- [ ] Keep the conservative defaults untouched: `max_total_exposure_pct: 0.95`,
      `drift_band: 0.05`, `rebalance_days: 63`, `pdt_guard: true`. **Do not loosen
      these for the first month.**
- [ ] Alpaca supports fractional shares, so a small sleeve still holds true 40/40/20.

## 5. The go-live flip (two-key tripwire)
Real orders require **ALL** of:
```
ETF_ENABLED=true
ETF_SELECTION_MODE=static_allocation
ETF_EXECUTION_MODE=live
ALPACA_PAPER=false
PAPER_TRADING=false
LIVE_TRADING_ENABLED=true
```
- [ ] Set them (Railway Variables / local `.env`), redeploy.
- [ ] Confirm the banner says **`mode=LIVE (REAL MONEY)`** and the universe is
      `SPY, AGG, GLD`.
- [ ] Watch the **first** rebalance establish the 40/40/20 position on Alpaca.

## 6. Kill-switch drill (do it once, in paper, before live)
- [ ] **Soft stop:** `ETF_EXECUTION_MODE=sim` → no real orders. Time it.
- [ ] **Hard stop:** pause the worker. Confirm positions persist.
- [ ] **Emergency exit:** manually sell SPY/AGG/GLD to cash in Alpaca; confirm the bot
      reconciles them away next run.
- [ ] **Revoke key:** confirm where to revoke the Alpaca key in 60 seconds.

## 7. Principal protection — the "house-money" rule
- [ ] When the sleeve reaches **~2× your original stake**, **manually withdraw the
      original stake** so you are playing with house money. The bot has **no withdrawal
      permission** — this is a manual transfer you initiate in Alpaca.
- [ ] Set a personal **max-tolerable-drawdown** line (e.g. −25%) you would not cross;
      if breached, stop and reassess rather than adding money.

## 8. Tax & accounting note (TAXABLE account)
- The sleeve is **tax-efficient by design**: ~quarterly rebalancing + a 5% drift band
  means few sales, and in the backtest **100% of realized gains were long-term**
  (held > 1 year), taxed at the lower LT rate. Estimated drag ~13% of gains vs a
  momentum strategy's ~22% (mixed ST/LT).
- **Wash sales are unlikely** (we don't harvest losses or rapidly re-buy), but if you
  *also* hold SPY/AGG/GLD elsewhere, a rebalance loss sale could interact — keep the
  sleeve's tickers distinct from your other holdings to avoid it.
- Keep Alpaca's **1099-B**; rebalances generate reportable sales. Consider holding the
  sleeve in a **tax-advantaged account** to remove tax drag entirely.
- This is **not tax advice** — consult a professional for your situation.

## 9. Ongoing discipline
- [ ] Run live at minimum size for **at least a quarter** before scaling.
- [ ] **Do not tune weights reactively** after a drawdown — that's how you overfit.
      The whole edge is discipline + diversification.
- [ ] Re-read [validation_report.md](validation_report.md) before any change.

---

**Final word.** This system is built to **lower drawdown and improve risk-adjusted,
after-tax survival** for a small taxable account — not to beat a bull market. Going
live is **your decision and your risk**, no profit is guaranteed, and you are solely
responsible for any real trades. If any box above is unchecked, **stay in paper.**
**Stop here — the agent does not enable live trading.**
