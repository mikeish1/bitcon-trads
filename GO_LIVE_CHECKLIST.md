# ✅ Safe Go-Live Checklist — read every line before risking real money

This bot defaults to **paper trading**. Switching to **real money** is a
deliberate, irreversible-with-your-cash decision. Do **not** skip steps. If you
can't tick a box, you are not ready.

> ⚠️ Crypto can lose money fast, including your entire balance. This software has
> **no guarantee of profit** and may contain bugs. Going live is **your** decision
> and **your** risk. Honest expectation: this strategy aims for **lower drawdown
> and better risk-adjusted return than buy-and-hold** — it will still **lose money
> in a broad crypto downturn**, just less. It does **not** beat a raw bull market.

---

## 1. Review the backtest (know what you're signing up for)
- [ ] Run it yourself: `python src/backtester.py --split 2024-06-01`
- [ ] Confirm the **out-of-sample (OOS)** column, not just full-period — OOS is
      the honest test.
- [ ] You understand the realistic profile: **~30% time in market**, **~5–10
      trades per coin per year**, **max drawdown still ~−40% to −45%** in bad
      periods (better than buy-and-hold's ~−80%, but large).
- [ ] You accept it **sits in cash and does nothing** for long stretches
      (especially while BTC is below its regime MA). Quiet ≠ broken.

## 2. Paper-trade first (non-negotiable)
- [ ] Paper-trade (Alpaca paper or simulation) for **at least 2–4 weeks**.
- [ ] You have seen at least **one full cycle**: a BUY → trailing stop ratchets
      up → SELL (exit), with the numbers making sense in the logs/Telegram.
- [ ] You have seen a **regime flip** (RISK-ON ↔ RISK-OFF) behave correctly.
- [ ] No errors/crashes in the logs over the test period.
- [ ] Set your Alpaca paper balance to roughly your **intended real size** (e.g.
      $250) so paper position sizes match reality.

## 3. Choose venue & confirm API permissions
**Binance.US (real money):**
- [ ] API key created at binance.us → API Management.
- [ ] ✅ **Enable Reading** and ✅ **Enable Spot Trading**.
- [ ] ❌ **Withdrawals DISABLED** — the bot never needs to move funds out. This
      is your single most important safety setting.
- [ ] *(Local runs only)* IP-restrict the key to your home IP. On Railway you
      can't (dynamic IP) — rely on the no-withdrawal setting.
- [ ] Account holds a small amount of **USDT** to trade with.

**Alpaca (real money):**
- [ ] Use your **live** (not paper) Alpaca keys, with crypto trading enabled.
- [ ] Set `ALPACA_PAPER=false` (in addition to the two switches below).
- [ ] ❌ No transfer/withdrawal scopes beyond trading.

## 4. Start very small
- [ ] Fund the account with an amount you are 100% willing to lose — start at
      **$100–$250**, not your savings.
- [ ] Set `DEFAULT_CAPITAL_USD` to match (used as a fallback/sizing reference).
- [ ] Keep the conservative defaults: ≤30% per coin, ≤90% total exposure, max 3
      positions, daily/weekly loss limits, circuit breaker. **Do not loosen
      these for your first live month.**

## 5. Flip the two-key tripwire (this is "go live")
Real orders require **BOTH**:
```
PAPER_TRADING=false
LIVE_TRADING_ENABLED=true
```
(Alpaca live also needs `ALPACA_PAPER=false`.)
- [ ] Change them in **Railway → Variables** (or your local `.env`).
- [ ] Redeploy / restart.
- [ ] Confirm the startup banner says **`mode=LIVE (REAL MONEY)`** and the
      universe lists the coins you expect.
- [ ] Watch the **first** real BUY and its stop order appear on the exchange.

## 6. Kill switch & emergency procedures (know these BEFORE you need them)
- [ ] **Soft stop (no new trades):** set `LIVE_TRADING_ENABLED=false` (or
      `PAPER_TRADING=true`) and redeploy. The bot drops back to paper — it stops
      placing real orders. Existing exchange stop-limit orders remain.
- [ ] **Hard stop:** in Railway, **Remove/Pause the deployment** (or locally,
      Ctrl+C). The process stops. ⚠️ With the bot stopped, the **software
      trailing stop no longer updates** — only the last exchange-side stop-limit
      protects you. Don't leave a live position unmonitored with the bot off.
- [ ] **Emergency exit your money:** log into the exchange and **manually sell**
      your coin positions to cash. The bot will reconcile (see the position is
      gone) on its next run.
- [ ] **Compromise / panic:** **revoke the API key** in the exchange dashboard.
      That instantly stops all bot trading regardless of what's deployed.
- [ ] Write down where each of the above lives so you can do it in 60 seconds.

## 7. Monitoring plan
- [ ] **Telegram alerts ON** — you get pinged on every buy, sell (with PnL),
      regime flip, circuit breaker, and error. This is your primary monitor.
- [ ] Check **Railway logs** at least daily for the first 1–2 weeks.
- [ ] Watch the **daily summary** (logs/Telegram): equity, open positions, win
      rate, consecutive losses.
- [ ] Know your auto-stops: **daily −3% / weekly −7%** loss limits and a
      **4-consecutive-loss** circuit breaker will pause trading on their own.
- [ ] Re-check the account balance on the exchange weekly vs the bot's reported
      equity — they should roughly agree.

## 8. Ongoing discipline
- [ ] Run live for a month at minimum size before considering scaling up.
- [ ] Don't tune parameters reactively after a loss — that's how you overfit.
- [ ] Re-run the backtest after any config change and re-read section 1.

---

**If any box above is unchecked, stay in paper.** A missed trade costs nothing;
a rushed go-live costs real money.
