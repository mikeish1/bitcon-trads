# Stage 2 — Data & Market-Mechanics: Bias Audit

> How the ETF/equities data path prevents look-ahead, survivorship, and
> data-snooping, and how equity-market mechanics (calendar, corporate actions,
> gaps) are handled. Companion to [plan.md](plan.md) / [risk_register.md](risk_register.md).
>
> **Selected design (Stage 1): B — Dual Momentum** with a strongest-of
> {bonds, gold, T-bills} defensive sleeve.
> **Bias-audit confidence: 0.85** → the Stage-2 human checkpoint is *selective*
> and not triggered (threshold 0.8), but the summary is surfaced for review.

---

## 1. What changed in Stage 2 (code)
| Change | File | Why |
|---|---|---|
| **Bars requested split+dividend adjusted** (`adjustment=ALL`, default; `ETF_ALPACA_ADJUSTMENT` to override) | [alpaca_broker.py](../../src/etf/brokers/alpaca_broker.py) | Alpaca's SDK defaults to RAW; a split prints a phantom −50/−75% gap that fires a false trend exit and corrupts the momentum rank. **R1 fixed.** |
| Config knob `etf.alpaca_adjustment` | [config_etf.py](../../src/etf/config_etf.py) | Make the (correct) default explicit + auditable. |
| **Data-quality validator** (`validate_bars`) wired into `EtfData.frames` | [data_quality.py](../../src/etf/data_quality.py), [data.py](../../src/etf/data.py) | Drop structural corruption (NaN/non-positive/high<low/dupes), flag gaps + OHLC quirks, before indicators. |
| Tests | `tests/test_etf_data_quality.py`, `tests/test_etf_broker.py` | Regression-pin the adjusted-bars request + every quality rule. |

---

## 2. Look-ahead bias — prevented
- **Causal indicators.** `DataPipeline.add_indicators` uses backward-rolling
  windows only (ATR, rolling highs). Computing on the full series then slicing
  introduces no future leakage — true by construction.
- **Confirmed-closed-candle signals.** Signals/rank/sizing-ATR read the last
  *settled* daily bar (`signal_on_closed_candle`, default true; `EtfData.closed_view`
  drops the still-forming session bar while the market is open). Marking and orders
  use the live price. This is what makes live match the close-based backtest.
- **Point-in-time backtest.** `run_backtest` slices `df[df["timestamp"] <= t]` at
  every step and rebalances only on frames up to `t` — no row from the future is
  ever visible to a decision.
- **No parameter peeking.** Walk-forward IS/OOS (Stage 4) fits only on the in-sample
  window; OOS degradation is reported, not hidden.

## 3. Survivorship bias — addressed (with one documented caveat)
- **Universe chosen by economic ROLE, not past returns** — broad, liquid,
  cross-asset ETFs (see §6), not a data-mined "winners" list. No single-name stocks
  (where delisting/survivorship bias is severe).
- **Low delisting risk.** The chosen ETFs are large, long-lived flagship funds; none
  has been delisted. The asset-class *roles* (US/intl/EM equity, Treasuries, gold,
  T-bills) are timeless even if a specific ticker were ever replaced.
- **Caveat (honest):** using *today's* basket is a mild forward peek — we implicitly
  "know" these funds survived and stayed liquid. Mitigations: (a) selection is by
  economic role, not historical performance; (b) for backtests predating an ETF's
  inception we use the **longest available adjusted history and disclose the
  truncation** — we do **not** silently splice index proxies (which would smuggle in
  a different cost/tracking profile). This caveat is carried into Stage 4 reporting.

## 4. Point-in-time / adjusted-data discipline
- Split+dividend **back-adjusted** bars are now used consistently for **both**
  backtest and live signal computation, so there is no relative look-ahead between
  research and production. Back-adjusted absolute levels shift as new dividends
  accrue, but a breakout/momentum signal computed *consistently* on the adjusted
  series is unaffected — live recomputes on the same basis each day.

## 5. Equity-market mechanics
- **Trading calendar.** Alpaca returns trading-day bars only (no synthetic
  weekend/holiday rows), so the backtester steps real session dates. Live uses the
  Alpaca **clock** (`is_market_open`, **fail-closed** if unreadable) — no orders when
  the market is closed, half-days/holidays included.
- **Corporate actions.** Splits/dividends/special distributions are handled on the
  **signal** side by adjusted bars. **Position qty after a split** is *not* yet
  auto-adjusted: `reconcile` only closes a "position gone at broker" case; a split
  silently changes held qty/basis until then. **→ Stage 3 adds split-aware reconcile
  (or an explicit alert)**, never auto-misstating basis. (R8)
- **Trading halts.** Alpaca simply won't fill a halted symbol; the order returns
  unfilled and `_fill_from_order` records **no phantom position** — fail-safe.
- **Data quality.** Bad ticks, dupes, NaN/zero prices dropped; calendar gaps and
  OHLC inconsistencies logged before they reach the signal.

## 6. Universe construction (Dual Momentum B)
Defined here for the data layer; the offensive/defensive *logic* lands in Stage 3.
All are liquid, commission-free and fractionally tradable on Alpaca.

| Sleeve | Symbols | Economic role |
|---|---|---|
| **Offensive** (ranked by abs+rel momentum) | `SPY` (US eq), `EFA` (dev intl), `EEM` (EM eq); optional `QQQ` | Risk-on growth engines; momentum rotates to the strongest. |
| **Defensive** (strongest-of, when offense fails abs-momentum) | `TLT`/`IEF` (Treasuries), `GLD` (gold), `BIL` (T-bills/cash) | Drawdown control; gold/T-bills cover the 2022 "bonds+stocks both fell" case that a bonds-only default misses. |

**Liquidity filter:** restrict to ETFs with deep ADV (these all clear it by orders of
magnitude); reuse the existing liquidity-gate philosophy from the crypto universe
work, calibrated to equity ADV. No illiquid/leveraged/inverse ETFs.

## 7. Carried forward to Stage 4 (not a Stage-2 concern)
- **Gap-at-open** is a *real* residual even with adjusted bars (earnings/macro
  overnight gaps). Adjusting for splits/divs does **not** remove it. The Stage-4
  backtest must model stop/exit fills at the **open**, not the trigger price. (R6)
