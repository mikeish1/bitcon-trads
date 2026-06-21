# 🪙 Long-Only Multi-Crypto Spot Trend-Follower (Binance.US / Alpaca)

An autonomous, long-only **multi-coin** spot bot. The **active strategy is a daily
Donchian breakout trend-follower**, applied independently to every coin in a
config-driven universe (BTC, ETH, BNB, SOL, XRP, DOGE, ADA, VET): it buys a coin
when it breaks to a new multi-week high, rides it with an ATR "chandelier"
trailing stop that lets winners run, and sits in cash the rest of the time. A
**portfolio layer** caps how many coins it holds at once and total exposure.

Coins the live venue doesn't list are **auto-skipped** at startup (e.g. BNB/VET
aren't on Alpaca), so you can list everything and let the system adapt.

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

### The trading universe — adding / removing coins
The universe is **fully config-driven** in `config/trading_config.yaml`:
```yaml
universe:
  bases: [BTC, ETH, BNB, SOL, XRP, DOGE, ADA, VET]   # add/remove coins here
  overrides: {ETH: {entry_period: 30}}               # optional per-coin tuning
portfolio:
  max_concurrent_positions: 3      # hold at most 3 coins at once
  max_total_exposure_pct: 0.90     # cap total deployed across all coins
  per_asset_alloc_pct: 0.30        # cap any single coin at 30% of equity
```
- **To add a coin:** add its base symbol to `bases`. No code changes. If the live
  venue lists it, it starts trading; if not, it's skipped with a warning.
- **Quote currency is automatic:** USD on Alpaca, USDT on Binance.US.
- **Override at runtime** without editing the file via the `SYMBOLS` env var, e.g.
  `SYMBOLS=BTC,ETH,SOL`.

### Allocation mode — how capital is spread across the universe
The Donchian breakout decides *when* each coin is in a trend; the **allocation
mode** decides *which* trending coins actually get your capital. Two modes:
```yaml
strategy:
  allocation:
    mode: "first_come"        # "first_come" (default) | "momentum_rotation"
    momentum_rotation:
      top_k: 4                # hold the 4 strongest active coins at once
      rebalance_days: 2       # rotate at most every N days (RISK exits still any day)
      lookback_days: 90       # momentum = close / close N days ago - 1
      keep_band: 1            # hysteresis: keep a held coin until rank > top_k+keep_band
```
- **`first_come` (default):** every coin that breaks out is sized independently
  under the `portfolio` caps above, in iteration order. This is the original,
  validated behavior — unchanged.
- **`momentum_rotation`:** instead of first-come, hold only the **K strongest**
  coins (by N-day momentum) that are currently in an active Donchian trend,
  rotating at most every `rebalance_days`. Risk exits (chandelier trail, BTC
  risk-off, exchange stops) still fire any day. In out-of-sample research
  (`src/momentum_final.py`) this was a **higher risk-adjusted allocator across
  market regimes** — it turned a ~flat baseline into a clear OOS gain and survived
  stressed fee/slippage assumptions — but it is a **bigger behavioral change** and
  trades more, so **paper-test it first.** It auto-raises
  `max_concurrent_positions` to `top_k` if lower. Flip it fast without editing
  YAML via the `ALLOCATION_MODE=momentum_rotation` env var.

  > Live deviation: the live loop does *whole-position* rotation (enter the
  > strongest, exit drop-outs, let winners run) rather than re-weighting every
  > holding back to 1/K each period as the backtest does — lower turnover, but a
  > slight fidelity gap, so judge it on its paper run, not the backtest number.

### Multi-asset backtests
Same strategy, same standards, every coin — single or multi:
```bash
python src/backtester.py --symbols BTC                 # single asset
python src/backtester.py --symbols BTC,ETH,SOL,XRP,DOGE,ADA   # multi
python src/backtester.py                               # whole config universe
```
It prints **per-asset** metrics (return, drawdown, MAR, Sharpe, % time in market)
and an **equal-weight portfolio** aggregate vs Buy & Hold, split in-sample /
out-of-sample. Results save to `backtests/`.

> Validation result (daily, 2020→2026, OOS after 2024-06): an equal-weight
> BTC/ETH/SOL Donchian portfolio returned **+1406% vs B&H +877% with −60% max
> drawdown vs −91%** (full period), and in the down OOS window lost **−23% vs B&H
> −51%**. Diversifying the trend-follower across coins is more robust than any
> single alt. As always: lower drawdown / better risk-adjusted return, **not**
> beating a raw crypto bull run.

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
src/config.py                # loads YAML + env vars (incl. ALLOCATION_MODE)
src/data_pipeline.py         # per-asset candles + indicators (incl. ATR) + balances
src/strategy.py              # DonchianStrategy (entry + active_state); legacy hi-conviction
src/momentum_allocator.py    # momentum_rotation selector (top-K by momentum)
src/risk_manager.py          # dynamic sizing + ATR stops + safety rails + state
src/executor.py              # order placement (+ paper simulation)
src/claude_orchestrator.py   # optional Claude yes/no + daily summary
src/main_loop.py             # the autonomous heartbeat (first_come / momentum_rotation)

# Research only (never trade; OOS-judged) — see "Strategy & validation":
src/backtester.py            # multi-asset daily Donchian portfolio
src/regime_backtester.py     # baselines: B&H / DCA / MA filter
src/strategy_search.py       # leaderboard of strategy families
src/improve_backtest.py      # A/B/C: baseline / +regime / +vol-target
src/profit_taking_research.py# scale-out/ratchet + momentum vs baseline
src/momentum_controls.py     # momentum controls (vs none/weakest) + param sweeps
src/momentum_final.py        # final: best momentum config + walk-forward by regime
```

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

## Deploy on Railway (step by step)

This bot is a **background worker** — it has no website/port. Railway runs it
24/7, restarts it automatically if it crashes, and shows you live logs. The repo
includes a `Dockerfile` and a `railway.json` so most settings are automatic.

### 1. Put the code on GitHub
Push this repo to GitHub (private recommended). If you used GitHub Desktop,
that's **Publish repository**.

### 2. Create the Railway project
1. Go to **railway.app** → **New Project** → **Deploy from GitHub repo**.
2. Pick this repo. Railway reads `railway.json`, builds the `Dockerfile`, and
   starts the worker. (No "Generate Domain" needed — it's not a web app.)

### 3. Add environment variables
Open the service → **Variables** tab → **New Variable** for each. Start in PAPER.

**Alpaca paper (recommended default):**

| Variable | Value |
|---|---|
| `EXCHANGE_ID` | `alpaca` |
| `ALPACA_PAPER` | `true` |
| `PAPER_TRADING` | `true` |
| `LIVE_TRADING_ENABLED` | `false` |
| `ALPACA_API_KEY` | *(your paper key)* |
| `ALPACA_API_SECRET` | *(your paper secret)* |
| `DEFAULT_CAPITAL_USD` | `250` |
| `TELEGRAM_BOT_TOKEN` | *(optional — phone alerts)* |
| `TELEGRAM_CHAT_ID` | *(optional)* |
| `ANTHROPIC_API_KEY` | *(optional — daily summaries)* |

**Binance.US instead** (all 8 coins, but real-money only — no paper there): set
`EXCHANGE_ID=binanceus`, `BINANCE_API_KEY` / `BINANCE_API_SECRET`, and to trade
the full universe set `SYMBOLS=BTC,ETH,BNB,SOL,XRP,DOGE,ADA,VET`.

> Variables you set here **override** the Dockerfile and YAML defaults. You never
> commit keys — they live only in Railway.

### 4. Recommended Railway settings
- **Replicas: exactly 1.** ⚠️ Never run 2+ — duplicate instances would place
  duplicate orders. (`railway.json` already pins `numReplicas: 1`.)
- **Restart policy:** `ON_FAILURE` with retries — already set in `railway.json`,
  so a crash auto-restarts and your SQLite state resumes where it left off.
- **Resources:** the default small instance is plenty (this is light: a few API
  calls every 15 minutes). No GPU, no big RAM.
- **Region:** any. (If you ever use `binanceus`, pick a US region.)

### 5. Persist the database (recommended)
So your trade history/positions survive redeploys:
1. Service → **Variables** → add `DB_PATH` = `/data/trading_state.db`
2. Service → **Settings → Volumes** → **New Volume**, mount path **`/data`**.
3. Redeploy. State now lives on the volume.

### 6. Monitor it
- **Deployments → View Logs** (or the **Logs** tab) — live, searchable. You'll
  see the startup banner, the tradable universe, the BTC regime state, per-coin
  decisions, and any orders.
- Set up **Telegram** (next section) for push alerts to your phone — the easiest
  way to know it bought, sold, flipped risk-on/off, or hit an error without
  watching logs.
- Railway emails you if the deploy crashes/can't start.

> ⚠️ **Railway + API-key IP restriction:** Railway's outbound IP changes, so you
> generally **cannot** IP-restrict the key there. For live on Railway, rely on a
> **no-withdrawal** key. For tighter control, run live on your own PC with an
> IP-restricted key instead. Alpaca paper has no real money, so this is moot
> until you go live.

> 📋 **Before flipping to real money, follow [GO_LIVE_CHECKLIST.md](GO_LIVE_CHECKLIST.md).**

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

## Sibling strategy: funding-rate carry (delta-neutral, optional)

A second, independent bot ships alongside the spot trend-follower: a
**delta-neutral funding-rate carry** (long spot + short perp on the same coin to
harvest funding). It is **USA-legal** via CFTC-regulated perps (e.g. Kraken
Futures), runs in its **own process**, and never touches the Donchian bot. It is
**SIM (paper) by default** and reuses the same two-key tripwire.

```bash
# paper (live funding/prices, no orders) — safe to run now:
python -m src.carry.main

# research backtest on real funding history (never trades):
python -m src.carry.backtester --assets BTC,ETH,SOL
```

Config lives in the `carry:` block of `config/trading_config.yaml`; keys/overrides
are in `.env` (`CARRY_*`, `KRAKEN_*`, `KRAKENFUTURES_*`). Going live needs
`CARRY_ENABLED=true` **and** `PAPER_TRADING=false` **and**
`LIVE_TRADING_ENABLED=true` **and** `carry.execution.mode: live`. Full design,
risks, and capital model: **[docs/CARRY_ARBITRAGE.md](docs/CARRY_ARBITRAGE.md)**.

> On Railway, run it as a **second** single-replica worker (separate service,
> start command `python -m src.carry.main`). Tests: `pip install -r
> requirements-dev.txt && pytest -q`.

## Sibling strategy: ETF cross-sectional momentum (optional)

A third, independent bot **reuses this repo's validated engine** — the Donchian
trend filter (`src/strategy.py`) + top-K momentum rotation
(`src/momentum_allocator.py`) — pointed at a **US ETF universe** via Alpaca
(stocks/bonds/gold/commodities). Long-only, commission-free, USA-legal, and it
**diversifies the crypto book**. Its own process, SIM by default.

```bash
python -m src.etf.main                                   # paper (live prices, no orders)
python -m src.etf.backtester --universe SPY,QQQ,TLT,GLD  # research backtest
```

Config: the `etf:` block of `config/trading_config.yaml` (overrides via `ETF_*`).
**Live US equities** run through the official `alpaca-py` adapter — `pip install
-r requirements-etf.txt`, then `ETF_EXECUTION_MODE=live` places real **paper**
orders on Alpaca (real money also needs the two-key tripwire + `ALPACA_PAPER=false`).
Full design + the live tiers: **[docs/ETF_MOMENTUM.md](docs/ETF_MOMENTUM.md)**.

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
`trades`, `decisions`, `state` (the carry/ETF bots use their own tables in the
same file).

---

## Development & PR workflow

Run the test suite (offline; no keys or network needed):
```bash
pip install -r requirements-dev.txt      # pytest
pip install -r requirements-etf.txt      # only if touching the ETF bot (alpaca-py)
pytest -q
```

Changes go through a pull request rather than committing to `main` directly:
```bash
git checkout -b my-change
# ...edit, then make sure pytest -q is green...
git commit -am "Describe the change"
git push -u origin my-change
gh pr create --base main --fill          # opens the PR (prints its URL)
gh pr merge --squash --delete-branch     # merge once reviewed / CI-green
```
All bots default to **paper/sim**; real orders require the two-key tripwire
(`PAPER_TRADING=false` + `LIVE_TRADING_ENABLED=true`) **plus** each bot's own
`*_ENABLED` + live execution mode. See **[docs/DEPLOY.md](docs/DEPLOY.md)** for
running them together via the `RUN_BOTS` supervisor.
