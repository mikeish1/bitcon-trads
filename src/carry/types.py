"""
Plain data containers shared across the carry package.

Kept dependency-free (no ccxt / pandas imports) so the pure signal/risk logic and
its unit tests never need a network or heavy libraries.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CarryParams:
    """Static thresholds for the signal, resolved once from config."""
    min_entry_apr: float
    min_hold_apr: float
    unwind_apr: float                    # below THIS (may be slightly <0) we count to unwind
    flip_confirm_reads: int
    max_basis_bps: float
    expected_hold_days: float
    funding_interval_hours: float
    roundtrip_cost_frac: float           # 4 x (taker_fee + slippage): open+close, 2 legs
    max_feed_staleness_seconds: float

    @property
    def periods_per_year(self) -> float:
        """Funding intervals per year (e.g. 1095 for an 8h interval)."""
        return 8760.0 / self.funding_interval_hours


@dataclass(frozen=True)
class FundingQuote:
    """A point-in-time view of one asset's carry opportunity."""
    asset: str
    funding_rate: float                  # per-interval rate (e.g. 0.0001 = 1bp / 8h)
    funding_apr: float                   # annualised, smoothed over the lookback
    spot: float                          # spot last price
    perp: float                          # perp mark/last price
    basis_bps: float                     # (perp - spot) / spot * 1e4
    age_seconds: float                   # staleness of the freshest input (live prices)


@dataclass(frozen=True)
class CarryDecision:
    """Output of the pure signal for one asset."""
    action: str                          # "OPEN" | "HOLD" | "UNWIND" | "SKIP"
    reason: str
    gross_apr: float
    net_apr: float
    low_reads: int                       # updated consecutive sub-threshold counter


@dataclass(frozen=True)
class Fill:
    """One executed (or simulated) leg."""
    leg: str                             # "spot" | "perp"
    side: str                            # "buy" | "sell"
    qty: float
    price: float
    notional: float
    fee: float
    order_id: str = ""


@dataclass(frozen=True)
class PairFill:
    """A matched spot+perp pair (or the result of unwinding one)."""
    asset: str
    spot: Fill
    perp: Fill
    notional: float
