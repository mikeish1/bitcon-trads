# Stage 0 — Plan & Scope: ETF / US-Equities Re-Platform

> Staged, judge-gated, human-checkpointed re-platform of **bitcon-trads** into a
> robust long-only US-equities/ETF sleeve. This file is the milestone plan +
> measurable acceptance criteria. Companion: [risk_register.md](risk_register.md).
> Decision trail (handoff + judge JSON per stage): [decisions/](decisions/).
>
> **Status: awaiting mandatory human checkpoint (Stage 0).** No code changes yet.

---

## 1. Where we actually are (grounded in the code, not the brief)

This is **not greenfield**. A working ETF sibling already exists under `src/etf/`
and was just hardened (commit `2e3bb34`). Confirmed by reading the modules:

| Capability | State | Evidence |
|---|---|---|
| Reused engine (Donchian `active_state` ∩ top-K momentum) | **Done** | [src/etf/selector.py](../../src/etf/selector.py) |
| Equal-weight 1/K risk + SQLite ledger + deployable-capital envelope | **Done** | [src/etf/risk.py](../../src/etf/risk.py) |
| sim / paper-broker / real-money tiers + **two-key tripwire** | **Done** | [src/etf/config_etf.py:67-92](../../src/etf/config_etf.py) |
| Alpaca adapter (fractional/notional orders, clock guard, no-phantom-fill) | **Done** | [src/etf/brokers/alpaca_broker.py](../../src/etf/brokers/alpaca_broker.py) |
| Market-hours guard (fail-closed) | **Done** | `is_market_open()` |
| Closed-candle signal discipline (live matches close-based backtest) | **Done** | [src/etf/data.py:31-40](../../src/etf/data.py) |
| Reconcile (close DB position the broker no longer holds) | **Partial** | only "position gone"; **splits not handled** |
| Pure walk-forward backtester | **Skeleton** | [src/etf/backtester.py](../../src/etf/backtester.py) — **no costs, no IS/OOS, no benchmarks** |
| Tests (selector/risk/broker/backtester) green | **Done** | `tests/test_etf_*.py` |

So the **plumbing is largely built and safe**. The gaps are concentrated in
**data integrity**, **honest validation**, and a few **equity-mechanics** items the
crypto lineage never needed.

### Reuse-vs-rewrite recommendation → **REUSE / EXTEND (high confidence)**
A rewrite is unjustified. The venue-agnostic broker ABC, the verbatim-reused
selector, and the intact tripwire are exactly the architecture the brief asks for.
Every stage below **extends** existing modules; nothing is rebuilt. The only
structural addition is a proper validation harness (Stage 4), because the current
backtester is too thin to honestly decide go/no-go.

---

## 2. Findings already surfaced (must be carried into later stages)

1. **🔴 CRITICAL — unadjusted bars.** `AlpacaBroker.daily_bars`
   ([alpaca_broker.py:68](../../src/etf/brokers/alpaca_broker.py)) calls
   `StockBarsRequest(...)` with **no `adjustment`** → alpaca-py defaults to
   `Adjustment.RAW`. Every Donchian breakout / momentum signal and every backtest
   is computed on **split- and dividend-unadjusted** prices. A split prints a fake
   −50%/−75% gap (false trend exit + bogus momentum); ex-div prints a fake gap
   down. **This violates the brief's mandatory "split- and dividend-adjusted data"
   rule and is the #1 fix (Stage 2): set `adjustment=Adjustment.ALL` + a regression
   test.** (Confirmed against Alpaca docs, June 2026.)
2. **🟠 Backtester has no cost/slippage/gap model and no IS/OOS split** — it marks
   close-to-close at zero cost. Any number it produces is optimistic and
   in-sample. Cannot be used for a deploy decision as-is (Stage 4).
3. **🟠 Dual-codepath drift risk** (carried from the spot bot's history): the live
   loop and the backtester are hand-mirrored with **no golden-master** for ETF.
   The spot bot already hit this exact bug. Stage 3 must add a sim≡backtest parity
   test (mirror of `tests/test_sim_live_parity.py`).
4. **🟡 Momentum is not robustly OOS-profitable on this lineage.** Memory note
   (2026-06): every crypto C2 momentum variant was weak OOS; the ETF sleeve
   inherited the same momentum fidelity gaps. Cross-asset dual-momentum has better
   academic support, but we treat the edge as **guilty until Stage 4 proves it**.
5. **🟡 Equity mechanics absent:** no explicit PDT guard, no wash-sale/tax-drag
   surfacing, no split-aware position reconcile, no gap-at-open fill modeling.

---

## 3. Milestones, dependencies, and ownership

Each stage is gated: judge ≥ 8.0 (≥ 8.5 + human sign-off for anything touching
real-money capability) **and** the mandatory human checkpoint before the next
stage starts.

| # | Stage | Depends on | Core work | Human checkpoint |
|---|---|---|---|---|
| 0 | **Plan & scope** (this doc) | — | plan + risk register + reuse decision | **Mandatory (now)** |
| 1 | Strategy & portfolio design | 0 ✅ | 3–4 diverse candidates; recommend one; economic rationale | **Mandatory — human selects** |
| 2 | Data & market-mechanics | 1 | **fix adjustment bug**; calendar (have); universe/bias audit; data-quality checks; point-in-time note | Selective (if bias-audit conf < 0.8) |
| 3 | Strategy & risk impl | 2 | scaffold → implement selected design; absolute-momentum/cash filter; correlation-aware caps; PDT guard; split-aware reconcile; **sim≡backtest parity test**; ADRs | **Mandatory — risk/sizing/exec code** |
| 4 | Validation harness | 3 | cost+slippage+gap model; walk-forward IS/OOS; regimes (2008/2020/2022/chop); Monte-Carlo/bootstrap; param sensitivity; full metrics vs **SPY** & **60/40**; tax-drag estimate | **Mandatory — go/no-go on results** |
| 5 | Paper deployment | 4 | wire Alpaca paper; alerts on fill/rebalance/regime/breaker/error; daily summary; kill-switch runbook; single-replica; acceptance criteria | **Mandatory go/no-go** |
| 6 | Go-live readiness package | 5 | go-live checklist (ETF); tiny-size protocol; house-money policy; kill-switch drill; tax note. **Produce, do not flip switches.** | **Mandatory** |

**Critical path:** 0 → 1 → 2 → 3 → 4 are strictly serial (each consumes the prior).
Stage 4 is the decision gate; Stages 5–6 only happen if Stage 4 clears the bar.

**Branching:** propose a dedicated branch `feat/etf-equities-replatform`. Open
question for the checkpoint: base it on `main`, or stack on the current
`fix/etf-sleeve-hardening` (which carries unmerged hardening)? See §6.

---

## 4. Measurable acceptance criteria (the bar Stage 4 must clear)

Framed honestly: the goal is **risk-adjusted, after-cost, after-tax survival and
compounding**, with **lower drawdown** than passive — **not** beating a bull
market's raw return. All gates judged **out-of-sample, net of costs**.

**Deploy-to-paper gate (Stage 4 must show ALL):**
- **G1 — Risk-adjusted edge:** OOS Sharpe **and** MAR/Calmar ≥ the better of SPY
  B&H and 60/40 on the same OOS window — *or* clearly lower drawdown at comparable
  net return. A raw-return shortfall vs a bull SPY is acceptable **only if**
  drawdown/risk-adjusted metrics win; it must be stated plainly, never hidden.
- **G2 — Drawdown ceiling:** OOS max drawdown **≤ −25%** (the stated tolerable
  line) **and** materially below SPY's OOS max drawdown.
- **G3 — OOS robustness:** OOS MAR ≥ **0.5 ×** IS MAR (≤ ~50% degradation) and OOS
  Sharpe does not flip negative. A sign inversion IS→OOS = fail.
- **G4 — Parameter stability:** a **±1 notch** change to any single core parameter
  (`entry_period`, `lookback_days`, `top_k`, `atr_trail_mult`, `rebalance_days`)
  must not swing the OOS headline metric by > ~25%. Collapse under perturbation =
  overfit = fail.
- **G5 — Regime survival:** non-catastrophic across 2008, 2020-COVID, 2022 bear,
  and ≥ 1 sideways/chop stretch (no single regime accounts for the entire edge).
- **G6 — Cost honesty:** the edge survives a realistic spread+slippage model and
  the modeled **gap-at-open** stop behavior (a stop fills at the open, not the
  trigger). Monte-Carlo/bootstrap on the trade sequence: median outcome — not a
  lucky path — clears G1.
- **G7 — Turnover/tax budget:** annualized turnover documented; estimated tax drag
  (short-term-gain + wash-sale interaction) surfaced; net-of-tax result for a
  taxable account still clears G1, **or** the design is explicitly scoped to a
  tax-advantaged account.

**Decision rule (binding):** if the design does **not** clearly clear G1 net of
cost and tax OOS, Stage 4 reports that plainly and recommends a simpler variant, a
return to Stage 1, or **not deploying** — no rationalizing a weak result.

**Paper-acceptance gate (Stage 5, before any go-live consideration):**
≥ 4 weeks paper; ≥ 1 full enter→hold→rotate-out cycle; ≥ 1 risk-on/off shift
(all-cash episode); zero unhandled errors; live paper behavior matches backtest
within tolerance (this is what the Stage-3 parity test underwrites).

---

## 5. Non-negotiables preserved throughout
Two-key tripwire never weakened · no-withdrawal keys · single-replica · AI stays
advisor/clerk (mechanical rules trade) · paper-first · no look-ahead /
survivorship / data-snooping · benchmark vs SPY **and** 60/40 · test suite stays
green · extend, don't rewrite.

## 6. Open questions for the human checkpoint
1. **Strategy latitude (Stage 1):** keep the existing dual-momentum rotation and
   only harden it, or also evaluate genuinely different designs (e.g. Antonacci
   GEM absolute+relative, risk-parity defensive sleeve, trend-on-bonds/gold
   overlay)? Recommendation: survey 3–4, but bias toward the smallest change that
   clears the bar.
2. **Account type:** taxable vs IRA/tax-advantaged? Drives how hard G7 (wash-sale/
   tax drag) binds. Recommendation needed to scope Stage 4 tax modeling.
3. **Branch base:** `feat/etf-equities-replatform` off `main`, or stacked on
   `fix/etf-sleeve-hardening`?
4. **Capital reality:** confirm intended ETF sleeve size (config default
   `etf.capital.sleeve_usd: 2000`) and "paper only" vs "paper then tiny live".
