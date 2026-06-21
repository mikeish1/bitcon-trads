# Deploying the bots (Railway)

Three independent bots ship in this repo:

| Bot | Module | What it trades |
|---|---|---|
| `spot` | `src.main_loop` | Donchian multi-crypto trend-follower (the validated one) |
| `carry` | `src.carry.main` | Delta-neutral funding-rate carry (Kraken perps) |
| `etf` | `src.etf.main` | US ETF cross-sectional momentum (Alpaca) |

## Default model: one container, a supervisor, `RUN_BOTS`

The Dockerfile runs **`src/run_all.py`** as PID 1. It launches the bots named in
the `RUN_BOTS` env var, forwards `SIGTERM` to them on shutdown, and restarts a
crashed child with backoff (one bot dying never takes the others down).

```
RUN_BOTS=spot              # default - just the trend-follower (== old behaviour)
RUN_BOTS=spot,carry,etf    # run all three together in one container
```

### ⚠️ Keep `numReplicas: 1`
`railway.json` already pins this. The supervisor runs each bot **exactly once**; a
second replica would duplicate every bot and place **duplicate orders**. Never
scale this service past 1.

### Per-bot enablement & the master tripwire
- **Real-money master switch (shared by all bots):** `PAPER_TRADING=false` **and**
  `LIVE_TRADING_ENABLED=true`. With either unset, *no* bot uses real money.
- **Per-bot opt-in** (still required on top of the master switch to go live):
  - carry: `CARRY_ENABLED=true` + `CARRY_EXECUTION_MODE=live`
  - etf: `ETF_ENABLED=true` + `ETF_EXECUTION_MODE=live`
    (Alpaca `ALPACA_PAPER=true` = real **paper** orders without the master switch.)
- So a bot trades real money only when **both** the master tripwire **and** its own
  enable/mode are set. Running a bot in its default mode is paper/sim.

### Required variables by bot
| Bot | Keys / vars |
|---|---|
| spot | `EXCHANGE_ID`, `ALPACA_API_KEY/SECRET` (or `BINANCE_API_KEY/SECRET`), `SYMBOLS` |
| carry | `KRAKEN_API_KEY/SECRET`, `KRAKENFUTURES_API_KEY/SECRET`, `CARRY_*` |
| etf | `ALPACA_API_KEY/SECRET`, `ALPACA_PAPER`, `ETF_*` (needs `requirements-etf.txt`, already in the image) |

All three can share one Railway service's Variables; each bot reads only its own.

### State / volume
All bots persist to the SQLite file at `DB_PATH` (separate tables per bot, WAL +
`busy_timeout` so concurrent writers don't collide). Mount a volume and set
`DB_PATH=/data/trading_state.db` so state survives redeploys (see the README
"Persist the database" section).

### Steps
1. Push to GitHub; Railway → New Project → Deploy from repo (reads `railway.json`).
2. Service → Variables: set `RUN_BOTS=spot,carry,etf` + the keys above. Start in paper.
3. Confirm **Replicas = 1**. Logs show one `SUPERVISOR` line per bot start, then
   each bot's own banner.

## Alternative: three separate Railway services (max isolation)
Prefer hard isolation (independent restarts, CPU, and logs)? Create **three
services from the same repo**, each with the **same `railway.json`/Dockerfile**
but a **Start Command override** in the dashboard:

```
Service "spot"   -> python -m src.main_loop
Service "carry"  -> python -m src.carry.main
Service "etf"    -> python -m src.etf.main
```

Each service: `numReplicas: 1`, its own Variables (give each only the keys it
needs), and ideally **separate `DB_PATH` volumes** so their state files don't
share a disk. This is more Railway-idiomatic but you manage three services
instead of one `RUN_BOTS` variable. Functionally equivalent to the supervisor.

## Local
```bash
python -m src.main_loop          # or src.carry.main / src.etf.main individually
RUN_BOTS=spot,carry,etf python -m src.run_all   # all three under the supervisor
```
