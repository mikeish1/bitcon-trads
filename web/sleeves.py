"""
Read-only query + computation layer for the CARRY and ETF sleeves.

The spot bot writes `state`/`trades`/`decisions`; the carry and ETF bots write
their OWN tables in the SAME SQLite file that `src/run_all.py` shares across all
three children:

    carry  -> carry_positions, carry_funding, carry_state   (delta-neutral pairs)
    etf    -> etf_positions, etf_state                       (long-only top-K / static)

This module reads those tables READ-ONLY, exactly like `web/queries.py` reads the
spot tables, so the dashboard can finally surface every strategy the supervisor
runs instead of just spot (architecture §14: "the same models generalize to a
per-sleeve view by filtering on mode/symbol namespace").

It deliberately does NOT import `EtfRiskManager` / `CarryRiskManager`: their
`__init__` opens a READ-WRITE connection and runs `CREATE TABLE` / `ALTER TABLE`,
which would violate the dashboard's load-bearing read-only guarantee. Instead we
read the documented schema via raw SQL on the read-only connection and degrade
gracefully when a sleeve has never run (its tables are simply absent).

No live prices are needed: ETF holdings are equities the dashboard's public crypto
feed cannot quote (so MTM falls back to cost and is flagged), and carry pairs are
delta-neutral (price P&L ~= 0 by construction; funding is the real driver).
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional

from web import queries as q
from web.models import (
    CarryFundingPoint,
    CarryPair,
    CarrySleeve,
    EtfHolding,
    EtfSleeve,
    SleeveCard,
    SleevesOverview,
)

# Hardcoded table names (never user input) used in a couple of f-string queries.
_ETF_STATE = "etf_state"
_CARRY_STATE = "carry_state"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _state_get(conn: sqlite3.Connection, table: str, key: str) -> Optional[str]:
    if not _has_table(conn, table):
        return None
    row = conn.execute(f"SELECT value FROM {table} WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def _state_float(conn: sqlite3.Connection, table: str, key: str,
                 default: Optional[float] = None) -> Optional[float]:
    v = _state_get(conn, table, key)
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def _latest_mode(conn: sqlite3.Connection, table: str) -> Optional[str]:
    """Mode label from the most recent row of a positions table (any status)."""
    row = conn.execute(f"SELECT mode FROM {table} ORDER BY id DESC LIMIT 1").fetchone()
    return row["mode"].upper() if row and row["mode"] else None


def _cap_source(cap: Any) -> Optional[str]:
    return cap.get("source") if isinstance(cap, dict) else None


def _cap_desc(cap: Any) -> Optional[str]:
    return cap.get("description") if isinstance(cap, dict) else None


def _deployable(pol: Any, equity: Optional[float], cash: Optional[float]) -> Optional[float]:
    """Best-effort deployable-capital envelope; None if the policy can't be evaluated."""
    if pol is None or equity is None:
        return None
    try:
        return round(float(pol.deployable_capital(equity, cash if cash is not None else 0.0)), 2)
    except Exception:  # pragma: no cover - defensive; never break a read
        return None


# --------------------------------------------------------------------------- #
# ETF sleeve                                                                   #
# --------------------------------------------------------------------------- #
def build_etf_sleeve(conn: sqlite3.Connection, prices: dict[str, float],
                     cap: Any, pol: Any) -> EtfSleeve:
    if not _has_table(conn, "etf_positions"):
        return EtfSleeve(
            available=False, mode=None, priced=False, paper_cash=None,
            holdings_cost_usd=0.0, holdings_market_value=None, equity_estimate=None,
            realized_pnl_usd=0.0, open_positions=0, deployable_capital=None,
            capital_source=_cap_source(cap), capital_description=_cap_desc(cap),
            last_rebalance=None, regime=None, holdings=[], as_of=_utcnow())

    open_rows = conn.execute(
        "SELECT * FROM etf_positions WHERE status='OPEN' ORDER BY id DESC").fetchall()
    holdings: list[EtfHolding] = []
    cost_total = 0.0
    mv_total = 0.0
    priced_any = False
    for p in open_rows:
        sym = p["symbol"]
        base = q._base_of(sym)  # equities have no "/", so base == symbol
        qty = float(p["qty"] or 0.0)
        entry = float(p["entry_price"] or 0.0)
        cost = float(p["cost_usd"] or entry * qty)
        cost_total += cost
        last = prices.get(base)
        priced = last is not None and last > 0
        priced_any = priced_any or priced
        mv = qty * last if priced else None
        if mv is not None:
            mv_total += mv
        upnl = (mv - cost) if mv is not None else None
        upnl_pct = (upnl / cost * 100.0) if (upnl is not None and cost) else None
        opened = q._parse_ts(p["opened_at"])
        age_days = ((_utcnow() - opened).total_seconds() / 86400.0) if opened else 0.0
        holdings.append(EtfHolding(
            id=int(p["id"]), symbol=sym, opened_at=opened or _utcnow(),
            age_days=round(age_days, 2), qty=qty, entry_price=entry,
            cost_usd=round(cost, 2), mode=(p["mode"] or "").upper() or "SIM",
            reason=p["reason"] or "",
            last_price=last if priced else None,
            market_value=round(mv, 2) if mv is not None else None,
            unrealized_pnl_usd=round(upnl, 2) if upnl is not None else None,
            unrealized_pnl_pct=round(upnl_pct, 2) if upnl_pct is not None else None,
            price_is_stale=not priced,
        ))

    realized = conn.execute(
        "SELECT COALESCE(SUM(realized_pnl_usd),0) p FROM etf_positions "
        "WHERE status='CLOSED' AND realized_pnl_usd IS NOT NULL").fetchone()["p"]
    paper_cash = _state_float(conn, _ETF_STATE, "paper_cash", None)
    holdings_mv = round(mv_total, 2) if priced_any else None
    equity_est = None
    if paper_cash is not None:
        equity_est = round(paper_cash + (mv_total if priced_any else cost_total), 2)
    return EtfSleeve(
        available=True, mode=_latest_mode(conn, "etf_positions"), priced=priced_any,
        paper_cash=round(paper_cash, 2) if paper_cash is not None else None,
        holdings_cost_usd=round(cost_total, 2), holdings_market_value=holdings_mv,
        equity_estimate=equity_est, realized_pnl_usd=round(float(realized), 2),
        open_positions=len(open_rows),
        deployable_capital=_deployable(pol, equity_est, paper_cash),
        capital_source=_cap_source(cap), capital_description=_cap_desc(cap),
        last_rebalance=_state_get(conn, _ETF_STATE, "etf_last_rebalance"),
        regime=_state_get(conn, _ETF_STATE, "etf_regime"),
        holdings=holdings, as_of=_utcnow())


# --------------------------------------------------------------------------- #
# Carry sleeve                                                                 #
# --------------------------------------------------------------------------- #
def build_carry_sleeve(conn: sqlite3.Connection, cap: Any, pol: Any) -> CarrySleeve:
    if not _has_table(conn, "carry_positions"):
        return CarrySleeve(
            available=False, mode=None, open_pairs_count=0, capital_used=0.0,
            deployable_capital=None, capital_source=_cap_source(cap),
            capital_description=_cap_desc(cap), funding_today_usd=0.0,
            funding_total_usd=0.0, realized_today_usd=0.0, realized_total_usd=0.0,
            kill_active=False, pairs=[], funding_series=[], as_of=_utcnow())

    today = _utcnow().date().isoformat()
    open_rows = conn.execute(
        "SELECT * FROM carry_positions WHERE status='OPEN' ORDER BY id DESC").fetchall()
    pairs: list[CarryPair] = []
    for p in open_rows:
        sq = float(p["spot_qty"] or 0.0)
        pq = float(p["perp_qty"] or 0.0)
        ref = max(sq, pq, 1e-12)
        opened = q._parse_ts(p["opened_at"])
        age_h = ((_utcnow() - opened).total_seconds() / 3600.0) if opened else 0.0
        pairs.append(CarryPair(
            id=int(p["id"]), asset=p["asset"], opened_at=opened or _utcnow(),
            age_hours=round(age_h, 2),
            notional_usd=round(float(p["notional_usd"] or 0.0), 2),
            capital_usd=round(float(p["capital_usd"] or 0.0), 2),
            spot_qty=sq, spot_entry=float(p["spot_entry"] or 0.0),
            perp_qty=pq, perp_entry=float(p["perp_entry"] or 0.0),
            funding_accrued_usd=round(float(p["funding_accrued_usd"] or 0.0), 4),
            low_reads=int(p["low_reads"] or 0),
            delta_drift_pct=round(abs(sq - pq) / ref * 100.0, 3),
            unwind_in_progress=bool(p["perp_closed"]) != bool(p["spot_closed"]),
            mode=(p["mode"] or "").upper() or "SIM", reason=p["reason"] or "",
        ))

    capital_used = conn.execute(
        "SELECT COALESCE(SUM(capital_usd),0) c FROM carry_positions WHERE status='OPEN'"
    ).fetchone()["c"]
    realized_total = conn.execute(
        "SELECT COALESCE(SUM(realized_pnl_usd),0) p FROM carry_positions WHERE status='CLOSED'"
    ).fetchone()["p"]
    realized_today = conn.execute(
        "SELECT COALESCE(SUM(realized_pnl_usd),0) p FROM carry_positions "
        "WHERE status='CLOSED' AND substr(closed_at,1,10)=?", (today,)).fetchone()["p"]

    funding_today = funding_total = 0.0
    series: list[CarryFundingPoint] = []
    if _has_table(conn, "carry_funding"):
        funding_total = conn.execute(
            "SELECT COALESCE(SUM(amount_usd),0) a FROM carry_funding").fetchone()["a"]
        funding_today = conn.execute(
            "SELECT COALESCE(SUM(amount_usd),0) a FROM carry_funding WHERE substr(ts,1,10)=?",
            (today,)).fetchone()["a"]
        srows = conn.execute(
            "SELECT substr(ts,1,10) d, COALESCE(SUM(amount_usd),0) a FROM carry_funding "
            "GROUP BY d ORDER BY d ASC").fetchall()
        series = [CarryFundingPoint(day=r["d"], amount_usd=round(float(r["a"]), 4))
                  for r in srows if r["d"]]

    return CarrySleeve(
        available=True, mode=_latest_mode(conn, "carry_positions"),
        open_pairs_count=len(open_rows), capital_used=round(float(capital_used), 2),
        deployable_capital=None,  # carry tracks no equity; the description carries the cap
        capital_source=_cap_source(cap), capital_description=_cap_desc(cap),
        funding_today_usd=round(float(funding_today), 4),
        funding_total_usd=round(float(funding_total), 4),
        realized_today_usd=round(float(realized_today), 2),
        realized_total_usd=round(float(realized_total), 2),
        kill_active=_state_get(conn, _CARRY_STATE, "carry_kill") == "1",
        pairs=pairs, funding_series=series, as_of=_utcnow())


# --------------------------------------------------------------------------- #
# Cross-sleeve overview                                                        #
# --------------------------------------------------------------------------- #
def build_overview(conn: sqlite3.Connection, cfg: dict[str, Any],
                   prices: dict[str, float], settings: Any) -> SleevesOverview:
    """One headline card per sleeve. `settings` is the CapitalSettingsService."""
    cards: list[SleeveCard] = []

    # --- spot (reuses the exact spot equity/realized math) ---
    spot_cap = settings.get("spot")
    if _has_table(conn, "trades"):
        equity, _cash, _ov, _ = q.compute_equity(conn, cfg, prices)
        spot_open = conn.execute("SELECT COUNT(*) c FROM trades WHERE status='OPEN'").fetchone()["c"]
        spot_realized = conn.execute(
            "SELECT COALESCE(SUM(pnl_usd),0) p FROM trades "
            "WHERE status='CLOSED' AND pnl_usd IS NOT NULL").fetchone()["p"]
        cards.append(SleeveCard(
            key="spot", label="Spot trend-following", active=True,
            mode=q.mode_badge(cfg).mode, open_positions=int(spot_open),
            primary_value_usd=round(equity, 2), primary_label="Equity",
            realized_pnl_usd=round(float(spot_realized), 2),
            capital_source=_cap_source(spot_cap), capital_description=_cap_desc(spot_cap)))
    else:
        cards.append(SleeveCard(
            key="spot", label="Spot trend-following", active=False,
            mode=q.mode_badge(cfg).mode, open_positions=0, primary_value_usd=None,
            primary_label="Equity", realized_pnl_usd=None,
            capital_source=_cap_source(spot_cap), capital_description=_cap_desc(spot_cap),
            note="Spot bot has not run yet"))

    # --- etf ---
    etf = build_etf_sleeve(conn, prices, settings.get("etf"), _safe_policy(settings, "etf"))
    cards.append(SleeveCard(
        key="etf", label="ETF momentum",
        active=etf.available and (etf.open_positions > 0 or etf.realized_pnl_usd != 0
                                  or etf.paper_cash is not None),
        mode=etf.mode, open_positions=etf.open_positions,
        primary_value_usd=(etf.equity_estimate if etf.equity_estimate is not None
                           else etf.holdings_cost_usd),
        primary_label="Equity (est.)" if etf.equity_estimate is not None else "Holdings cost",
        realized_pnl_usd=etf.realized_pnl_usd,
        capital_source=etf.capital_source, capital_description=etf.capital_description,
        note=None if etf.available else "ETF sleeve has not run yet"))

    # --- carry --- (realized_total already includes funding on closed pairs) ---
    carry = build_carry_sleeve(conn, settings.get("carry"), None)
    cards.append(SleeveCard(
        key="carry", label="Funding carry",
        active=carry.available and (carry.open_pairs_count > 0
                                    or carry.funding_total_usd != 0
                                    or carry.realized_total_usd != 0),
        mode=carry.mode, open_positions=carry.open_pairs_count,
        primary_value_usd=carry.capital_used, primary_label="Capital used",
        realized_pnl_usd=carry.realized_total_usd,
        capital_source=carry.capital_source, capital_description=carry.capital_description,
        note=None if carry.available else "Carry sleeve has not run yet"))

    return SleevesOverview(cards=cards, as_of=_utcnow())


def _safe_policy(settings: Any, sleeve: str) -> Any:
    try:
        return settings.policy(sleeve)
    except Exception:  # pragma: no cover - invalid/absent policy -> no deployable number
        return None
