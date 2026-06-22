"""
GET /api/trades         - paginated, filterable, sortable closed-trade history
GET /api/trades/aggregates - footer totals for the current filter
GET /api/trades/{id}    - one trade + the decisions that preceded it
"""
from __future__ import annotations

from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from web.db import ReadOnlyDB
from web.deps import AppState, auth_read, get_ctx, require_db
from web.models import ClosedTrade, Decision, Page, TradeAggregates
from web import queries as q

router = APIRouter(prefix="/api/trades", tags=["history"])


@router.get("", response_model=Page[ClosedTrade], dependencies=[Depends(auth_read)])
def list_trades(
    db: ReadOnlyDB = Depends(require_db),
    limit: int = Query(50, ge=1, le=200),
    cursor: Optional[int] = Query(None, description="keyset cursor: id of last seen row"),
    symbol: Optional[str] = Query(None, examples=["BTC/USDT"]),
    status: str = Query("CLOSED", pattern="^(CLOSED|OPEN)$"),
    date_from: Optional[date] = Query(None),
    date_to: Optional[date] = Query(None),
    sort: str = Query("id:desc", pattern="^(id|closed_at|pnl_usd):(asc|desc)$"),
) -> Page[ClosedTrade]:
    with db.conn() as c:
        return q.query_trades(
            c, limit=limit, cursor=cursor, symbol=symbol, status=status,
            date_from=date_from.isoformat() if date_from else None,
            date_to=date_to.isoformat() if date_to else None, sort=sort)


@router.get("/aggregates", response_model=TradeAggregates, dependencies=[Depends(auth_read)])
def aggregates(
    db: ReadOnlyDB = Depends(require_db),
    symbol: Optional[str] = Query(None),
    date_from: Optional[date] = Query(None),
    date_to: Optional[date] = Query(None),
) -> TradeAggregates:
    with db.conn() as c:
        return q.trade_aggregates(
            c, symbol=symbol,
            date_from=date_from.isoformat() if date_from else None,
            date_to=date_to.isoformat() if date_to else None)


@router.get("/{trade_id}", dependencies=[Depends(auth_read)])
def trade_detail(trade_id: int, ctx: AppState = Depends(get_ctx),
                 db: ReadOnlyDB = Depends(require_db)) -> dict:
    """Trade + its preceding decisions (joined by symbol, on/before the open time)."""
    with db.conn() as c:
        trade = q.get_trade(c, trade_id)
        if trade is None:
            raise HTTPException(status_code=404, detail=f"trade {trade_id} not found")
        decisions = q.decisions_for_symbol_since(
            c, trade.symbol, since_iso=trade.opened_at.isoformat()) if trade.symbol else []
        return {"trade": trade.model_dump(mode="json"),
                "decisions": [d.model_dump(mode="json") for d in decisions]}
