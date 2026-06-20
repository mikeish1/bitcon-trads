# 🪙 Binance.US Spot — High-Conviction Long-Only BTC Bot

An autonomous, **very conservative** Bitcoin bot for **Binance.US spot**. It buys
BTC with USDT **only on high-conviction long setups** confirmed across multiple
timeframes, protects every position with a stop, and otherwise **stays flat**.

It **defaults to PAPER mode** (simulated, no real money) and requires **two
separate switches** to ever place a real order.

> ⚠️ **Risk warning — read this.** Crypto trading can lose money, up to your
> entire balance. This software has **no guarantee of profit**, may contain bugs,
> and trades autonomously. **You are solely responsible for any real trades.**
> Start in paper mode, test thoroughly, and never trade money you can't afford to
> lose. Long-only spot means your worst case on an open trade is the asset going
> to zero between stop checks — size accordingly.

---

## What it does (plain English)

1. Every ~60 seconds it pulls BTC/USDT candles on **5m, 15m and 1h** from Binance.US.
2. It only considers buying when **all** of these are true (the "gates"):
   - **1h** is in a real uptrend (price above its 50 & 200 EMAs, ADX > 25, +DI > −DI)
   - **15m** agrees (price above its 50 EMA)
   - Market structure shows **higher highs and higher lows**
3. Then it needs a **cluster of bullish triggers** on the 5m candle (RSI pullback
   in the uptrend, MACD turning up, volume confirmation, pullback to a rising EMA,
   etc.) — at least **6 of 8** must fire.
4. Any **bearish veto** (overbought RSI, negative 1h momentum) cancels the setup.
5. Optionally, **Claude** gives a final yes/no on borderline setups.
6. On a buy it sizes the position **dynamically** (risk ~1% of equity, based on the
   ATR stop distance), places an **exchange-side stop**, then **trails the stop up**
   with ATR as price rises. It exits on the trailing stop or take-profit.

Because the bar is so high, **it will stay flat most of the time. That is by design.**

---

## Long-only, spot — what that means

- It can only **buy BTC with USDT**, then later **sell BTC for USDT**. **No shorting.**
- "Flat" = holding USDT. "In a trade" = holding BTC.
- Binance.US is spot-only and has **no testnet**, so paper mode here uses **live
  public prices with simulated fills** — realistic, but no real orders.

---

## Project layout

```
config/trading_config.yaml   # all tunable settings (safe defaults)
src/config.py                # loads YAML + env vars
src/data_pipeline.py         # Binance.US data (5m/15m/1h) + balances
src/strategy.py              # the high-conviction long gate/trigger engine
src/risk_manager.py          # dynamic sizing + ATR stops + safety rails + state
src/executor.py              # Binance.US order placement (+ paper simulation)
src/claude_orchestrator.py   # optional Claude yes/no + daily summary
src/main_loop.py             # the autonomous heartbeat
src/backtester.py            # (older long/short backtester — see note below)
```

> **Note:** `src/backtester.py` reflects the *previous* long/short logic, not the
> new long-only rules. It still runs for rough exploration; a rebuild for the new
> strategy is a planned separate step.

---

## Binance.US API key — exact requirements

1. Log in at **binance.us** → profile → **API Management**.
2. **Create API**, label it (e.g. `btc-bot`), complete 2FA/email verification.
3. **Permissions:**
   - ✅ **Enable Reading**
   - ✅ **Enable Spot Trading**
   - ❌ **Do NOT enable Withdrawals** — the bot never needs to move funds out.
4. **Restrict access to your IP** (recommended, since you'll run it on your own PC):
   add your home IP address. (Skip IP restriction only if running somewhere with a
   changing IP, like Railway.)
5. Copy the **API Key** and **Secret** — the secret is shown **only once**.

Your account also needs some **USDT** to trade with.

---

## Run locally (paper mode — start here)

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows  (macOS/Linux: source .venv/bin/activate)
pip install -r requirements.txt

copy .env.example .env            # macOS/Linux: cp .env.example .env
#   -> edit .env: keep PAPER_TRADING=true and LIVE_TRADING_ENABLED=false.
#      Add your Binance.US keys (optional in paper) and (optional) Anthropic key.

python -m src.main_loop
```

You'll see it load data, then print a decision each candle (usually "FLAT — gate
failed", which is normal). Press **Ctrl+C** to stop.

---

## ✅ Testing protocol before going live (do not skip)

1. **Run paper mode for at least 1–2 weeks.** Read the logs daily. Confirm the
   decisions and (simulated) entries/exits look sane.
2. **Check it actually takes some trades.** If it never trades, loosen
   `strategy.triggers.min_required` or `gates.adx_min` slightly in the YAML and
   keep testing in paper.
3. **Verify balances + permissions** with keys added (still paper): the startup log
   should read your real balance without errors.
4. **First live run = tiny.** Set `DEFAULT_CAPITAL_USD` low and keep only a small
   USDT amount on the account. Watch the very first real buy and its stop order
   appear on Binance.US.
5. Only scale up once you've seen a full **buy → trail → exit** cycle behave
   correctly with real (small) money.

---

## Going live (only after the protocol above)

Real orders need **BOTH** switches flipped:

```
PAPER_TRADING=false
LIVE_TRADING_ENABLED=true
```

Either one alone keeps you in paper mode — this is a deliberate two-key safety
tripwire. When live, the startup banner will say **"LIVE (REAL ORDERS)"**.

Keep the conservative defaults (1% risk/trade, ATR stops, daily/weekly loss
limits, cooldown, max trades/day) until you fully understand them.

---

## Deploy on Railway

1. Push this repo to **GitHub** (private recommended).
2. Railway → **New Project → Deploy from GitHub repo** → pick the repo (it builds
   the `Dockerfile`).
3. Service → **Variables** → add:

   | Variable | Value |
   |---|---|
   | `EXCHANGE_ID` | `binanceus` |
   | `PAPER_TRADING` | `true` *(keep until fully trusted)* |
   | `LIVE_TRADING_ENABLED` | `false` |
   | `BINANCE_API_KEY` | *(your key)* |
   | `BINANCE_API_SECRET` | *(your secret)* |
   | `ANTHROPIC_API_KEY` | *(optional)* |
   | `DEFAULT_CAPITAL_USD` | `250` |

4. *(Recommended)* Add a **Volume** mounted at `/data` and set
   `DB_PATH=/data/trading_state.db` so your trade history survives redeploys.
5. Watch the **Logs** tab.

> ⚠️ **Railway + API key IP restriction:** Railway's outbound IP changes, so you
> generally **cannot** IP-restrict the key there. If you go live on Railway, rely
> on the **no-withdrawal** permission as your protection. For tighter control,
> run live on your own PC with an IP-restricted key instead.

---

## FAQ

**Why does it almost never trade?** That's the point — it only takes
high-conviction, multi-timeframe-confirmed pullbacks in an uptrend. Long flat
stretches are expected.

**Does it need Claude?** No. Without `ANTHROPIC_API_KEY`, borderline setups simply
proceed on the rule engine, and you get no daily summary. Everything else works.

**What protects me if my PC/Railway goes down mid-trade?** Live buys place an
**exchange-side stop-limit** that stays active on Binance.US even if the bot is
offline. The trailing (ratcheting the stop upward) only happens while the bot runs.

**Where are my trades recorded?** In the SQLite file at `DB_PATH` — tables
`trades`, `decisions`, `state`.
