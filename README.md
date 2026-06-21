# 🪙 Long-Only BTC Spot Trend-Follower (Binance.US / Alpaca)

An autonomous, long-only Bitcoin bot for spot trading. The **active strategy is a
daily Donchian breakout trend-follower** — it buys when BTC breaks to a new
multi-week high, rides the position with an ATR "chandelier" trailing stop that
lets winners run, and sits in cash the rest of the time (~60–70%).

This strategy was chosen because it is the **only design that beat buy-and-hold
out-of-sample** in our research: comparable-or-better risk-adjusted return with
roughly **half the drawdown** (~−30% vs BTC's ~−77%). It **will not** out-return
a raging bull market — its edge is downside protection and risk-adjusted growth,
not beating BTC's raw return. See "Strategy & validation" below.

It **defaults to PAPER mode** (simulated, no real money) and requires **two
separate switches** to ever place a real order.

> An older "high-conviction" 5-minute multi-timeframe strategy also ships
> (`strategy.mode: high_conviction`) but was validated **unprofitable** and is off
> by default. The section below it describes that legacy mode.

> ⚠️ **Risk warning — read this.** Crypto trading can lose money, up to your
> entire balance. This software has **no guarantee of profit**, may contain bugs,
> and trades autonomously. **You are solely responsible for any real trades.**
> Start in paper mode, test thoroughly, and never trade money you can't afford to
> lose. Long-only spot means your worst case on an open trade is the asset going
> to zero between stop checks — size accordingly.

---

## What it does (plain English) — active Donchian trend-follower

1. Once a day it pulls **daily** BTC candles.
2. **Entry:** if today's close is a fresh **40-day high** (a breakout = momentum),
   it buys with (almost) all available cash.
3. **Exit:** it tracks the highest close since you bought and exits when price
   falls **3 × ATR** below that high (a "chandelier" trailing stop). There is **no
   fixed profit target** — winners are allowed to run.
4. The rest of the time it holds **cash**. It's in the market only ~30–40% of the
   time, which is how it sidesteps the worst crashes.

That's the whole strategy — deliberately simple. It trades roughly **5–10 times a
year**. Patience is the edge: every high-turnover variant we tested lost money.

### Strategy & validation
On 6.7 years of daily BTC (2019→2026), tuned in-sample and judged out-of-sample
against Buy & Hold, DCA, and an MA filter, the Donchian breakout + ATR trail was
the **only** strategy that beat buy-and-hold out-of-sample on risk-adjusted terms,
with ~half the drawdown. The research tools are included:
```
python src/strategy_search.py       # leaderboard of strategy families (OOS-ranked)
python src/regime_backtester.py     # baselines: B&H / DCA / MA filter
```
Honest expectation: it aims to **match BTC's upside with much lower drawdown and
win in choppy/bear markets** — not to beat BTC's raw return in a bull run.

---

## Long-only, spot — what that means

- It can only **buy BTC with USDT**, then later **sell BTC for USDT**. **No shorting.**
- "Flat" = holding USDT. "In a trade" = holding BTC.
- Binance.US is spot-only and has **no testnet**, so paper mode here uses **live
  public prices with simulated fills** — realistic, but no real orders.

---

## Choosing a venue (`EXCHANGE_ID`)

| `EXCHANGE_ID` | Symbol | Paper mode | Real money |
|---|---|---|---|
| `alpaca` *(recommended for testing)* | `BTC/USD` | **Real paper brokerage** — orders are actually placed on Alpaca's paper account (realistic fills, you can watch the account), no real money | `ALPACA_PAPER=false` + the two-key tripwire |
| `binanceus` | `BTC/USDT` | Internal simulation (live prices, simulated fills) | the two-key tripwire |

### Alpaca paper trading (the easy, safe way to test)

1. Create a free account at **alpaca.markets** and open the **Paper** dashboard.
2. Copy your **paper** API key + secret (Home → API Keys).
3. *(Optional, recommended)* Set your paper account's cash to roughly the amount
   you'd really trade (e.g. $250) so position sizes are realistic — Alpaca paper
   starts at $100,000 by default, which makes the bot trade big. Sizing is
   dynamic, so it scales to whatever your paper balance is.
4. In `.env`:
   ```
   EXCHANGE_ID=alpaca
   ALPACA_PAPER=true
   ALPACA_API_KEY=your_key
   ALPACA_API_SECRET=your_secret
   ```
5. Run `python -m src.main_loop`. The banner will say **PAPER-BROKER (Alpaca
   paper)** and real (paper) orders will appear in your Alpaca dashboard.

> Note: Alpaca crypto may not accept exchange-side stop orders; if so, the bot
> logs that and protects the position with its **in-loop software stop** instead.

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

## Telegram notifications (optional)

Get a phone alert whenever the bot **buys**, **sells**, posts a **daily
summary**, or trips a **circuit breaker / error**. Completely optional — if you
don't set it up, the bot runs exactly the same, just without alerts.

### 1. Create a free Telegram bot (2 minutes)
1. In Telegram, search for **@BotFather** and open a chat with it.
2. Send **`/newbot`**. Follow the prompts: give it a name and a username
   (must end in `bot`, e.g. `my_btc_alerts_bot`).
3. BotFather replies with a **token** that looks like
   `123456789:AAExampleTokenStringHere`. Copy it — that's your `TELEGRAM_BOT_TOKEN`.

### 2. Get your chat ID
1. **Send any message** (e.g. "hi") to your new bot in Telegram first — a bot
   can't message you until you've messaged it.
2. In a browser, open (paste your token in place of `<TOKEN>`):
   `https://api.telegram.org/bot<TOKEN>/getUpdates`
3. Look for `"chat":{"id":123456789,...}`. That number is your `TELEGRAM_CHAT_ID`.
   (If it's empty, send your bot another message and refresh the page.)

### 3. Turn it on
**Locally** — add to your `.env`:
```
TELEGRAM_ENABLED=true
TELEGRAM_BOT_TOKEN=123456789:AAExampleTokenStringHere
TELEGRAM_CHAT_ID=123456789
```
**On Railway** — add the same three as **Variables** (Service → Variables tab).

Restart the bot. The log will say **"Telegram notifications: ON"** and you'll get
a "🤖 Trading bot started" message. To disable later, set `TELEGRAM_ENABLED=false`
or just clear the token.

> Your token/chat id are secrets — they live only in `.env` (git-ignored) or in
> Railway Variables, never in the code.

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
