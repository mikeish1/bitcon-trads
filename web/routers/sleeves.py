"""
Sleeve coverage endpoints - the carry & ETF strategies that src/run_all.py runs
alongside spot into the SAME database, previously invisible to the dashboard.

  GET /api/sleeves        - one headline card per sleeve (spot / carry / etf)
  GET /api/sleeves/etf    - ETF holdings, realized P&L, rebalance state
  GET /api/sleeves/carry  - open delta-neutral pairs, funding income, kill switch

All read-only (raw SQL on the shared read-only connection); no order surface and
no instantiation of the sleeves' read-write risk managers. See web/sleeves.py.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends

from web.db import ReadOnlyDB
from web.deps import AppState, auth_read, get_ctx, require_db
from web.models import CarrySleeve, EtfSleeve, SleevesOverview
from web import sleeves as s

router = APIRouter(prefix="/api/sleeves", tags=["sleeves"])


def _live_prices(ctx: AppState) -> dict[str, float]:
    """Crypto spot prices keyed by base asset (used for ETF MTM when a holding
    happens to be a crypto symbol; equities simply won't resolve and fall back to
    cost - handled in web/sleeves.py)."""
    return {b: pq.price for b, pq in ctx.prices.get_prices(ctx.universe_bases()).items()
            if pq.price > 0}


def _policy(ctx: AppState, sleeve: str) -> Optional[Any]:
    return s._safe_policy(ctx.settings, sleeve)


@router.get("", response_model=SleevesOverview, dependencies=[Depends(auth_read)])
def get_overview(ctx: AppState = Depends(get_ctx),
                 db: ReadOnlyDB = Depends(require_db)) -> SleevesOverview:
    prices = _live_prices(ctx)
    with db.conn() as c:
        return s.build_overview(c, ctx.cfg, prices, ctx.settings)


@router.get("/etf", response_model=EtfSleeve, dependencies=[Depends(auth_read)])
def get_etf(ctx: AppState = Depends(get_ctx),
            db: ReadOnlyDB = Depends(require_db)) -> EtfSleeve:
    prices = _live_prices(ctx)
    with db.conn() as c:
        return s.build_etf_sleeve(c, prices, ctx.settings.get("etf"), _policy(ctx, "etf"))


@router.get("/carry", response_model=CarrySleeve, dependencies=[Depends(auth_read)])
def get_carry(ctx: AppState = Depends(get_ctx),
              db: ReadOnlyDB = Depends(require_db)) -> CarrySleeve:
    with db.conn() as c:
        return s.build_carry_sleeve(c, ctx.settings.get("carry"), _policy(ctx, "carry"))
