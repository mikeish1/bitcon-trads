# Sleeve overlay + disciplined universe expansion

Two thin, opt-in, **default-off** additions that turn the three independent bots
(Donchian / Carry / ETF) into a more diversified, adaptive system — without a
monolithic framework. The three bots keep running exactly as before.

## 1. Thin sleeve allocator — `src/portfolio_sleeve_allocator.py`

Computes target weights across the three sleeves from each sleeve's recent equity
curve. It is **informational by default**: the spot loop logs the weights once a
day and moves no capital. A future meta-runner can read them to notionally
re-allocate. The allocator never duplicates any bot's risk/execution/data logic.

**Data contract** — `compute_weights(performance, regime_state=None, mode=None, prev_weights=None)`:
```python
performance = {
  "donchian": {"ret": 0.12, "vol": 0.025, "sharpe": 1.4},  # summary metrics, or…
  "carry":    {"equity": <pd.Series of daily equity>},      # …a raw curve
  "etf":      {"ret": 0.03, "vol": 0.010},
}
```
`metrics_from_equity(curve, lookback)` converts a curve → `{ret, vol, sharpe, n}`.
`aggregate_sleeve_equity(db_path, cfg)` / `build_sleeve_performance(db_path, cfg)`
read each sleeve's recent curve READ-ONLY from the shared SQLite (spot's real
`equity_history`; carry's cumulative funding; ETF's cumulative realized PnL).

**Modes** (`portfolio.sleeves.allocator_mode`):
- `risk_parity` — weight ∝ 1 / recent daily vol (diversifies; cuts combined vol).
- `momentum_of_strategies` — tilt toward stronger recent Sharpe/return, blended
  with equal weight by `momentum_tilt` (anti-whipsaw).

Optional `regime_state={"risk_on": bool}` modestly boosts the Donchian weight when
crypto is risk-on. Weights are clamped to `[min_weight, max_weight]`, summed to 1
(box-constrained simplex projection), and only changed when drift exceeds
`rebalance_threshold` (turnover control). Missing sleeves degrade gracefully.

**Config** — `portfolio.sleeves` (enabled, members, allocator_mode, lookback_days,
min/max_weight, rebalance_threshold, momentum_metric/tilt, regime_boost_factor).
**Enable the daily log**: `portfolio.sleeves.enabled: true` (or `SLEEVES_ENABLED=1`).

**Demo**: `python -m src.portfolio_sleeve_research` — blends three real-ish sleeve
curves three ways and reports combined vol / CAGR / max-DD / Calmar. In testing
`risk_parity` cut combined volatility ~35% and max drawdown ~7pts vs a static
equal-weight blend.

## 2. Disciplined universe expansion — `src/universe.py`

A candidate coin is **approved only if it clears every gate**:
1. **Liquidity** — rolling-avg daily dollar volume ≥ an absolute floor AND ≥
   `min_relative_to_median_pct` of the median existing-member ADV (the relative
   floor auto-calibrates to the venue).
2. **Correlation** — max pairwise daily-return correlation with any member ≤
   `max_pairwise_correlation`.
3. **Portfolio benefit** — added to an equal-weight Donchian portfolio it must
   reduce realized vol OR improve Calmar, without blowing up turnover.

`validate_universe_addition(symbol, df, members, cfg)` returns a structured verdict.
**Config**: `universe.expansion` (candidates, approved_expanded_universe) +
`liquidity_filters` (thresholds).

**Run**: `python -m src.universe_expansion_research --candidates AVAX,LINK,LTC,DOT`
(thresholds overridable with `--min-volume/--rel-pct/--max-corr`).

> Calibration note: the absolute liquidity floor defaults to a liquid-global-venue
> scale. Binance.US alt volume is far thinner (even BTC ≈ $3.5M/day in its data),
> so for a Binance.US-only book lower `min_avg_daily_volume_usdt` to the venue's
> scale (the report prints the member ADV reference) or lean on the relative floor.

**Validation result (2026-06, 4y daily):** of AVAX/LINK/LTC/DOT, only **LTC**
cleared correlation (0.85) + diversification (portfolio vol −0.8%, turnover
+17.6%). AVAX failed on no diversification benefit, LINK on correlation (0.93 vs
ETH), DOT on liquidity. LTC is staged in `approved_expanded_universe` but NOT added
to live `bases` pending venue liquidity calibration.
