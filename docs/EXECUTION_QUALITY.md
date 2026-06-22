# Execution quality: limit orders · slippage tracking · cost preference

Three configuration-driven, backward-compatible improvements that shrink the gap
between backtest and live net returns. Market orders remain the reliable fallback;
paper trading and backtests are unaffected.

## 1. Limit orders with market fallback (entries) — `src/executor.py`

`SpotExecutor.open_buy(symbol, quote, price_hint, intended_price)` replaces the
direct market buy on entries (both `first_come` and `momentum_rotation`):
- rests a **limit** at `price_hint * (1 + entry_limit_offset_bps/1e4)` (optionally
  `post_only` for maker fees);
- polls to `limit_order_timeout_sec`, then **cancels and market-buys the unfilled
  remainder** so a genuine breakout is never missed (partial fills combined);
- in paper/sim it models the limit filling at the limit price (price improvement vs
  the market-slippage path — optimistic on fills, documented);
- exits keep their existing market/stop-limit logic untouched.

## 2. Slippage instrumentation — `src/slippage.py`

Every fill (buy/sell, paper/live, limit/market) is recorded to a dedicated `fills`
table with intended vs actual price, **slippage in bps and USD** (adverse = positive),
order type, fee, and mode. Toggle with `slippage_logging_enabled`; warns past
`max_slippage_tolerance_bps`. Query aggregates:
```
python -m src.slippage            # whole history: avg/max-adverse/best bps, by symbol & order type
python -m src.slippage --days 7   # last 7 days
```
`slippage_summary(db_path, since_iso)` returns the same data programmatically for
comparison against the backtest's assumed `paper_slippage_pct`.

### Paper fill realism + chase guard
Live fills are always real. Paper/sim fidelity is configurable:
- `paper_limit_fill_model: optimistic` (whole order fills at the limit; best case) or
  `realistic` (only `paper_limit_fill_ratio` fills at the limit, the rest market-fills
  — so paper reflects that passive limits don't always fully fill).
- `max_entry_chase_bps` (sim **and** live): if the market fallback would execute more
  than this far above the signal price, the entry is **abandoned** instead of chasing
  a breakout that already ran away — modeling the real opportunity-miss cost. `0` = off.

Demonstrated (3 entries): optimistic → +0.00 bps; realistic(0.7) → +3.50 bps blended
(30% pays the market remainder); realistic + 1 bps chase cap → remainder abandoned,
back to the limit-only portion.

## 3. Cost-aware pair preference — `src/cost_model.py`

`effective_cost_bps = 2 × taker_fee_bps + spread_proxy_factor × spread_proxy`, where
the spread proxy is the median daily `(high-low)/close` (a relative liquidity proxy).
`execution.cost_preference_mode`:
- `off` — no effect (default).
- `soft` — cheaper coins are favoured as a tie-breaker (first-come trades them
  first; rotation applies a small `cost_penalty_weight` penalty to scores).
- `strict` — coins above `max_effective_cost_bps` are dropped each cycle.

It never overrides regime/active-trend/risk gates — only orders or trims what those
already allow.

**Real fees + live spreads.** With `cost_use_live_quotes: true` the score uses the
actual venue **taker tier** (`exchange.market(sym)['taker']`, overridable per-base via
`fee_overrides`) and the **live bid/ask spread** (`exchange.fetch_ticker`), giving an
absolute round-trip cost in bps. When a quote is unavailable (offline/backtest) it
falls back per-symbol to the daily-range proxy. Example: a 0.08% taker tier + a 4 bps
live spread → **20 bps** real, vs **26 bps** from the config-fee + proxy path.

## Recommended YAML (`execution`)
```yaml
execution:
  taker_fee_pct: 0.001
  maker_fee_pct: 0.001
  paper_slippage_pct: 0.0007
  use_limit_orders_on_entry: true
  entry_limit_offset_bps: 0        # <0 = passive/maker-seeking (more improvement, more miss-risk)
  limit_order_timeout_sec: 60
  limit_poll_interval_sec: 3
  post_only: false                 # true on venues that support it -> maker fees
  slippage_logging_enabled: true
  max_slippage_tolerance_bps: 50
  cost_preference_mode: "off"      # soft | strict to enable
  max_effective_cost_bps: 60
  cost_penalty_weight: 1.0
  spread_proxy_window: 20
  spread_proxy_factor: 0.1
```
Fast env flips: `USE_LIMIT_ORDERS`, `SLIPPAGE_LOGGING`, `COST_PREFERENCE_MODE`.

## Demonstrated improvement (paper, identical entries)
| Entry type | avg slippage | total slippage | fees |
|---|--:|--:|--:|
| market | **+7.00 bps** | $2.10 | $3.00 (taker) |
| limit (post-only) | **+0.00 bps** | $0.00 | $1.50 (maker) |

→ ~7 bps/entry slippage saved + half the fees. Live passive limits sometimes miss
(→ market fallback, unit-tested); use the backtest slippage knob for the pessimistic
case.

## Run
```
pytest tests/test_executor_limit.py tests/test_slippage.py tests/test_cost_model.py
USE_LIMIT_ORDERS=1 COST_PREFERENCE_MODE=soft python -m src.main_loop   # paper
python -m src.slippage                                                  # monitor fills
```
