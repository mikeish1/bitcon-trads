# Adaptive enhancements (sizing · regime · profit-taking · composite momentum)

Four opt-in extensions to the daily Donchian spot bot. **All default OFF** — live
behaviour is unchanged until you flip the toggle, and no toggle can raise risk
beyond the existing hard caps (`portfolio.*`, `capital_policy`). Tune everything in
`config/trading_config.yaml`; flip fast via env vars.

| Feature | YAML section | Env flip | Code |
|---|---|---|---|
| A. Dynamic sizing + vol targeting | `risk.risk_budget` | `RISK_BUDGET_ENABLED` | `RiskManager.size_for_asset/size_rotation` |
| B. Regime gate | `strategy.regime` | `REGIME_ENABLED`, `REGIME_METHOD` | `src/regime.py` |
| C. Staged profit-taking | `strategy.profit_taking` | `PROFIT_TAKING_ENABLED` | `RiskManager.profit_taking_plan/reduce_position`, `main_loop._manage` |
| D. Composite momentum | `strategy.allocation.momentum_rotation` | `MOMENTUM_SCORING=composite` | `MomentumRotation.score_candidates` |

## A — Dynamic position sizing & volatility targeting
With `risk.risk_budget.enabled: true`, a new trend position is sized so an ATR
stop-out costs ~`risk_per_trade_pct` of equity:
`risk_notional = equity * risk_per_trade_pct / (atr_pct * atr_stop_mult)`. An
optional global scalar (`target_portfolio_vol`, clamped `[vol_scalar_min, vol_scalar_max]`)
nudges total exposure toward a target daily portfolio vol. The hard caps still bind,
so the scalar can only grow size *up to* the per-asset/envelope caps.

The scalar's vol estimate is selectable via `vol_source`:
- `proxy` (default) — mean ATR% across the held book; stateless, reacts instantly.
- `realized` — stdev of daily portfolio-equity returns over `vol_lookback_days`,
  read from a per-day equity snapshot (`RiskManager.record_equity`); falls back to
  the proxy until ≥3 daily snapshots exist.

## B — Higher-timeframe regime gate
`strategy.regime` supersedes the legacy `strategy.btc_regime` when `enabled: true`.
Methods: `ma` (== legacy), `ma_slope`, `vol`, `composite`. Risk-off produces a
`size_factor` (0.0 = block new entries, 0.2 = shrink) and can either flatten
(`risk_off_exit: true`, legacy default) or hold-and-tighten (`tighten_trail_mult`).

## C — Staged profit-taking / ratcheting exits
Builds on the chandelier. As a winner extends (profit measured in ATR *at entry*),
sell tiers (`tiers`), lift the stop to breakeven+buffer (`breakeven_after_tier`,
`breakeven_buffer_atr`), and tighten the runner's trail (`ratchet_trail_mults`). The
final runner can also time-out (`time_stop_days`). **Live-grade fidelity:** a scale-out
places a real partial sell and cancels/re-places the exchange stop for the reduced
qty (no oversell); realized PnL is conserved across scale-outs and the final close,
and a dust remainder exits fully without double-closing. This whole path is covered
end-to-end by `tests/test_main_loop_staged_exit.py`. In SIM/paper-internal mode (no
exchange orders) the same logic runs ledger-only.

## D — Composite momentum allocator
`scoring: composite` ranks rotation candidates by a cross-sectionally normalized
weighted blend of breakout strength, long & short ROC, relative strength vs BTC, and
inverse ATR. `min_momentum_threshold` drops weak names before top-K ranking.
`scoring: simple` (default) keeps the OOS-validated N-day ROC.

## Recommended starting values (paper-test first)
```yaml
risk:
  risk_budget: {enabled: true, risk_per_trade_pct: 0.0075, atr_stop_mult: 2.0,
                target_portfolio_vol: 0.025, vol_scalar_min: 0.5, vol_scalar_max: 2.0,
                vol_source: proxy, vol_lookback_days: 20}   # vol_source: realized once warmed up
strategy:
  regime: {enabled: true, method: composite, ma_period: 100, slope_lookback: 20,
           vol_period: 20, vol_ceiling: 0.05, score_threshold: 0.5,
           risk_off_size_factor: 0.0, risk_off_exit: true}
  profit_taking: {enabled: true, breakeven_after_tier: 1, breakeven_buffer_atr: 0.5,
                  ratchet_trail_mults: [3.0, 2.5, 2.0],
                  tiers: [{profit_atr: 1.5, scale_pct: 0.33}, {profit_atr: 3.0, scale_pct: 0.33}]}
  allocation:
    mode: momentum_rotation
    momentum_rotation: {scoring: composite, min_momentum_threshold: 0.0}
```

## Validate (research only — never trades)
```bash
python -m src.improve_backtest --split 2024-06-01          # A: + risk-parity(ATR) column
python -m src.regime_backtester --split 2024-06-01         # B: + "Regime module" contenders
python -m src.profit_taking_research --split 2024-06-01    # C: B scale-out, D: C3 composite
pytest tests/test_risk_budget_sizing.py tests/test_regime.py \
       tests/test_profit_taking.py tests/test_composite_momentum.py
```
