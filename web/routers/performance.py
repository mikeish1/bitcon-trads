"""
Performance analytics endpoints (architecture §6.2):
  GET /api/performance/equity      - equity + drawdown series (from equity_snapshots)
  GET /api/performance/stats       - win rate, profit factor, expectancy, max DD
  GET /api/performance/attribution - realized PnL per coin
  GET /api/performance/regime      - PnL split: BTC regime ON vs OFF
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query

from web.db import ReadOnlyDB
from web.deps import auth_read, require_db
from web.models import CoinAttribution, EquitySeries, PerformanceStats, RegimeSplit
from web import queries as q

router = APIRouter(prefix="/api/performance", tags=["performance"])

_RANGE_DAYS = {"7d": 7, "30d": 30, "90d": 90, "all": None}


def _since_iso(range_: str) -> str | None:
    days = _RANGE_DAYS.get(range_)
    if not days:
        return None
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


@router.get("/equity", response_model=EquitySeries, dependencies=[Depends(auth_read)])
def equity_series(db: ReadOnlyDB = Depends(require_db),
                  range: str = Query("30d", pattern="^(7d|30d|90d|all)$"),
                  max_points: int = Query(600, ge=50, le=5000)) -> EquitySeries:
    has = db.table_exists("equity_snapshots")
    with db.conn() as c:
        return q.build_equity_series(c, has, since_iso=_since_iso(range), max_points=max_points)


@router.get("/stats", response_model=PerformanceStats, dependencies=[Depends(auth_read)])
def stats(db: ReadOnlyDB = Depends(require_db)) -> PerformanceStats:
    has = db.table_exists("equity_snapshots")
    with db.conn() as c:
        return q.build_performance_stats(c, has)


@router.get("/attribution", response_model=list[CoinAttribution], dependencies=[Depends(auth_read)])
def attribution(db: ReadOnlyDB = Depends(require_db)) -> list[CoinAttribution]:
    with db.conn() as c:
        return q.build_attribution(c)


@router.get("/regime", response_model=RegimeSplit, dependencies=[Depends(auth_read)])
def regime(db: ReadOnlyDB = Depends(require_db)) -> RegimeSplit:
    has = db.table_exists("equity_snapshots")
    with db.conn() as c:
        return q.build_regime_split(c, has)
