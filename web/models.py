"""
Pydantic v2 response models for the dashboard API.

These mirror and ENRICH the bot's SQLite schema (`trades`, `state`, `decisions`)
with computed fields. Where a metric also exists in the bot, the formula here is
deliberately identical to `RiskManager` (e.g. win rate, day/week return, the
`can_open_trade` gates) so the dashboard never disagrees with the bot's own
numbers. See docs/DASHBOARD_ARCHITECTURE.md §5.4.

All models are frozen (immutable) and forbid extra fields, so a typo in a query
builder fails loudly instead of silently shipping a wrong shape to the frontend.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Generic, Optional, TypeVar

from pydantic import BaseModel, ConfigDict, Field

_T = TypeVar("_T")


class _Base(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# --------------------------------------------------------------------------- #
# Shared / envelope                                                           #
# --------------------------------------------------------------------------- #
class ModeBadge(_Base):
    """The PAPER / PAPER-BROKER / LIVE indicator the UI shows prominently."""

    mode: str = Field(description="PAPER | PAPER-BROKER | LIVE")
    real_money: bool
    place_orders: bool
    exchange_id: str
    quote_ccy: str


class Page(_Base, Generic[_T]):
    """Keyset-paginated envelope (cursor on the autoincrement id, DESC)."""

    items: list[_T]
    next_cursor: Optional[int] = None
    has_more: bool = False
    total_estimate: int = 0


# --------------------------------------------------------------------------- #
# Positions                                                                   #
# --------------------------------------------------------------------------- #
class OpenPosition(_Base):
    id: int
    symbol: str
    opened_at: datetime
    entry_price: float
    qty: float
    cost_usd: float
    initial_stop: float = Field(description="trades.stop_price - the first stop placed")
    current_stop: float
    peak_price: float
    mode: str
    reason: str
    # ---- computed (live price) ----
    last_price: float
    market_value: float = Field(description="qty * last_price")
    unrealized_pnl_usd: float = Field(description="market_value - cost_usd")
    unrealized_pnl_pct: float
    distance_to_stop_pct: float = Field(description="(last_price - current_stop)/last_price * 100")
    r_multiple: float = Field(description="(last_price - entry)/(entry - initial_stop)")
    drawdown_from_peak_pct: float
    pct_of_per_asset_cap: float = Field(description="market_value / (equity * per_asset_alloc_pct)")
    age_hours: float
    price_is_stale: bool


# --------------------------------------------------------------------------- #
# Trade history                                                               #
# --------------------------------------------------------------------------- #
class ClosedTrade(_Base):
    id: int
    symbol: str
    opened_at: datetime
    closed_at: Optional[datetime]
    entry_price: float
    exit_price: Optional[float]
    qty: float
    cost_usd: float
    pnl_usd: Optional[float]
    return_pct: Optional[float]
    hold_hours: Optional[float]
    r_multiple: Optional[float]
    mode: str
    reason: str


class TradeAggregates(_Base):
    """Footer totals for the currently-filtered history view."""

    count: int
    total_pnl_usd: float
    wins: int
    losses: int
    win_rate_pct: float


# --------------------------------------------------------------------------- #
# Decisions                                                                   #
# --------------------------------------------------------------------------- #
class Decision(_Base):
    id: int
    ts: datetime
    symbol: Optional[str]
    action: str
    conviction: int
    consulted_claude: bool
    reasoning: str


# --------------------------------------------------------------------------- #
# KPI summary                                                                 #
# --------------------------------------------------------------------------- #
class KpiSummary(_Base):
    mode: ModeBadge
    equity: float
    cash: float
    open_value: float
    unrealized_pnl_usd: float
    day_return_pct: float
    week_return_pct: float
    pnl_today_usd: float
    open_positions: int
    closed_today: int
    trades_today: int
    wins: int
    losses: int
    win_rate_pct: float
    consecutive_losses: int
    equity_basis: str = Field(
        description="'paper_ledger' (paper_cash + MTM) or 'approx' (broker mode; "
        "cash read from the paper ledger as a fallback since the dashboard holds "
        "no exchange keys)")
    as_of: datetime
    price_age_seconds: float


# --------------------------------------------------------------------------- #
# Risk gauges (one per can_open_trade gate)                                    #
# --------------------------------------------------------------------------- #
class GaugeValue(_Base):
    key: str
    label: str
    current: float
    limit: float
    pct_of_limit: float = Field(ge=0, description="current/limit, clamped >= 0 for the bar")
    breached: bool
    tooltip_plain: str
    tooltip_math: str


class RiskGauges(_Base):
    daily_loss: GaugeValue
    weekly_loss: GaugeValue
    consecutive_losses: GaugeValue
    trades_today: GaugeValue
    concurrent_positions: GaugeValue
    total_exposure: GaugeValue
    circuit_breaker_tripped: bool
    regime_enabled: bool
    regime_on: Optional[bool]
    as_of: datetime


# --------------------------------------------------------------------------- #
# Performance                                                                 #
# --------------------------------------------------------------------------- #
class EquityPoint(_Base):
    ts: datetime
    equity: float
    drawdown_pct: float = Field(description="(equity - running_peak)/running_peak * 100, <= 0")


class EquitySeries(_Base):
    points: list[EquityPoint]
    start_equity: Optional[float]
    end_equity: Optional[float]
    max_drawdown_pct: float
    downsampled: bool
    available: bool = Field(description="False until the snapshot sampler has data")


class PerformanceStats(_Base):
    closed_trades: int
    wins: int
    losses: int
    win_rate_pct: float
    gross_profit_usd: float
    gross_loss_usd: float
    profit_factor: Optional[float] = Field(description="gross_profit/|gross_loss|; None if no losses")
    expectancy_usd: float = Field(description="mean PnL per closed trade")
    avg_win_usd: float
    avg_loss_usd: float
    avg_hold_hours: float
    best_trade_usd: float
    worst_trade_usd: float
    max_drawdown_pct: float


class CoinAttribution(_Base):
    base: str
    closed_trades: int
    realized_pnl_usd: float
    wins: int
    losses: int
    win_rate_pct: float


class RegimeBucket(_Base):
    regime_on: Optional[bool]
    label: str
    closed_trades: int
    realized_pnl_usd: float
    win_rate_pct: float


class RegimeSplit(_Base):
    buckets: list[RegimeBucket]
    available: bool = Field(description="needs equity_snapshots with regime flags")


# --------------------------------------------------------------------------- #
# Config (read-only, secrets redacted)                                         #
# --------------------------------------------------------------------------- #
class ConfigView(_Base):
    mode: ModeBadge
    universe: list[str]
    strategy: dict[str, Any]
    risk: dict[str, Any]
    exits: dict[str, Any]
    safety: dict[str, Any]
    portfolio: dict[str, Any]
    market: dict[str, Any]
    capital_limits: dict[str, Any]
    redacted_keys: list[str] = Field(description="config keys removed before serving")


# --------------------------------------------------------------------------- #
# Capital simulation                                                           #
# --------------------------------------------------------------------------- #
class CapitalSimulation(_Base):
    sleeve: str
    valid: bool
    errors: list[dict[str, Any]] = Field(default_factory=list)
    policy: Optional[dict[str, Any]] = None
    description: Optional[str] = None
    equity: float
    available_cash: float
    committed: float
    deployable_capital: float
    remaining_capacity: float
    current_exposure_pct: Optional[float] = None


# --------------------------------------------------------------------------- #
# Health                                                                       #
# --------------------------------------------------------------------------- #
class HealthStatus(_Base):
    status: str = Field(description="healthy | degraded | stale | starting")
    db_ok: bool
    mode: str
    open_positions: int
    last_bot_activity_at: Optional[datetime] = Field(
        description="newest write across trades/decisions - the only bot-liveness "
        "signal visible to a read-only observer")
    last_bot_activity_age_seconds: Optional[float]
    last_decision_at: Optional[datetime]
    last_trade_opened_at: Optional[datetime]
    poll_seconds: int
    regime_enabled: bool
    regime_on: Optional[bool]
    circuit_breaker_tripped: bool
    snapshot_count: int
    db_size_bytes: int
    server_time: datetime


class ApiError(_Base):
    error: dict[str, Any]


# --------------------------------------------------------------------------- #
# Sleeves (multi-strategy coverage: spot / carry / etf)                        #
#                                                                             #
# src/run_all.py runs up to three independent bots into the SAME SQLite file: #
# the spot trend-follower (state/trades/decisions), the funding-carry bot     #
# (carry_positions/carry_funding/carry_state), and the ETF momentum bot       #
# (etf_positions/etf_state). The models below let the read-only dashboard     #
# surface the carry and ETF sleeves it previously ignored. They are derived   #
# purely from those tables + the audited CapitalSettingsService; no live      #
# equity prices are required (ETF holdings are equities the crypto price feed  #
# cannot quote, and carry pairs are delta-neutral), so the views degrade       #
# gracefully rather than asserting numbers they cannot verify read-only.       #
# --------------------------------------------------------------------------- #
class SleeveCard(_Base):
    """One headline tile per sleeve for the overview strip."""

    key: str = Field(description="spot | carry | etf")
    label: str
    active: bool = Field(description="True once the sleeve has run (its tables/state exist)")
    mode: Optional[str] = Field(description="PAPER/LIVE/SIM... from the sleeve's own rows; None if never run")
    open_positions: int
    primary_value_usd: Optional[float]
    primary_label: str = Field(description="what primary_value_usd means, e.g. 'Equity' or 'Capital used'")
    realized_pnl_usd: Optional[float] = Field(description="lifetime realized P&L from CLOSED positions")
    capital_source: Optional[str] = Field(description="winning capital-limit source: env/override_file/yaml/legacy")
    capital_description: Optional[str]
    note: Optional[str] = None


class SleevesOverview(_Base):
    cards: list[SleeveCard]
    as_of: datetime


class EtfHolding(_Base):
    id: int
    symbol: str
    opened_at: datetime
    age_days: float
    qty: float
    entry_price: float
    cost_usd: float
    mode: str
    reason: str
    # ---- live-price fields are Optional: the dashboard's crypto price feed cannot
    # quote equities, so these are null with price_is_stale=True for ETF symbols ----
    last_price: Optional[float]
    market_value: Optional[float]
    unrealized_pnl_usd: Optional[float]
    unrealized_pnl_pct: Optional[float]
    price_is_stale: bool


class EtfSleeve(_Base):
    available: bool = Field(description="False until the ETF bot has created its tables")
    mode: Optional[str]
    priced: bool = Field(description="True if live prices were available for any holding (rare for equities)")
    paper_cash: Optional[float]
    holdings_cost_usd: float
    holdings_market_value: Optional[float]
    equity_estimate: Optional[float] = Field(description="paper_cash + holdings (market value if priced, else cost)")
    realized_pnl_usd: float
    open_positions: int
    deployable_capital: Optional[float]
    capital_source: Optional[str]
    capital_description: Optional[str]
    last_rebalance: Optional[str]
    regime: Optional[str] = Field(description="dual-momentum regime label, when the selector emits one")
    holdings: list[EtfHolding]
    as_of: datetime


class CarryPair(_Base):
    id: int
    asset: str
    opened_at: datetime
    age_hours: float
    notional_usd: float
    capital_usd: float
    spot_qty: float
    spot_entry: float
    perp_qty: float
    perp_entry: float
    funding_accrued_usd: float = Field(description="funding income booked so far on this open pair (unrealized)")
    low_reads: int = Field(description="consecutive polls below the hold threshold (unwind confirmation count)")
    delta_drift_pct: float = Field(description="|spot_qty - perp_qty| / max(qty) * 100; >0 means legs drifted")
    unwind_in_progress: bool = Field(description="exactly one leg closed (a resumable, half-done unwind)")
    mode: str
    reason: str


class CarryFundingPoint(_Base):
    day: str
    amount_usd: float


class CarrySleeve(_Base):
    available: bool = Field(description="False until the carry bot has created its tables")
    mode: Optional[str]
    open_pairs_count: int
    capital_used: float = Field(description="sum of capital_usd across OPEN pairs (spot + futures margin)")
    deployable_capital: Optional[float]
    capital_source: Optional[str]
    capital_description: Optional[str]
    funding_today_usd: float
    funding_total_usd: float
    realized_today_usd: float
    realized_total_usd: float = Field(description="lifetime realized P&L on CLOSED pairs (includes their funding)")
    kill_active: bool = Field(description="carry kill switch engaged (state flag)")
    pairs: list[CarryPair]
    funding_series: list[CarryFundingPoint] = Field(description="daily funding income for a small trend chart")
    as_of: datetime
