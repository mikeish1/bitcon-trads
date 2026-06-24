# Stage 1 — Strategy & Portfolio Design

> Tree-of-Thoughts: survey the landscape, generate **4 genuinely different**
> candidate designs, evaluate against the constraints, recommend one.
> Context locked at the Stage-0 checkpoint: **long-only · small TAXABLE account
> ($250–$2k) · weekly rebalance with hysteresis · ETF basket across asset classes
> · paper-only · drawdown-first, don't-cut-winners, house-money principal rule.**
>
> **Taxable** makes turnover, short-term-gain, and 30-day wash-sale interaction
> first-class selection penalties. **Small account** makes ticket count / fixed
> friction matter. The honest goal is **lower drawdown + decent risk-adjusted,
> after-tax return — not beating a bull market**.
>
> **Status: awaiting mandatory human checkpoint — you select the design.**

---

## 1. Taxonomy survey (accept / reject, one line each)

| Family | Verdict | Reason |
|---|---|---|
| Trend-following / TSMOM (Donchian, MA-cross, channel, CTA) | **ACCEPT (core)** | The reused engine; strongest economic prior (Moskowitz/Ooi/Pedersen TSMOM); natural risk-off. |
| Cross-sectional momentum (top-K rotation) | **ACCEPT (w/ caveat)** | Already built, but weaker across a small *heterogeneous* asset-class basket than within a homogeneous one; best paired with an absolute filter. |
| **Dual momentum** (absolute + relative, GEM) | **ACCEPT (lead)** | Best-documented small-N rotation; absolute filter is the drawdown control; low turnover → tax-friendly. |
| Factor tilts (value/quality/low-vol/size/carry) | **REJECT (primary)** | Hard to express cleanly with a few broad ETFs; adds cost/expense-ratio + overfit surface at this scale. Low-vol could be a weighting nuance only. |
| Mean reversion (RSI-2, Bollinger) | **REJECT** | Opposite regime bet; high turnover = tax-hostile in a taxable account; cuts winners — conflicts with the house-money/let-winners-run mandate. |
| Regime filters / canaries (200-DMA, VIX, credit, breadth, curve) | **ACCEPT (overlay)** | Economically justified risk-on/off + defensive sleeve. Use price/abs-momentum (robust); avoid VIX/credit/curve (data + overfit risk) at this scale. |
| Vol-targeting / risk parity | **ACCEPT (construction option)** | Inverse-vol weighting is robust at small N; full vol-target adds a parameter — keep it light. |
| Portfolio construction: EW / inverse-vol / MV / BL / **HRP** | **EW + inverse-vol ACCEPT; MV/BL/HRP REJECT** | Estimation error dominates with small N + short history; HRP needs many assets to pay off. EW and inverse-vol are the robust choices here. |
| Sizing: fixed-frac / ATR / fractional-Kelly / vol-target | **ATR+EW ACCEPT; Kelly REJECT(primary)** | Reconcile with the existing cap stack; Kelly needs a reliable edge estimate we do not have — heavily discounted at best. |
| Crisis / defensive overlays (trend-on-bonds/gold, cash floor) | **ACCEPT (core)** | The defensive sleeve (bonds/gold/T-bills/cash when risk-off) is the main small-account drawdown lever. |
| Execution: limit / market / MOO-MOC / TWAP-VWAP | **Market notional + (opt) MOO ACCEPT; TWAP/VWAP REJECT** | Alpaca fractional/notional fits 1/K sizing; size too small to need TWAP/VWAP. |
| ML (regime classify / signal blend) | **REJECT (now)** | Overfit + regime-fragility + short data; no evidence it beats a simple rule at this scale. Revisit only as walk-forward regime classification, never a trade trigger. |

---

## 2. The four candidates

All four reuse the Donchian/momentum engine and the existing cap stack + tripwire;
they differ in **how concentrated, how reactive, and how they go defensive**.

### A — Harden-the-incumbent: Donchian-gated rotation + absolute-momentum cash filter + chandelier stop
- **Thesis.** Keep the top-K rotation (Donchian `active_state` ∩ N-day momentum); plug its two holes: (1) an **absolute-momentum / cash filter** so a leader with negative own-momentum routes to T-bills/cash instead of being the "least-bad" holding, and (2) a **daily chandelier stop** between rebalances.
- **Mechanisms.** TSMOM (Donchian) + cross-sectional momentum + absolute momentum.
- **Why it should work.** Trend + relative strength; the Donchian gate already yields natural risk-off; the absolute filter closes the "holds losers in a bear" gap.
- **Failure modes.** Momentum V-reversals (2009/2020 snapbacks); chop whipsaw; the chandelier stop is **gap-exposed** (fills at the open); **highest turnover of the four → worst taxable drag**.
- **Parameters.** ~7 (entry_period, lookback, top_k, rebalance_days, keep_band, atr_trail, abs-mom threshold) — most of the four.
- **Fit.** Smallest code change, reuses everything; but inherits the documented *"momentum weak OOS on this lineage"* risk and is the least tax-friendly.

### B — Dual Momentum (Antonacci GEM-style), enriched defensive sleeve  ← recommended primary
- **Thesis.** Rank a small **offensive** set (US `SPY`, intl `EFA`/`EEM`) by relative momentum; hold the strongest **only if** its absolute ~12-month momentum beats T-bills (`BIL`); otherwise rotate to the strongest **defensive** asset (`TLT`/`IEF`/`GLD`/`BIL`). Hold 1–2 names; weekly clock with hysteresis but a slow lookback so it acts ~monthly.
- **Mechanisms.** Absolute + relative momentum + defensive sleeve.
- **Why it should work.** The best-documented small-N rotation; the absolute gate is the drawdown control; **very low turnover → mostly long-term gains, few wash-sale events** (ideal for taxable); lets the one big winner run (house-money-friendly).
- **Failure modes.** Concentration (1–2 holdings); threshold whipsaw; the slow lookback lags fast crashes (2018Q4, Feb-2020); single-name gap risk; **2022 is the stress — bonds *and* stocks fell**, so the defensive sleeve must be tested then (gold/T-bills, not just long bonds).
- **Parameters.** ~4 (lookback, top_k 1–2, abs-mom benchmark, hysteresis) — **fewest; hardest to overfit; best `simplicity` score.**
- **Fit.** Excellent for taxable + small + drawdown-first. Different in behavior (concentrated, slow) from the incumbent; reuses momentum + adds an absolute gate and a defensive default.

### C — CTA-style multi-asset trend ensemble, inverse-vol weighted
- **Thesis.** Run the Donchian/TSMOM signal **independently per ETF** across the whole basket; hold **every** asset currently trending, weighted **inverse-vol** (risk-parity-lite); the rest in cash. Long-only managed futures.
- **Mechanisms.** Per-asset TSMOM + inverse-vol construction + cash floor.
- **Why it should work.** Diversification across many simultaneous trends smooths the curve; inverse-vol stops a volatile sleeve (`DBC`/`EEM`) from dominating; goes mostly-cash in a correlated crash → **best drawdown profile of the four**.
- **Failure modes.** More holdings = more tickets/cost/**tax** in a small account; needs a vol estimate (one extra param); lags a single roaring asset (won't beat a stock-only bull); rebalance drag.
- **Parameters.** ~5 (entry_period, atr_trail, vol-lookback, cash floor, rebalance).
- **Fit.** Best **risk-adjusted/drawdown** candidate and most diversified; turnover/tax higher than B/D. Reuses Donchian; changes weighting from EW to inverse-vol and holds the full trending set.

### D — Regime-gated barbell (offense ↔ defense switch)
- **Thesis.** A slow **200-DMA / absolute-momentum canary on SPY** flips the whole book: **risk-on** → an equal-weight or momentum-picked offensive equity/credit basket; **risk-off** → a defensive sleeve (`TLT`/`IEF`/`GLD`/`BIL`). Always invested in *something*.
- **Mechanisms.** Regime canary + defensive sleeve + light intra-regime selection.
- **Why it should work.** "Be out of equities in sustained downtrends" is the biggest historical drawdown lever; a slow canary captures most of it with **few flips/year → tax-light**; defense earns bond/gold carry instead of 0% cash.
- **Failure modes.** Threshold whipsaw (2011/2015/2018); single-canary point of failure; **2022 again** (bond defense failed); slow re-entry after V-bottoms.
- **Parameters.** ~4 (MA period, canary asset, the two baskets).
- **Fit.** Strong drawdown control + tax-light + simple; binary character differs from the incumbent; 2022 is its decisive stress test.

---

## 3. Comparison matrix

| Axis | A Harden-incumbent | **B Dual-momentum** | C CTA inverse-vol | D Regime barbell |
|---|---|---|---|---|
| Concentration | Medium (top-K) | **High (1–2)** | Low (many) | Medium (basket) |
| Reactivity | Fast | Slow | Fast | Slow |
| Turnover / **tax drag (taxable)** | **High (worst)** | **Low (best)** | Med-High | Low |
| Expected drawdown control | Medium | High | **Highest** | High |
| Wash-sale exposure | High | **Low** | Med | Low |
| Param count / overfit risk | ~7 (highest) | **~4 (lowest)** | ~5 | ~4 |
| Economic-prior strength | Medium | **High** | High | Medium-High |
| "Lets winners run" (house-money) | OK (stop can clip) | **Strong** | Strong | OK |
| Reuse / build effort | **Lowest** | Low-Med | Medium | Low-Med |
| 2022 (bonds+stocks down) resilience | Med (goes cash) | Test-dependent | **Best (cash)** | **Weakest (bond defense fails)** |
| Small-account ticket friction | High | **Low** | High | Low |

---

## 4. Recommendation

**Primary: Candidate B — Dual Momentum with an enriched defensive sleeve.**
It is the best fit to *every* locked constraint: taxable (lowest turnover, fewest
wash-sales, mostly long-term gains), small account (fewest tickets), drawdown-first
(absolute filter + defensive rotation), and house-money (lets one big winner run).
Crucially it is **the fewest-parameter design**, which — given the honest prior that
momentum is *not* robustly OOS-profitable on this lineage — gives it the best chance
to survive Stage-4 walk-forward without curve-fitting. Enrich GEM's single bond
default into a **strongest-of {bonds, gold, T-bills}** defensive pick so the 2022
"bonds and stocks both fell" failure mode has somewhere real to hide (gold/T-bills).

**Alternative: Candidate C** if you weight *maximum* drawdown reduction and
diversification over tax efficiency — it had the best crash behavior but costs more
turnover/tax in a taxable account.

**Control (not a choice — always run in Stage 4): Candidate A.** The hardened
incumbent is the thing any new design must *beat* net-of-tax OOS to justify the
switch, alongside the mandatory SPY B&H and 60/40 benchmarks. If B/C don't clearly
beat A **and** the passive benchmarks after tax OOS, the honest call is "keep the
incumbent" or "don't deploy."

**Rejected as primary: D** — elegant and tax-light, but a single canary + the 2022
bond-defense failure make it fragile; its best ideas (the defensive sleeve, an
absolute regime gate) are **folded into B** rather than run standalone.

---

## 5. What Stage 4 must decide (carried forward)
Validate the selected design against gates **G1–G7** ([plan.md §4](plan.md)) net of a
realistic cost+slippage+**gap-at-open** model, walk-forward IS/OOS, across
2008/2020/2022/chop, with Monte-Carlo on the trade sequence and ±1-notch parameter
sensitivity — benchmarked vs **Candidate A (control)**, **SPY B&H**, and **60/40**,
with **after-tax** (short-term-gain + wash-sale) results surfaced for the taxable
account. Decision rule is binding: no clear OOS, after-tax, risk-adjusted win → report
plainly and recommend simpler / re-design / don't deploy.
