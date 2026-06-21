"""
Carry signal — PURE, mechanical, no network, no LLM, fully unit-testable.

Decides OPEN / HOLD / UNWIND / SKIP for one asset from a FundingQuote and the
current position state. The hard part it gets right is being honest about fees:
it gates on NET annualised carry (funding minus amortised round-trip fees), so a
positive-but-thin funding rate that a round trip would eat is correctly skipped.

Sign convention: perpetual funding > 0 means LONGS pay SHORTS. We are SHORT the
perp, so positive funding is INCOME. `funding_apr` is therefore our gross yield.
"""
from __future__ import annotations

from .types import CarryDecision, CarryParams, FundingQuote


def annualize_funding(rate_per_interval: float, interval_hours: float) -> float:
    """Per-interval funding rate -> annualised (e.g. 1bp/8h -> ~0.1095)."""
    if interval_hours <= 0:
        return 0.0
    return rate_per_interval * (8760.0 / interval_hours)


def fee_drag_apr(roundtrip_cost_frac: float, expected_hold_days: float) -> float:
    """Annualised cost of one open+close round trip, amortised over the hold.

    Short holds are expensive: a 0.40% round trip held only 14 days is ~10.4%/yr
    of drag. This is why thin funding is unprofitable even when positive.
    """
    if expected_hold_days <= 0:
        return float("inf")
    return roundtrip_cost_frac * (365.0 / expected_hold_days)


def net_carry_apr(funding_apr: float, params: CarryParams) -> float:
    """Gross funding APR minus amortised round-trip fee drag."""
    return funding_apr - fee_drag_apr(params.roundtrip_cost_frac, params.expected_hold_days)


def evaluate(quote: FundingQuote, *, held: bool, low_reads: int,
             params: CarryParams) -> CarryDecision:
    """Decide what to do with one asset this poll.

    Parameters
    ----------
    quote     : current funding/price view for the asset.
    held      : True if we currently hold a carry pair in this asset.
    low_reads : consecutive prior polls where funding sat below `min_hold_apr`.
    params    : resolved thresholds.
    """
    gross = quote.funding_apr
    net = net_carry_apr(gross, params)

    # Stale data -> never trade on it. The loop turns this into a breaker.
    if quote.age_seconds > params.max_feed_staleness_seconds:
        return CarryDecision("SKIP", f"stale feed ({quote.age_seconds:.0f}s)",
                             gross, net, low_reads)

    if not held:
        if abs(quote.basis_bps) > params.max_basis_bps:
            return CarryDecision("SKIP", f"basis too wide ({quote.basis_bps:.0f}bps)",
                                 gross, net, 0)
        if net >= params.min_entry_apr:
            return CarryDecision("OPEN", f"net carry {net:.1%} >= entry {params.min_entry_apr:.1%}",
                                 gross, net, 0)
        return CarryDecision("SKIP", f"net carry {net:.1%} < entry {params.min_entry_apr:.1%}",
                             gross, net, 0)

    # Held: three-zone hysteresis to avoid churn when funding is choppy near zero.
    #   gross >= min_hold_apr            -> comfortable: HOLD and reset the counter
    #   unwind_apr <= gross < min_hold   -> tolerance band: HOLD, neither count nor reset
    #   gross <  unwind_apr              -> clearly bad: count toward a confirmed unwind
    # unwind_apr may be slightly negative, so brief mildly-negative funding is
    # tolerated rather than paying a round trip to exit and immediately re-enter.
    if gross >= params.min_hold_apr:
        return CarryDecision("HOLD", f"funding healthy ({gross:.1%})", gross, net, 0)
    if gross >= params.unwind_apr:
        return CarryDecision(
            "HOLD",
            f"funding soft ({gross:.1%}) - in tolerance band "
            f"[{params.unwind_apr:.1%}, {params.min_hold_apr:.1%})",
            gross, net, low_reads)                       # neither count nor reset
    low_reads += 1
    if low_reads >= params.flip_confirm_reads:
        return CarryDecision("UNWIND",
                             f"funding {gross:.1%} < unwind {params.unwind_apr:.1%} "
                             f"for {low_reads} reads", gross, net, low_reads)
    return CarryDecision("HOLD",
                         f"funding {gross:.1%} below band, {low_reads}/"
                         f"{params.flip_confirm_reads} toward unwind", gross, net, low_reads)
