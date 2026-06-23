# Architecture Decision Records — ETF / US-Equities Re-Platform

Concise ADRs for the significant decisions. Each: context → decision → consequences.

---

## ADR-001 — Reuse/extend the ETF sleeve; do not rewrite *(Stage 0)*
**Context.** A working, hardened ETF sibling already exists (`src/etf/`): venue-agnostic
broker ABC, verbatim-reused selector, two-key tripwire, closed-candle signals.
**Decision.** Extend it in place; the only structural addition is a proper validation
harness (Stage 4). No greenfield rewrite.
**Consequences.** Minimal new surface area; preserves the safety architecture; new code
is glue + one new selector. Carries forward existing limitations to fix explicitly
(adjusted bars, validation harness) rather than re-deriving them.

## ADR-002 — Request split+dividend-adjusted bars (`Adjustment.ALL`) *(Stage 2)*
**Context.** `AlpacaBroker.daily_bars` used the SDK default (`RAW`/unadjusted). Splits
print phantom −50/−75% gaps that fire false trend exits and corrupt momentum.
**Decision.** Request `adjustment=ALL` by default; expose `etf.alpaca_adjustment` /
`ETF_ALPACA_ADJUSTMENT` so the (correct) default is explicit and auditable, with `raw`
available only as a deliberate, tested opt-out.
**Consequences.** Signal now matches the close-based backtest and live recomputation.
Back-adjusted levels shift as dividends accrue, but consistently across backtest+live.

## ADR-003 — Dual Momentum as a new selector, reusing `MomentumRotation` *(Stage 3)*
**Context.** Stage-1 selected Candidate B (GEM-style absolute+relative momentum with a
defensive sleeve) for a small taxable account. It differs from the incumbent rotation
(absolute filter, offensive/defensive split, always-invested defense).
**Decision.** Add `DualMomentumSelector` (pure) that **reuses `MomentumRotation`
verbatim** for momentum, the rebalance clock, and top-K + hysteresis; it only adds the
regime split (which candidate set feeds the allocator). Select it via
`etf.selection.mode = "rotation" | "dual_momentum"` through a `build_selector` factory,
so the loop and backtester stay mode-agnostic. Default stays `rotation` (opt-in,
reversible) — consistent with how this repo ships features off by default.
**Consequences.** Maximal reuse, identical interface (`is_due`/`plan`/`top_k`), no loop
or backtester rewrite. In dual-momentum mode the tradable universe is the union of the
offensive + defensive sleeves. Validation (Stage 4) decides whether to ever enable it.

## ADR-004 — Split-aware reconcile = flag only, never auto-rewrite basis *(Stage 3)*
**Context.** A split (or external partial change) makes the broker qty diverge from the
ledger. A split changes qty+price but not cost; an external sale changes cost — and the
broker snapshot alone cannot distinguish them.
**Decision.** `reconcile` detects qty drift beyond a 2% tolerance and **flags it**
(warning log + alert note returned to the loop for Telegram), but never rewrites cost
basis automatically. The "position gone" case still auto-closes (unambiguous). Mirrors
the spot bot's "leave ambiguous corporate actions to a human" philosophy.
**Consequences.** No silently-wrong cost basis. A human resolves a flagged split (paper
-only intent makes this low-stakes now). Marking uses possibly-stale ledger qty until
resolved — accepted; revisit if real-money scale changes the tradeoff.

## ADR-005 — PDT same-day guard *(Stage 3)*
**Context.** A <$25k margin account is limited to 3 day-trades / 5 business days. The
design holds multi-day/weekly, so day-trades are structurally near-impossible already.
**Decision.** Add a lightweight, low-state guard (`pdt_guard`, default on): never sell a
position the same calendar day it was opened. Defense-in-depth + a clean invariant
(zero same-day round-trips), rather than a stateful rolling day-trade counter.
**Consequences.** Guarantees PDT-safety with one date comparison; a same-day exit is
deferred one day (safe, since v1 has no intraday risk stop — all exits are rotation).

## ADR-006 — Defer correlation-aware position caps *(Stage 3)*
**Context.** The brief flags "three holdings secretly one bet." Dual Momentum's default
is `top_k = 1` (a single holding), and its diversification is **across time** (the
regime switch into the defensive sleeve), not across simultaneous correlated holdings.
**Decision.** Do **not** add a correlation cap now. Document the rationale; equal-weight
1/K + the existing exposure/capital caps suffice at `top_k` 1–2.
**Consequences.** Keeps the parameter count minimal (the design's main OOS-robustness
advantage). If `top_k` is later raised with correlated offensive names, revisit.
