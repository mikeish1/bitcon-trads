# Stage 4 — Validation Report (ETF Dual Momentum)

> Realistic, gap-aware, walk-forward validation of the Stage-1-selected design
> (Candidate B, Dual Momentum) against **Control-A** (the incumbent rotation),
> **SPY** buy-and-hold, and **60/40**. Real split+dividend-adjusted daily data
> (yfinance), **2008-07 → 2026-06** (252-day warmup from BIL's 2007-05 inception).
> Net of **5 bps/side** slippage; orders fill at the **next session's open**
> (so overnight gap risk is modeled). Harness: `src/etf/research/`, unit-tested
> (`tests/test_etf_research.py`). Reproduce: `python -m src.etf.research.validate`.
>
> **VERDICT: ❌ DO NOT DEPLOY Dual Momentum.** It fails the deploy gate decisively
> (details below). The honest winner for this mandate is a **simple, low-turnover
> 60/40-style allocation**, which beats every active design on risk-adjusted,
> after-cost, after-tax, out-of-sample terms.

---

## 1. Headline results (2008-07 → 2026-06, net of cost)

| Strategy | CAGR | maxDD | Sharpe | Sortino | Calmar | **OOS** Sharpe | OOS maxDD | Turnover/yr | Est. tax drag¹ |
|---|---|---|---|---|---|---|---|---|---|
| **Dual Momentum** (252/top1/BIL) | 6.05% | **33.7%** | 0.41 | 0.50 | 0.18 | 0.56 | 33.7% | 8.5× | ~22% of init |
| Control-A rotation | 5.16% | 14.8% | 0.57 | 0.67 | 0.35 | 0.69 | 14.8% | 15.7× | ~43% of init |
| **SPY** buy & hold | **12.24%** | 47.2% | 0.68 | 0.83 | 0.26 | 0.86 | 33.7% | ~0 | ~0 |
| **60/40** SPY/AGG | 8.95% | 29.7% | **0.77** | **0.94** | 0.30 | **0.89** | 21.7% | ~0 (rebal) | ~0 |

¹ Rough taxable-account estimate (in-year taxation, ST 24% / LT 15%, simplified
wash-sale); buy-and-hold benchmarks realize almost nothing, so their drag ≈ 0.

**Walk-forward (Dual Momentum):** IS (2008-16) CAGR 2.9% / Sharpe 0.25 / Calmar 0.10;
OOS (2017-26) CAGR 8.8% / Sharpe 0.56 / Calmar 0.26. OOS is *better* than IS (no
overfit-collapse), but the OOS bar is set by the benchmarks — and DM loses to both.

## 2. Regime behaviour (window return / maxDD) — DM vs SPY
| Regime | Dual Momentum | SPY | Read |
|---|---|---|---|
| GFC 2008-09 | **−23.8%** / 27.5% | −26.4% / 47.2% | DM's **only** win — lower drawdown in the slow grind. |
| COVID 2020 | −24.7% / 33.7% | **−3.9%** / 33.7% | **Momentum V-reversal**: went defensive after the crash, missed the snap-back. |
| Bear 2022 | −20.2% / 21.2% | **−18.7%** / 24.5% | The predicted "bonds **and** stocks both fell" failure — the defensive sleeve didn't help. |
| Chop 2015-16 | −5.3% / 12.2% | **+5.2%** / 13.0% | Whipsawed in a sideways tape. |

The edge is concentrated in a **single regime (2008)**; DM was beaten in COVID,
2022, and chop. This is the textbook momentum-crash / whipsaw failure the Stage-1
analysis flagged — now confirmed with data.

## 3. Monte-Carlo (block bootstrap, DM daily returns, n=1000, block=20)
CAGR p5 **0.3%** / p50 6.6% / p95 13.1% · maxDD p50 **39.9%** / p95 **61.5%** ·
Sharpe p5 0.11 / p50 0.44. The **median** path has a ~40% drawdown and a Sharpe of
0.44 — i.e. the realized result is not a lucky path, and the *typical* outcome still
fails the bar. The 5th-percentile CAGR is ~0%.

## 4. Parameter sensitivity (DM, full period)
CAGR **5.7%–9.7%**, maxDD **24%–38%**, Sharpe **0.41–0.61**, Calmar **0.16–0.29**
across lookback ∈ {126,189,252,315} × top_k ∈ {1,2}. The headline (Calmar) nearly
doubles across settings, and the GEM-standard 252/top-1 is among the **weaker**
choices (126/top-1 was best at CAGR 9.7% / Sharpe 0.61) — yet **none** of the 16
configurations beats 60/40's Sharpe (0.77) or SPY's OOS Sharpe (0.86). The
absolute-benchmark choice (BIL vs 0.0) barely matters. Conclusion: no parameter
neighbourhood rescues the design; the result is not a tuning artifact.

## 5. Decision rule (plan.md G1–G7)
| Gate | Result | Evidence |
|---|---|---|
| **G1** risk-adjusted edge vs SPY/60-40 | ❌ **FAIL** | OOS Sharpe 0.56 < SPY 0.86 / 60-40 0.89; OOS Calmar 0.26 < 0.45 / 0.46. |
| **G2** maxDD ≤ −25% & < SPY | ❌ **FAIL** | DM maxDD 33.7% > 25%; OOS DD ties SPY (33.7%) and is worse than 60/40 (21.7%). |
| **G3** OOS ≥ 0.5× IS, no sign flip | ✅ pass (weak) | OOS > IS — but only because IS was very weak. |
| **G4** ±1-notch parameter stability | ⚠️ marginal | Calmar 0.16–0.29; 252-default is sub-optimal; no config clears the bar. |
| **G5** regime survival (no single-regime edge) | ❌ **FAIL** | Edge is GFC-only; lost COVID, 2022, chop. |
| **G6** cost/gap honest; MC median clears G1 | ❌ **FAIL** | Median bootstrap maxDD ~40%, Sharpe 0.44 — below the benchmarks. |
| **G7** turnover/tax; after-tax clears G1 | ❌ **FAIL** | 8.5× turnover, ~22% tax drag; after-tax the G1 gap widens. |

**5 of 7 gates fail** (1 weak-pass, 1 marginal). The binding decision rule applies.

## 6. Recommendation (honest, per the brief)
**Do not deploy Dual Momentum.** It does not beat buy-and-hold or 60/40 on
risk-adjusted, after-cost, after-tax, OOS terms — it loses on most of them, breaches
the drawdown ceiling, and is whipsawed outside the GFC.

**The simpler approach wins.** For this exact mandate — *lower drawdown + decent
risk-adjusted, after-tax return for a small taxable account* — a **plain, low-turnover
60/40-style allocation** (SPY/AGG, rebalanced periodically) delivered the best Sharpe
(0.77 full / 0.89 OOS) and Calmar (0.30 / 0.46), a tolerable 29.7%/21.7% drawdown,
and **near-zero turnover and tax drag**. Control-A had the lowest drawdown (14.8%) but
the worst tax drag (~43%) and still trailed 60/40 on Sharpe — also not worth deploying
over a passive blend.

**Suggested next step (human decision):** either
1. **Return to Stage 1** and implement a *simple static/low-turnover allocation*
   (60/40 or an inverse-vol "all-weather" buy-and-hold-rebalance) as the actual ETF
   sleeve — it is the evidence-based winner and is trivially tax-efficient; or
2. **Do not add an ETF sleeve** and keep capital passive.

The dual-momentum code remains in the repo **off by default** (`selection.mode`
stays `rotation`); nothing is enabled. No real money is at stake.

## 7. Honest limitations of this harness
- **Warmup:** the 252-day lookback means trading starts ~2008-07, so the 2007 market
  top is excluded (the worst of the GFC crash is included). Does not change the verdict.
- **Tax model** is a rough estimate (in-year taxation, simplified wash-sale), used for
  *relative* comparison; the active-vs-passive tax gap is the robust takeaway.
- **Slippage** fixed at 5 bps/side; these liquid ETFs trade tighter, so costs are if
  anything conservative — and DM already loses gross.
- **Survivorship:** broad flagship ETFs (low risk), per [data_bias_audit.md](data_bias_audit.md).
