# 🪙 Bitcoin Ensemble Trading System

An autonomous, **conservative** Bitcoin (BTC/USDT) trading bot that only acts
when an ensemble of **31 independent prediction paths** strongly agrees. It runs
as a single process, is designed to deploy on **Railway** in well under 2 hours,
and **defaults to PAPER TRADING** (no real money) until you explicitly turn that
off.

> ⚠️ **Risk warning.** Trading cryptocurrency can lose you money — potentially
> all of it. This software is provided for educational purposes with no
> guarantee of profit. **Start in paper mode. Use the Binance testnet. Never
> trade money you cannot afford to lose.** You are solely responsible for any
> live trades.

---

## How it works (plain English)

1. Every 5 minutes the bot pulls fresh BTC/USDT candles from Binance.
2. **28 fast technical models** each vote LONG, SHORT, or stay FLAT.
3. If those 28 are *almost* in agreement (a borderline 26–27), it asks
   **3 Claude AI "experts"** to break the tie — that's the only time it calls
   the AI, so it stays cheap.
4. A trade only opens when **≥ 28 of the 31 paths agree**. Anything below 26 →
   it does nothing.
5. Position size uses **fractional Kelly** with conservative caps (≤ 1% risk per
   trade by default).
6. **Safety rails** stop trading after daily/weekly loss limits, a string of
   losses, or during a cooldown — automatically.
7. Once a day it writes a short plain-English summary to the logs.

Everything is logged with its reasoning, and all state lives in a small SQLite
file so it survives restarts and redeploys.

---

## Project layout

```
.
├── README.md
├── requirements.txt
├── Dockerfile
├── .dockerignore
├── .env.example
├── config/
│   └── trading_config.yaml      # all tunable settings (safe defaults)
└── src/
    ├── config.py                # loads YAML + env overrides
    ├── data_pipeline.py         # Binance data + indicators (WS + polling)
    ├── ensemble_engine.py       # the 31 prediction paths
    ├── risk_manager.py          # Kelly sizing + safety rails + SQLite state
    ├── claude_orchestrator.py   # Claude system prompt + helpers
    └── main_loop.py             # the autonomous 5-minute heartbeat
```

---

## What you need

- A **Binance account**. For paper trading, create **Futures Testnet** keys at
  <https://testnet.binancefuture.com/> (free, fake money).
- An **Anthropic API key** from <https://console.anthropic.com/> (optional — if
  omitted, borderline cases just resolve to "stay flat", the safe default).
- A **Railway account** at <https://railway.app/> (for cloud deployment).

---

## Run it locally (5 minutes)

```bash
# 1. Get the code into a folder, then create a virtual environment
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Create your settings file and edit it
cp .env.example .env
#   -> open .env and paste your keys. LEAVE PAPER_TRADING=true.

# 4. Run the bot
python -m src.main_loop
```

You'll see it backfill candles, then print a decision roughly every 5 minutes.
Press **Ctrl+C** to stop cleanly.

The only thing a basic user ever needs to set is the values in `.env`. Deeper
tuning lives in `config/trading_config.yaml`, which already has safe defaults.

---

## Deploy on Railway (the easy path)

1. Push this folder to a **GitHub repository** (private is fine).
2. Go to <https://railway.app/> → **New Project** → **Deploy from GitHub repo**
   and pick your repo. Railway detects the `Dockerfile` and builds it.
3. Open the service → **Variables** tab → add these (click "New Variable" for
   each), matching `.env.example`:

   | Variable             | Value                                   |
   | -------------------- | --------------------------------------- |
   | `PAPER_TRADING`      | `true`  *(keep this until you trust it)* |
   | `BINANCE_TESTNET`    | `true`                                   |
   | `BINANCE_API_KEY`    | *your Binance **testnet** API key*       |
   | `BINANCE_API_SECRET` | *your Binance **testnet** API secret*    |
   | `ANTHROPIC_API_KEY`  | *your Anthropic key* (optional)          |
   | `CLAUDE_MODEL`       | `claude-haiku-4-5` (optional)            |

4. Railway redeploys automatically. Open the **Deploy logs / Logs** tab and
   watch it run — you'll see backfill, then decisions every 5 minutes, and a
   daily summary.

That's it. The bot runs continuously. On every redeploy Railway sends a clean
shutdown signal and the SQLite state file keeps your history.

> 💡 **Persisting state across redeploys (optional but recommended):** add a
> Railway **Volume** mounted at e.g. `/data`, then set `DB_PATH=/data/trading_state.db`
> in Variables so your trade history isn't reset on each deploy.

---

## Going live (only when you're ready)

You should run in paper mode for a good while first and review the logs. When —
and only when — you fully understand and trust the behaviour:

1. Replace the testnet keys with your **real** Binance API keys.
2. Set `BINANCE_TESTNET=false`.
3. Set `PAPER_TRADING=false`.
4. Start small. Lower `STARTING_CAPITAL_USD` / position caps if you want.

Going live places **real orders with real money**. Re-read the risk warning
above. The conservative defaults (1% risk per trade, 0.25 Kelly, 3% daily loss
limit) are there to protect you — don't loosen them until you know why.

---

## Tuning (advanced, optional)

Open `config/trading_config.yaml`. The most relevant knobs:

- `ensemble.trade_threshold` (default 28) — how many of 31 must agree to trade.
- `risk.max_risk_per_trade` (default 0.01) — hard cap on risk per trade.
- `risk.kelly_fraction` (default 0.25) — how aggressive position sizing is.
- `safety.daily_loss_limit_pct` / `weekly_loss_limit_pct` — auto-stop levels.
- `safety.cooldown_minutes` — pause after each closed trade.

Any of these can also be set as environment variables (see `.env.example`),
which always override the YAML.

---

## FAQ

**Does it need the Claude API?** No. Without `ANTHROPIC_API_KEY`, borderline
ties simply resolve to "stay flat", which is the safe choice. You just lose the
tie-break nuance and the daily summary.

**How often does it call Claude?** Only when the 28 technical models land in the
marginal 26–27 zone — typically a small fraction of candles. The default model
is the inexpensive `claude-haiku-4-5`.

**Will it trade a lot?** No — by design it stays flat unless agreement is
extremely high. Long quiet stretches are normal and intended.

**Where are my trades recorded?** In the SQLite file at `DB_PATH`
(`trading_state.db` by default): tables `trades`, `decisions`, and `state`.
