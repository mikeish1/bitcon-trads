"""
Read-only query + computation layer.

Every function here takes an already-open read-only `sqlite3.Connection` and turns
the bot's raw rows into the enriched Pydantic models in `web/models.py`.

Where a number also exists in the bot, the math is copied EXACTLY from
`src.risk_manager.RiskManager` so the dashboard and the bot can never disagree:
  * `_win_rate`  : 0.5 until >= 10 closed, else wins/(wins+losses)
  * day/week return : equity/period_start - 1
  * `daily_stats`, `can_open_trade` gate thresholds
  * equity (paper): paper_cash + sum(qty * price)

This module performs NO writes and imports NO trading runtime (only the pure
`capital_policy` value object for the exposure gauge / simulation).
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional

from src.capital_policy import DeployableCapitalPolicy

from web.models import (
    ClosedTrade,
    CoinAttribution,
    Decision,
    EquityPoint,
    EquitySeries,
    GaugeValue,
    KpiSummary,
    ModeBadge,
    OpenPosition,
    PerformanceStats,
    Page,
    RegimeBucket,
    RegimeSplit,
    RiskGauges,
    TradeAggregates,
)

_WIN_RATE_MIN_SAMPLE = 10  # matches RiskManager._win_rate


# --------------------------------------------------------------------------- #
# Low-level helpers                                                           #
# --------------------------------------------------------------------------- #
def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _base_of(symbol: Optional[str]) -> str:
    return (symbol or "").split("/")[0]


def _parse_ts(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _state_get(conn: sqlite3.Connection, key: str) -> Optional[str]:
    row = conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def _state_float(conn: sqlite3.Connection, key: str, default: float = 0.0) -> float:
    v = _state_get(conn, key)
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def _state_int(conn: sqlite3.Connection, key: str, default: int = 0) -> int:
    return int(_state_float(conn, key, default))


def _win_rate(wins: int, losses: int) -> float:
    total = wins + losses
    return 0.5 if total < _WIN_RATE_MIN_SAMPLE else wins / total


def mode_badge(cfg: dict[str, Any]) -> ModeBadge:
    rt = cfg["runtime"]
    if rt["real_money"]:
        mode = "LIVE"
    elif rt["place_orders"]:
        mode = "PAPER-BROKER"
    else:
        mode = "PAPER"
    return ModeBadge(
        mode=mode,
        real_money=bool(rt["real_money"]),
        place_orders=bool(rt["place_orders"]),
        exchange_id=str(rt["exchange_id"]),
        quote_ccy=str(cfg.get("quote_ccy", "USDT")),
    )


# --------------------------------------------------------------------------- #
# Equity / cash (mirrors RiskManager.current_equity in the PAPER case)        #
# --------------------------------------------------------------------------- #
def open_positions_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM trades WHERE status='OPEN' ORDER BY id DESC").fetchall())


def open_value(rows: list[sqlite3.Row], prices: dict[str, float]) -> float:
    """Mark-to-market of open positions (prices keyed by base asset); falls back to
    entry_price when a price is missing - identical to RiskManager.open_value."""
    total = 0.0
    for p in rows:
        base = _base_of(p["symbol"])
        total += p["qty"] * prices.get(base, p["entry_price"])
    return total


def compute_equity(conn: sqlite3.Connection, cfg: dict[str, Any],
                   prices: dict[str, float]) -> tuple[float, float, float, str]:
    """Return (equity, cash, open_value, basis_label).

    For the default PAPER mode this is exact: cash = paper_cash ledger, equity =
    cash + MTM(open positions). In broker mode the dashboard holds no exchange
    keys, so it cannot read the live quote balance; it falls back to the paper
    ledger value and flags the basis as 'approx'. Documented limitation.
    """
    default_capital = cfg["risk"]["default_capital_usd"]
    rows = open_positions_rows(conn)
    ov = open_value(rows, prices)
    cash = _state_float(conn, "paper_cash", default_capital)
    basis = "paper_ledger" if not cfg["runtime"]["uses_broker"] else "approx"
    return cash + ov, cash, ov, basis


# --------------------------------------------------------------------------- #
# Positions                                                                   #
# --------------------------------------------------------------------------- #
def build_open_positions(conn: sqlite3.Connection, cfg: dict[str, Any],
                         prices: dict[str, float], price_stale: dict[str, bool],
                         equity: float) -> list[OpenPosition]:
    per_asset_alloc = cfg["portfolio"].get("per_asset_alloc_pct", 0.30)
    out: list[OpenPosition] = []
    for p in open_positions_rows(conn):
        base = _base_of(p["symbol"])
        entry = float(p["entry_price"])
        qty = float(p["qty"])
        cost = float(p["cost_usd"] or entry * qty)
        last = prices.get(base, entry)
        stale = price_stale.get(base, last == entry and base not in prices)
        mkt = qty * last
        upnl = mkt - cost
        upnl_pct = (upnl / cost * 100.0) if cost else 0.0
        cur_stop = float(p["current_stop"] or p["stop_price"] or 0.0)
        init_stop = float(p["stop_price"] or cur_stop)
        peak = float(p["peak_price"] or entry)
        dist_stop = ((last - cur_stop) / last * 100.0) if last else 0.0
        risk_per_unit = entry - init_stop
        r_mult = ((last - entry) / risk_per_unit) if risk_per_unit > 0 else 0.0
        dd_peak = ((peak - last) / peak * 100.0) if peak else 0.0
        cap = equity * per_asset_alloc
        pct_cap = (mkt / cap * 100.0) if cap > 0 else 0.0
        opened = _parse_ts(p["opened_at"])
        age_h = ((_utcnow() - opened).total_seconds() / 3600.0) if opened else 0.0
        out.append(OpenPosition(
            id=int(p["id"]), symbol=p["symbol"], opened_at=opened or _utcnow(),
            entry_price=entry, qty=qty, cost_usd=cost, initial_stop=init_stop,
            current_stop=cur_stop, peak_price=peak, mode=p["mode"] or "PAPER",
            reason=p["reason"] or "", last_price=last, market_value=round(mkt, 6),
            unrealized_pnl_usd=round(upnl, 4), unrealized_pnl_pct=round(upnl_pct, 4),
            distance_to_stop_pct=round(dist_stop, 4), r_multiple=round(r_mult, 4),
            drawdown_from_peak_pct=round(dd_peak, 4), pct_of_per_asset_cap=round(pct_cap, 4),
            age_hours=round(age_h, 2), price_is_stale=bool(stale),
        ))
    return out


# --------------------------------------------------------------------------- #
# Trade history (keyset pagination + filtering)                               #
# --------------------------------------------------------------------------- #
def _closed_trade(row: sqlite3.Row) -> ClosedTrade:
    entry = float(row["entry_price"])
    qty = float(row["qty"])
    cost = float(row["cost_usd"] or entry * qty)
    exit_price = float(row["exit_price"]) if row["exit_price"] is not None else None
    pnl = float(row["pnl_usd"]) if row["pnl_usd"] is not None else None
    ret = (pnl / cost * 100.0) if (pnl is not None and cost) else None
    opened = _parse_ts(row["opened_at"])
    closed = _parse_ts(row["closed_at"])
    hold = ((closed - opened).total_seconds() / 3600.0) if (opened and closed) else None
    init_stop = float(row["stop_price"]) if row["stop_price"] is not None else None
    r_mult = None
    if exit_price is not None and init_stop is not None and (entry - init_stop) > 0:
        r_mult = (exit_price - entry) / (entry - init_stop)
    return ClosedTrade(
        id=int(row["id"]), symbol=row["symbol"], opened_at=opened or _utcnow(),
        closed_at=closed, entry_price=entry, exit_price=exit_price, qty=qty,
        cost_usd=cost, pnl_usd=pnl, return_pct=round(ret, 4) if ret is not None else None,
        hold_hours=round(hold, 3) if hold is not None else None,
        r_multiple=round(r_mult, 4) if r_mult is not None else None,
        mode=row["mode"] or "PAPER", reason=row["reason"] or "",
    )


def query_trades(conn: sqlite3.Connection, *, limit: int = 50, cursor: Optional[int] = None,
                 symbol: Optional[str] = None, status: str = "CLOSED",
                 date_from: Optional[str] = None, date_to: Optional[str] = None,
                 sort: str = "id:desc") -> Page[ClosedTrade]:
    where = ["status = ?"]
    params: list[Any] = [status]
    if symbol:
        where.append("symbol = ?")
        params.append(symbol)
    if date_from:
        where.append("substr(COALESCE(closed_at, opened_at),1,10) >= ?")
        params.append(date_from)
    if date_to:
        where.append("substr(COALESCE(closed_at, opened_at),1,10) <= ?")
        params.append(date_to)
    # Keyset pagination on id (stable, index-friendly). Only DESC is exposed as a
    # cursor flow; other sorts fall back to id ordering for cursor stability.
    if cursor is not None:
        where.append("id < ?")
        params.append(cursor)
    where_sql = " AND ".join(where)

    rows = conn.execute(
        f"SELECT * FROM trades WHERE {where_sql} ORDER BY id DESC LIMIT ?",
        (*params, limit + 1),
    ).fetchall()
    has_more = len(rows) > limit
    rows = rows[:limit]
    items = [_closed_trade(r) for r in rows]
    next_cursor = items[-1].id if (items and has_more) else None

    total = conn.execute(
        f"SELECT COUNT(*) c FROM trades WHERE {where_sql.split(' AND id <')[0]}",
        tuple(params[: len(params) - (1 if cursor is not None else 0)]),
    ).fetchone()["c"]
    return Page[ClosedTrade](items=items, next_cursor=next_cursor,
                             has_more=has_more, total_estimate=int(total))


def trade_aggregates(conn: sqlite3.Connection, *, symbol: Optional[str] = None,
                     date_from: Optional[str] = None, date_to: Optional[str] = None) -> TradeAggregates:
    where = ["status = 'CLOSED'"]
    params: list[Any] = []
    if symbol:
        where.append("symbol = ?")
        params.append(symbol)
    if date_from:
        where.append("substr(closed_at,1,10) >= ?")
        params.append(date_from)
    if date_to:
        where.append("substr(closed_at,1,10) <= ?")
        params.append(date_to)
    where_sql = " AND ".join(where)
    row = conn.execute(
        f"SELECT COUNT(*) c, COALESCE(SUM(pnl_usd),0) p, "
        f"COALESCE(SUM(CASE WHEN pnl_usd >= 0 THEN 1 ELSE 0 END),0) w, "
        f"COALESCE(SUM(CASE WHEN pnl_usd < 0 THEN 1 ELSE 0 END),0) l "
        f"FROM trades WHERE {where_sql}", tuple(params)).fetchone()
    c, w, l = int(row["c"]), int(row["w"]), int(row["l"])
    wr = (w / (w + l) * 100.0) if (w + l) else 0.0
    return TradeAggregates(count=c, total_pnl_usd=round(float(row["p"]), 4),
                           wins=w, losses=l, win_rate_pct=round(wr, 2))


def get_trade(conn: sqlite3.Connection, trade_id: int) -> Optional[ClosedTrade]:
    row = conn.execute("SELECT * FROM trades WHERE id=?", (trade_id,)).fetchone()
    return _closed_trade(row) if row else None


# --------------------------------------------------------------------------- #
# Decisions                                                                   #
# --------------------------------------------------------------------------- #
def _decision(row: sqlite3.Row) -> Decision:
    return Decision(
        id=int(row["id"]), ts=_parse_ts(row["ts"]) or _utcnow(), symbol=row["symbol"],
        action=row["action"] or "", conviction=int(row["conviction"] or 0),
        consulted_claude=bool(row["consulted_claude"]), reasoning=row["reasoning"] or "",
    )


def query_decisions(conn: sqlite3.Connection, *, limit: int = 50, cursor: Optional[int] = None,
                    symbol: Optional[str] = None, action: Optional[str] = None) -> Page[Decision]:
    where: list[str] = []
    params: list[Any] = []
    if symbol:
        where.append("symbol = ?")
        params.append(symbol)
    if action:
        where.append("action = ?")
        params.append(action)
    base_where = (" WHERE " + " AND ".join(where)) if where else ""
    cur_where = base_where
    cur_params = list(params)
    if cursor is not None:
        cur_where = base_where + (" AND" if where else " WHERE") + " id < ?"
        cur_params.append(cursor)
    rows = conn.execute(
        f"SELECT * FROM decisions{cur_where} ORDER BY id DESC LIMIT ?",
        (*cur_params, limit + 1)).fetchall()
    has_more = len(rows) > limit
    rows = rows[:limit]
    items = [_decision(r) for r in rows]
    next_cursor = items[-1].id if (items and has_more) else None
    total = conn.execute(f"SELECT COUNT(*) c FROM decisions{base_where}", tuple(params)).fetchone()["c"]
    return Page[Decision](items=items, next_cursor=next_cursor,
                          has_more=has_more, total_estimate=int(total))


def decisions_for_symbol_since(conn: sqlite3.Connection, symbol: str, since_iso: str,
                               limit: int = 20) -> list[Decision]:
    rows = conn.execute(
        "SELECT * FROM decisions WHERE symbol=? AND ts >= ? ORDER BY id DESC LIMIT ?",
        (symbol, since_iso, limit)).fetchall()
    return [_decision(r) for r in rows]


# --------------------------------------------------------------------------- #
# KPI summary + daily stats (mirrors RiskManager.daily_stats)                 #
# --------------------------------------------------------------------------- #
def build_summary(conn: sqlite3.Connection, cfg: dict[str, Any], prices: dict[str, float],
                  price_age_seconds: float) -> KpiSummary:
    equity, cash, ov, basis = compute_equity(conn, cfg, prices)
    day_start = _state_float(conn, "day_start_equity", equity)
    week_start = _state_float(conn, "week_start_equity", equity)
    wins, losses = _state_int(conn, "wins"), _state_int(conn, "losses")
    today = _state_get(conn, "day_date") or _utcnow().date().isoformat()
    closed = conn.execute(
        "SELECT COUNT(*) c, COALESCE(SUM(pnl_usd),0) p FROM trades "
        "WHERE status='CLOSED' AND substr(closed_at,1,10)=?", (today,)).fetchone()
    trades_today = conn.execute(
        "SELECT COUNT(*) c FROM trades WHERE substr(opened_at,1,10)=?", (today,)).fetchone()["c"]
    n_open = conn.execute("SELECT COUNT(*) c FROM trades WHERE status='OPEN'").fetchone()["c"]
    # Unrealized PnL = mark-to-market value of open positions minus their entry cost.
    unrealized = ov - sum_open_cost(conn)
    return KpiSummary(
        mode=mode_badge(cfg), equity=round(equity, 2), cash=round(cash, 2),
        open_value=round(ov, 2), unrealized_pnl_usd=round(unrealized, 4),
        day_return_pct=round((equity / day_start - 1) * 100, 4) if day_start else 0.0,
        week_return_pct=round((equity / week_start - 1) * 100, 4) if week_start else 0.0,
        pnl_today_usd=round(float(closed["p"]), 4), open_positions=int(n_open),
        closed_today=int(closed["c"]), trades_today=int(trades_today),
        wins=wins, losses=losses, win_rate_pct=round(_win_rate(wins, losses) * 100, 2),
        consecutive_losses=_state_int(conn, "consecutive_losses"),
        equity_basis=basis, as_of=_utcnow(), price_age_seconds=round(price_age_seconds, 2),
    )


def sum_open_cost(conn: sqlite3.Connection) -> float:
    row = conn.execute("SELECT COALESCE(SUM(cost_usd),0) c FROM trades WHERE status='OPEN'").fetchone()
    return float(row["c"])


# --------------------------------------------------------------------------- #
# Risk gauges (one per can_open_trade gate)                                   #
# --------------------------------------------------------------------------- #
def build_risk_gauges(conn: sqlite3.Connection, cfg: dict[str, Any], prices: dict[str, float],
                      policy: DeployableCapitalPolicy, regime_on: Optional[bool]) -> RiskGauges:
    s, pf = cfg["safety"], cfg["portfolio"]
    equity, cash, ov, _ = compute_equity(conn, cfg, prices)
    day_start = _state_float(conn, "day_start_equity", equity)
    week_start = _state_float(conn, "week_start_equity", equity)
    day_ret = (equity / day_start - 1) if day_start else 0.0
    week_ret = (equity / week_start - 1) if week_start else 0.0
    consec = _state_int(conn, "consecutive_losses")
    today = _state_get(conn, "day_date") or _utcnow().date().isoformat()
    trades_today = conn.execute(
        "SELECT COUNT(*) c FROM trades WHERE substr(opened_at,1,10)=?", (today,)).fetchone()["c"]
    n_open = conn.execute("SELECT COUNT(*) c FROM trades WHERE status='OPEN'").fetchone()["c"]

    daily_limit = s["daily_loss_limit_pct"]
    weekly_limit = s["weekly_loss_limit_pct"]
    max_consec = s["max_consecutive_losses"]
    max_tpd = s["max_trades_per_day"]
    max_conc = pf.get("max_concurrent_positions", 3)
    deployable = float(policy.deployable_capital(equity, cash))

    def _g(key, label, current, limit, breached, plain, math) -> GaugeValue:
        pct = (current / limit) if limit else 0.0
        return GaugeValue(key=key, label=label, current=round(current, 4), limit=round(limit, 4),
                          pct_of_limit=max(0.0, round(pct, 4)), breached=breached,
                          tooltip_plain=plain, tooltip_math=math)

    return RiskGauges(
        daily_loss=_g("daily_loss", "Daily loss", abs(min(day_ret, 0.0)) * 100, daily_limit * 100,
                      day_ret <= -daily_limit,
                      f"Trading pauses for the rest of the UTC day after a {daily_limit:.0%} equity drawdown.",
                      "(equity - day_start_equity) / day_start_equity ≤ "
                      f"-{daily_limit:.2f}"),
        weekly_loss=_g("weekly_loss", "Weekly loss", abs(min(week_ret, 0.0)) * 100, weekly_limit * 100,
                       week_ret <= -weekly_limit,
                       f"Trading pauses for the week after a {weekly_limit:.0%} equity drawdown.",
                       "(equity - week_start_equity) / week_start_equity ≤ "
                       f"-{weekly_limit:.2f}"),
        consecutive_losses=_g("consecutive_losses", "Consecutive losses", consec, max_consec,
                              consec >= max_consec,
                              "A circuit breaker halts new entries after this many losing trades in a row.",
                              f"consecutive_losses ≥ {max_consec}"),
        trades_today=_g("trades_today", "Trades today", trades_today, max_tpd,
                        trades_today >= max_tpd,
                        "Hard cap on the number of new entries opened per UTC day.",
                        f"trades_today ≥ {max_tpd}"),
        concurrent_positions=_g("concurrent_positions", "Concurrent positions", n_open, max_conc,
                                n_open >= max_conc,
                                "The bot never holds more than this many coins at once.",
                                f"open_positions ≥ {max_conc}"),
        total_exposure=_g("total_exposure", "Deployed capital", ov, deployable, ov > deployable + 1e-9,
                          policy.describe().capitalize() + ". The total value of open positions "
                          "may not exceed this envelope.",
                          f"open_value ≤ deployable_capital ({deployable:,.2f})"),
        circuit_breaker_tripped=consec >= max_consec,
        regime_enabled=bool(cfg["strategy"].get("btc_regime", {}).get("enabled", False)),
        regime_on=regime_on, as_of=_utcnow(),
    )


# --------------------------------------------------------------------------- #
# Performance: equity series + drawdown                                       #
# --------------------------------------------------------------------------- #
def build_equity_series(conn: sqlite3.Connection, has_table: bool, *,
                        since_iso: Optional[str], max_points: int = 600) -> EquitySeries:
    if not has_table:
        return EquitySeries(points=[], start_equity=None, end_equity=None,
                            max_drawdown_pct=0.0, downsampled=False, available=False)
    if since_iso:
        rows = conn.execute(
            "SELECT ts, equity FROM equity_snapshots WHERE ts >= ? ORDER BY ts ASC", (since_iso,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT ts, equity FROM equity_snapshots ORDER BY ts ASC").fetchall()
    if not rows:
        return EquitySeries(points=[], start_equity=None, end_equity=None,
                            max_drawdown_pct=0.0, downsampled=False, available=True)

    peak = float("-inf")
    pts: list[EquityPoint] = []
    max_dd = 0.0
    for r in rows:
        eq = float(r["equity"])
        peak = max(peak, eq)
        dd = ((eq - peak) / peak * 100.0) if peak > 0 else 0.0
        max_dd = min(max_dd, dd)
        ts = _parse_ts(r["ts"]) or _utcnow()
        pts.append(EquityPoint(ts=ts, equity=round(eq, 4), drawdown_pct=round(dd, 4)))

    downsampled = False
    if len(pts) > max_points:
        # Uniform decimation preserving first/last (a pragmatic stand-in for LTTB,
        # which is noted as a future enhancement in the architecture).
        stride = len(pts) / max_points
        idx = sorted({0, len(pts) - 1, *(int(i * stride) for i in range(max_points))})
        pts = [pts[i] for i in idx if i < len(pts)]
        downsampled = True

    return EquitySeries(points=pts, start_equity=pts[0].equity, end_equity=pts[-1].equity,
                        max_drawdown_pct=round(max_dd, 4), downsampled=downsampled, available=True)


def build_performance_stats(conn: sqlite3.Connection, has_snapshots: bool) -> PerformanceStats:
    rows = conn.execute(
        "SELECT pnl_usd, cost_usd, opened_at, closed_at FROM trades "
        "WHERE status='CLOSED' AND pnl_usd IS NOT NULL").fetchall()
    pnls = [float(r["pnl_usd"]) for r in rows]
    n = len(pnls)
    wins = [p for p in pnls if p >= 0]
    losses = [p for p in pnls if p < 0]
    gross_profit = sum(wins)
    gross_loss = sum(losses)
    holds = []
    for r in rows:
        o, c = _parse_ts(r["opened_at"]), _parse_ts(r["closed_at"])
        if o and c:
            holds.append((c - o).total_seconds() / 3600.0)
    max_dd = build_equity_series(conn, has_snapshots, since_iso=None).max_drawdown_pct
    return PerformanceStats(
        closed_trades=n, wins=len(wins), losses=len(losses),
        win_rate_pct=round((len(wins) / n * 100.0), 2) if n else 0.0,
        gross_profit_usd=round(gross_profit, 4), gross_loss_usd=round(gross_loss, 4),
        profit_factor=round(gross_profit / abs(gross_loss), 4) if gross_loss else None,
        expectancy_usd=round(sum(pnls) / n, 4) if n else 0.0,
        avg_win_usd=round(gross_profit / len(wins), 4) if wins else 0.0,
        avg_loss_usd=round(gross_loss / len(losses), 4) if losses else 0.0,
        avg_hold_hours=round(sum(holds) / len(holds), 3) if holds else 0.0,
        best_trade_usd=round(max(pnls), 4) if pnls else 0.0,
        worst_trade_usd=round(min(pnls), 4) if pnls else 0.0,
        max_drawdown_pct=max_dd,
    )


def build_attribution(conn: sqlite3.Connection) -> list[CoinAttribution]:
    rows = conn.execute(
        "SELECT symbol, COUNT(*) c, COALESCE(SUM(pnl_usd),0) p, "
        "SUM(CASE WHEN pnl_usd >= 0 THEN 1 ELSE 0 END) w, "
        "SUM(CASE WHEN pnl_usd < 0 THEN 1 ELSE 0 END) l "
        "FROM trades WHERE status='CLOSED' AND pnl_usd IS NOT NULL "
        "GROUP BY symbol ORDER BY p DESC").fetchall()
    by_base: dict[str, dict[str, float]] = {}
    for r in rows:
        base = _base_of(r["symbol"])
        agg = by_base.setdefault(base, {"c": 0, "p": 0.0, "w": 0, "l": 0})
        agg["c"] += int(r["c"]); agg["p"] += float(r["p"])
        agg["w"] += int(r["w"] or 0); agg["l"] += int(r["l"] or 0)
    out = []
    for base, a in sorted(by_base.items(), key=lambda kv: kv[1]["p"], reverse=True):
        tot = a["w"] + a["l"]
        out.append(CoinAttribution(base=base, closed_trades=int(a["c"]),
                   realized_pnl_usd=round(a["p"], 4), wins=int(a["w"]), losses=int(a["l"]),
                   win_rate_pct=round(a["w"] / tot * 100, 2) if tot else 0.0))
    return out


def build_regime_split(conn: sqlite3.Connection, has_snapshots: bool) -> RegimeSplit:
    """Bucket closed trades by the BTC regime state at the time they CLOSED, using
    the nearest preceding equity snapshot's `regime_on` flag. Needs snapshots."""
    if not has_snapshots:
        return RegimeSplit(buckets=[], available=False)
    snaps = conn.execute(
        "SELECT ts, regime_on FROM equity_snapshots WHERE regime_on IS NOT NULL ORDER BY ts ASC"
    ).fetchall()
    if not snaps:
        return RegimeSplit(buckets=[], available=False)
    snap_ts = [s["ts"] for s in snaps]
    snap_on = [bool(s["regime_on"]) for s in snaps]

    import bisect
    buckets: dict[Optional[bool], dict[str, float]] = {True: {"c": 0, "p": 0.0, "w": 0},
                                                       False: {"c": 0, "p": 0.0, "w": 0},
                                                       None: {"c": 0, "p": 0.0, "w": 0}}
    rows = conn.execute(
        "SELECT closed_at, pnl_usd FROM trades WHERE status='CLOSED' AND pnl_usd IS NOT NULL").fetchall()
    for r in rows:
        ca = r["closed_at"]
        regime: Optional[bool] = None
        if ca:
            i = bisect.bisect_right(snap_ts, ca) - 1
            if i >= 0:
                regime = snap_on[i]
        b = buckets[regime]
        b["c"] += 1; b["p"] += float(r["pnl_usd"])
        if float(r["pnl_usd"]) >= 0:
            b["w"] += 1
    label = {True: "Regime ON (BTC uptrend)", False: "Regime OFF (BTC below MA)", None: "Unknown"}
    out = []
    for key in (True, False, None):
        b = buckets[key]
        if b["c"] == 0:
            continue
        out.append(RegimeBucket(regime_on=key, label=label[key], closed_trades=int(b["c"]),
                   realized_pnl_usd=round(b["p"], 4),
                   win_rate_pct=round(b["w"] / b["c"] * 100, 2) if b["c"] else 0.0))
    return RegimeSplit(buckets=out, available=True)
