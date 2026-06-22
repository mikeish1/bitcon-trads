# Risk Register — ETF / US-Equities Re-Platform

Companion to [plan.md](plan.md). Likelihood × Impact on a L/M/H scale. "Stage" =
where the mitigation lands. Ordered by current exposure (severity first).

| # | Risk | L | I | Mitigation | Stage |
|---|------|---|---|------------|-------|
| R1 | **Unadjusted bars** — Donchian/momentum + backtest run on RAW (split/div-unadjusted) Alpaca data; splits print fake −50/−75% gaps → false exits & bogus momentum; ex-div prints fake gap-down. Already in live code. | H | H | Set `adjustment=Adjustment.ALL` in `daily_bars`; regression test on a known split (e.g. a 4:1) asserting no phantom gap; bias-audit note. | 2 |
| R2 | **Optimistic / in-sample backtest** — current harness marks close-to-close at **zero cost**, no IS/OOS split, no gap-at-open, no benchmarks → any "edge" is illusory. | H | H | Build cost+slippage+**gap-at-open** model; walk-forward IS/OOS; Monte-Carlo/bootstrap; benchmark vs SPY & 60/40; binding decision rule (plan G1–G7). | 4 |
| R3 | **Dual-codepath drift** — live loop vs backtester are hand-mirrored, **no golden master** for ETF; the spot bot already shipped a "validated" number its live code didn't run. | H | H | sim≡backtest parity test (mirror `tests/test_sim_live_parity.py`); single shared selector already helps; reconcile fill models across sim/paper/backtest. | 3/4 |
| R4 | **Momentum may not be robustly OOS-profitable** on this lineage (memory: every crypto C2 variant weak OOS; ETF inherited the fidelity gaps). | M | H | Treat edge as guilty until proven; cross-asset dual-momentum has better priors but Stage 4 decides; be willing to recommend **not deploying** or a simpler variant. | 1/4 |
| R5 | **Overfitting via parameter search** — small N, tempting to tune. | M | H | Few, economically-justified params; parameter-sensitivity (plan G4); walk-forward only; reject configs that collapse under ±1 notch. | 3/4 |
| R6 | **Gap risk on stops** — equities gap at the open; a chandelier stop does **not** protect against an overnight gap; it fills at the open. | M | M | Model gaps in backtest (G6); prefer rotation-cadence exits over tight intraday stops; document residual gap exposure; size for it. | 3/4 |
| R7 | **Tax drag / wash-sale** (taxable acct) — rotation re-entry within 30 days of a loss sale triggers wash-sale; short-term gains taxed high. | M | M | Surface tax drag in reporting (G7); document wash-sale × rotation; recommend longer hysteresis or tax-advantaged account; confirm account type at checkpoint. | 4/6 |
| R8 | **Split-unaware position reconcile** — reconcile only handles "position gone"; a split changes qty/basis silently until then. | M | M | With adjusted bars the signal is safe; add split detection → alert/re-sync (or document the manual step, as the spot bot does); never auto-misstate basis. | 3 |
| R9 | **Small-account fixed-cost / dust drag** — $250–$2k sleeve; min-notional + spread dominate. | M | M | Fractional/notional orders (already used); `min_notional_usd` dust guard (have); cost-aware turnover budget; favor liquid ETFs only. | 3/4 |
| R10 | **PDT rule** — <$25k margin acct limited to 3 day-trades / 5 days. | L | M | Weekly cadence + multi-day holds keep it clear; add an explicit guard/flag that refuses a same-day round-trip of one symbol; document. | 3 |
| R11 | **Survivorship / universe look-ahead** — using today's "good" ETF list is a mild forward peek. | L | M | Fixed, liquid, economically-justified cross-asset basket (not data-mined); document that selection is by economic role, not past returns; note delisted-ETF caveat. | 2 |
| R12 | **Corporate actions / halts** mishandled mid-trade. | L | M | Adjusted bars (R1); reconcile (R8); Alpaca simply won't fill a halted symbol — fail-safe; log + skip. | 2/3 |
| R13 | **Duplicate orders from >1 replica.** | L | H | `railway.json` pins `numReplicas: 1`; single-replica documented in runbook; startup banner states mode. | 5 |
| R14 | **Live execution safety regressions** (phantom fill, key scope). | L | H | No-phantom-fill already fixed; two-key tripwire preserved & tested; **no-withdrawal** keys; least-privilege; checklist. | 3/5/6 |
| R15 | **Look-ahead via indicator computation.** | L | H | Indicators are causal (backward-rolling) + closed-candle discipline already enforced; lock with the bias-audit note + a test. | 2 |
| R16 | **Scope creep / over-engineering** — brief lists many methods; small account rewards simplicity. | M | M | Bias toward smallest change clearing the bar; judge gate's "simplicity" dimension; ADRs justify every added parameter. | 1–3 |

## Top-3 to watch
- **R1** (data integrity) — invalidates everything downstream; fix first.
- **R2 + R3** (validation honesty + codepath drift) — the difference between a real
  edge and a backtest mirage; this is where prior work on this repo got burned.
- **R4** (the edge may not exist OOS) — the register's honest hypothesis is that the
  most likely Stage-4 outcome is "lower drawdown, not higher return," and a real
  possibility is "don't deploy." Both are acceptable, reportable outcomes.
