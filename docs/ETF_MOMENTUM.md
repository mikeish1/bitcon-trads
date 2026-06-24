# Integration Architecture — ETF Cross-Sectional Momentum

> Status: **paper-first / `sim` by default.** A sibling bot that **reuses the
> crypto engine almost verbatim**, pointed at a US ETF universe via Alpaca.
> Long-only, USA-legal, commission-free. Real orders need the two-key tripwire.

## 0. What it is
Hold the **K strongest** ETFs that are currently in an **active Donchian trend**,
ranked by N-day momentum, rebalancing at most every `rebalance_days`. This is
classic **dual-momentum / managed-futures** applied across asset classes
(equities, intl, bonds, gold, commodities, real estate) — a return stream that
**diversifies the crypto book** (bonds/gold trend up when stocks fall).

It is **not** a money printer: it aims for B&H-like upside with lower drawdown and
crisis diversification — the same honest framing as the crypto trend-follower.

## 1. Maximal reuse (the point of this module)
| Concern | Reused component | Where |
|---|---|---|
| Trend filter (eligibility) | `DonchianStrategy.active_state` | [src/strategy.py:213](../src/strategy.py) |
| Top-K cross-sectional selection + rotation clock + hysteresis | `MomentumRotation` | [src/momentum_allocator.py](../src/momentum_allocator.py) |
| Indicators (incl. `atr`) | `DataPipeline.add_indicators` | [src/data_pipeline.py:72](../src/data_pipeline.py) |

The new code is just the glue: a config block, a thin data provider, an
equal-weight long-only risk/ledger, a sim/live executor, a rebalance loop, and a
backtester. The **selection logic is the crypto allocator, unchanged**
([src/etf/selector.py](../src/etf/selector.py)).

## 2. Layout (`src/etf/`)
```
config_etf.py     # parse cfg["etf"] + ETF_* env; resolve the sim/paper/live tier
selector.py       # EtfMomentumSelector = DonchianStrategy.active_state + MomentumRotation (PURE)
brokers/
  base.py         # EtfBroker ABC — the ONLY venue dependency (bars/account/orders)
  alpaca_broker.py# AlpacaBroker — real US equities/ETFs via alpaca-py (paper or live)
  ccxt_broker.py  # CcxtBroker — ccxt data/fallback for non-Alpaca venues
  __init__.py     # build_broker(cfg) factory (picks by etf.venue)
data.py           # EtfData: broker.daily_bars + DataPipeline.add_indicators (venue-agnostic)
risk.py           # EtfRiskManager: equal-weight 1/K sizing, exposure cap, paper ledger (SQLite)
executor.py       # EtfExecutor: sim fills internal; live delegates to the broker
backtester.py     # PURE walk-forward + history CLI
main.py           # EtfBot rebalance loop (mirrors src/main_loop.py)
```

## 3. Flow (each rebalance)
```mermaid
flowchart TB
  BARS[daily ETF bars (ccxt)] --> IND[add_indicators] --> FBS[frames per symbol]
  FBS --> SEL[EtfMomentumSelector\nactive Donchian trend ∩ top-K momentum]
  SEL -->|target / enter / exit| RISK[EtfRiskManager\nequal-weight 1/K, exposure cap]
  RISK --> EXEC[EtfExecutor sim|live]
  EXEC --> LEDGER[(SQLite etf_*)]
  LEDGER --> NOTIF[Notifier 📈ETF]
```

## 4. Capital & risk
- **Dedicated sleeve** (`etf.capital.sleeve_usd`), siloed from crypto equity.
- **Equal-weight** target `1/top_k` per name, bounded by `max_total_exposure_pct`
  and available cash; `min_notional_usd` blocks dust.
- **Exits are rotation-driven** (v1): a symbol that loses its Donchian trend drops
  out of candidates and is sold at the next rebalance. (A daily chandelier stop
  between rebalances is a documented v2 add — see §7.)
- **Two-key tripwire** for live, identical to the spot bot.

## 5. Backtesting
`run_backtest` is **pure** and unit-tested offline (synthetic trending panels): it
walks the daily grid, rebalances via the selector, equal-weights the target, and
marks to market — reporting total return, max drawdown, annualised Sharpe, and
deployment. CLI for real bars:
```bash
python -m src.etf.backtester --universe SPY,QQQ,TLT,GLD,DBC
```
> Because indicators are causal (backward-looking rolling), computing them on the
> full series then slicing introduces **no lookahead** — verified by construction.

## 6. Live equities via Alpaca (`AlpacaBroker`)
Real US equities/ETF data + orders go through **`alpaca-py`** behind the
`EtfBroker` interface. Everything else (selector, risk, backtester, loop) is
venue-agnostic and unchanged.

```bash
pip install -r requirements-etf.txt      # alpaca-py (only the ETF live path needs it)
```
Uses the **same Alpaca keys** as the spot bot (`ALPACA_API_KEY` /
`ALPACA_API_SECRET`) — note **data also needs keys**, so even SIM on Alpaca
requires them (free paper keys work). Execution tiers (mirror the crypto bot):

| `ETF_EXECUTION_MODE` | `ALPACA_PAPER` | tripwire | Result |
|---|---|---|---|
| `sim` (default) | — | — | internal paper ledger, **no orders** |
| `live` + `ETF_ENABLED=true` | `true` | not needed | **real PAPER orders** on Alpaca (no money) |
| `live` + `ETF_ENABLED=true` | `false` | `PAPER_TRADING=false` + `LIVE_TRADING_ENABLED=true` | **real-money** orders |

A real-money request *without* the two-key tripwire safely falls back to `sim`.
Details: commission-free, **fractional/notional** market orders (ideal for 1/K
sizing), free **IEX** data feed (`etf.alpaca_feed`, or `sip` if subscribed), and a
**market-hours guard** — live orders are skipped when the equities market is
closed (fail-closed if the clock can't be read). Non-Alpaca venues fall back to
`CcxtBroker` (data/crypto-style; ccxt equity support is limited).

## 7. Recommended follow-ups
1. Daily chandelier stop between rebalances (reuse the crypto risk trail) for
   tighter downside control.
2. ~~Absolute-momentum / cash filter (Antonacci dual momentum)~~ — **done** as a
   selector mode: set `etf.selection.mode: dual_momentum` (or `ETF_SELECTION_MODE`).
   Holds the strongest offensive ETF only while its absolute momentum beats a T-bill
   hurdle, else rotates to the strongest of a defensive sleeve (bonds/gold/T-bills).
   Off by default; validate before enabling. See
   [docs/equities_replatform/](equities_replatform/) (strategy survey, ADRs, bias audit).
3. Vol-targeted weights instead of pure equal-weight.
4. ~~A live equities adapter via `alpaca-py`~~ — **done** (`AlpacaBroker`, §6).

> Bars are **split+dividend adjusted** (`etf.alpaca_adjustment: all`) and pass a
> data-quality check (`src/etf/data_quality.py`) before indicators. A PDT same-day
> guard (`etf.pdt_guard`) and split-aware reconcile keep the live path safe.
