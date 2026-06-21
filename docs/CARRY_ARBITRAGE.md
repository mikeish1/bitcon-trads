# Integration Architecture — Delta-Neutral Funding-Rate Carry

> Status: **paper-first / `sim` by default.** Real orders require the three-key
> tripwire (see §7). This strategy is a **sibling** of the Donchian spot bot — it
> reuses the config, SQLite-state, Notifier and safety conventions but runs in its
> own process and never touches the validated trend-follower.

## 0. What this strategy is (plain English)

Hold **long spot** and an equal-notional **short perpetual** of the same coin at
the same time. Price can do whatever it wants — a gain on one leg is a loss on the
other, so your directional exposure is ~zero (**delta-neutral**). What you keep is
the **funding payment**: on a perpetual, when funding is positive (the normal
crypto regime) **shorts are paid by longs** every funding interval. You are short
the perp, so you **collect** that funding. The trade earns a yield, not a
direction.

- Typical realistic net yield: **~8–15% APY** in good conditions, low single
  digits when funding cools (Q2 2026 has been compressed). Drawdowns are small but
  **not zero** — see the risks.
- It is **USA-legal** via CFTC-regulated perpetuals (e.g. Kraken Futures /
  Bitnomial: BTC, ETH, SOL, XRP), reachable through the same `ccxt` this repo
  already depends on. No geofence games, unlike Polymarket.

### This is not free money — the honest risks
1. **Funding flips negative** → you start *paying* instead of collecting. Handled
   by a three-zone unwind rule with a **tolerance band** (`min_hold_apr` /
   `unwind_apr` / `flip_confirm_reads`) so brief, mildly-negative funding is
   tolerated rather than churning a costly round trip.
2. **Fees dominate short holds.** A round trip is 4 taker fills (open+close × 2
   legs). The signal gates on **net** APR after amortizing fees over an assumed
   hold horizon — short holds can be net-negative even with positive funding.
3. **Execution/leg risk.** If one leg fills and the other doesn't you are briefly
   naked-directional. OPEN sequences legs and **rolls back** a half-open pair;
   UNWIND is **resumable** — each leg's close is persisted, so a failure or
   restart mid-unwind finishes the remaining leg next poll without ever
   re-hitting a closed one.
4. **Margin/liquidation on the short leg.** Run low leverage; monitor margin ratio.
5. **Counterparty / basis blowups.** Daily loss limit + staleness breaker + kill
   switch.

## 1. Why it fits THIS codebase (and Polymarket didn't)

| | Polymarket latency-arb | **Funding carry (this)** |
|---|---|---|
| Cadence | sub-second HFT | **8h funding cycle → 15-min polling is ample** |
| Runtime | needed async/WS rewrite | **fits the existing synchronous loop model** |
| Venue access (US) | geofenced | **CFTC-regulated, legal** |
| New deps | web3, clob client, websockets | **none — `ccxt` already present** |
| Backtestable | very hard | **yes — `fetchFundingRateHistory` is native** |

It reuses your patterns verbatim: the `cfg` dict + env overrides
([config.py:47](../src/config.py)), the two-key tripwire
([config.py:51](../src/config.py)), the SQLite `state`/positions model
([risk_manager.py:81](../src/risk_manager.py)), the three execution tiers
([executor.py:35](../src/executor.py)), and the fail-safe `Notifier`
([notifications.py:29](../src/notifications.py)).

## 2. Module layout (new `src/carry/` package — isolated)

```
src/carry/
  config_carry.py   # parse cfg["carry"] + env; build spot+perp ccxt clients; tripwire
  types.py          # FundingQuote, CarryDecision, Fill, PairFill, CarryParams
  data.py           # CarryData: funding (history-based + validated), prices, basis, margin
  signal.py         # pure logic: annualize, net-carry, OPEN/HOLD/UNWIND/SKIP
  risk.py           # CarryRiskManager: paired positions, sizing, funding accrual, limits (SQLite)
  executor.py       # CarryExecutor: paired spot-buy + perp-short, sim|live, leg rollback
  main.py           # CarryBot sync loop (mirrors src/main_loop.py)
  backtester.py     # pure funding-series sim + ccxt-history CLI (research only)
tests/test_carry_*.py
docs/CARRY_ARBITRAGE.md
```

## 3. Data flow

```mermaid
flowchart TB
  PERP[perp venue (krakenfutures)\nfunding history + mark] --> DATA[CarryData]
  SPOT[spot venue (kraken)\nlast price + balances] --> DATA
  DATA -->|FundingQuote: net APR, basis, staleness| SIG[CarrySignal (pure)]
  SIG -->|OPEN/HOLD/UNWIND/SKIP| RISK[CarryRiskManager\nsleeve/per-asset caps, daily loss, margin]
  RISK -->|sized notional| EXEC[CarryExecutor]
  EXEC -->|sim fills OR live: spot buy + perp short| LEDGER[(SQLite carry_*)]
  EXEC -. rollback half-open pair .-> EXEC
  RISK -->|accrue funding each poll| LEDGER
  LEDGER --> NOTIF[Notifier (throttled)]
```

## 4. The mechanical signal (no LLM, fully testable)

```
funding_apr   = funding_rate_per_interval × (8760 / interval_hours)   # e.g. ×1095 @8h
fee_drag_apr  = roundtrip_cost_frac × (365 / expected_hold_days)
                roundtrip_cost_frac = 4 × (taker_fee + slippage)      # open+close, 2 legs
net_apr       = funding_apr_smoothed − fee_drag_apr
```
Decision (per asset):
- `staleness > max` → **SKIP** (loop trips the staleness breaker)
- not held: `net_apr ≥ min_entry_apr` AND `|basis_bps| ≤ max_basis_bps` → **OPEN**, else **SKIP**
- held (three-zone hysteresis, anti-churn):
  - `gross ≥ min_hold_apr` → **HOLD**, reset the counter (comfortable)
  - `unwind_apr ≤ gross < min_hold_apr` → **HOLD**, *tolerance band* — neither count
    nor reset (`unwind_apr` may be slightly negative)
  - `gross < unwind_apr` for `flip_confirm_reads` consecutive polls → **UNWIND**

Funding is smoothed over `funding_lookback` reads from `fetchFundingRateHistory`
(native), avoiding the emulated-`fetchFundingRate` quirk.

## 5. Capital model (honest about cross-venue collateral)

A cross-venue carry needs capital on **both** venues: ~`N` to buy spot **and**
`N / target_leverage` as futures margin. So capital used per pair ≈
`N × (1 + 1/leverage)`. The dedicated **sleeve** (`carry.capital.sleeve_usd`) is
the *total* capital across both venues and is **siloed** from the Alpaca/Binance
equity the existing `RiskManager` tracks. Sizing:
`notional = clamp(remaining_sleeve/(1+1/lev), min_notional, per_asset_cap)`.

## 6. Risk controls
- Per-pair: `per_asset_cap_usd`, `min_notional_usd`, `max_basis_bps` (entry slip guard).
- Portfolio: `sleeve_usd` cap, `max_concurrent` (= len(assets)), `daily_loss_limit_usd`.
- Funding-flip unwind: `min_hold_apr` + `flip_confirm_reads` (anti-churn).
- Margin: `target_leverage`/`max_leverage`, `margin_alert_ratio`.
- Leg risk: OPEN sequences spot→perp and **rolls back** the filled leg if the
  second fails; UNWIND is **resumable** (per-leg state persisted, never re-hits a
  closed leg on retry/restart).
- Breakers: feed `max_feed_staleness_seconds`; kill switch (env/state flag); SIGTERM
  drains cleanly (positions are delta-neutral and persist in SQLite — not force-closed).
- **Three-key live tripwire:** `CARRY_ENABLED=true` AND `PAPER_TRADING=false` AND
  `LIVE_TRADING_ENABLED=true`. Anything less = `sim`.

## 7. Backtesting / validation
1. **Pure funding-series sim** (`backtester.py`, unit-tested offline): feed a
   funding-rate series, apply the entry/hold rule, accrue funding − fees, report
   APR, max drawdown, % time deployed, # funding flips.
2. **Live history CLI**: `python -m src.carry.backtester --assets BTC,ETH` pulls
   real `fetchFundingRateHistory` and runs (1) on it.
3. **Live `sim` mode** (primary): the full loop against live funding/prices,
   simulating fills and accruing funding, **sending nothing** — paper-first, the
   same protocol as the Donchian rollout. Run for weeks before flipping the keys.

## 8. Monitoring
Reuse `Notifier` (throttled): startup banner, open/unwind, daily PnL + funding
summary, breaker/kill alerts, hourly heartbeat. Persist `carry_positions`,
`carry_funding`, and state in the **same** SQLite DB (new tables, WAL).

## 9. Interactions with the existing system
Separate process, separate venues (Kraken vs Alpaca/Binance.US), separate capital
sleeve, separate tables. Shared DB file (WAL, arb writes on the cold path) and
shared Telegram chat (carry alerts prefixed `🪢CARRY`). If the carry bot dies the
spot bot is unaffected, and vice-versa. On Railway it's a **second** single-replica
worker.
