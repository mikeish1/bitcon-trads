"""GET /api/summary - the Overview KPI header (equity, returns, mode, win rate)."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from web.db import ReadOnlyDB
from web.deps import AppState, auth_read, get_ctx, require_db
from web.models import KpiSummary
from web import queries as q

router = APIRouter(prefix="/api", tags=["overview"])


@router.get("/summary", response_model=KpiSummary, dependencies=[Depends(auth_read)])
def get_summary(ctx: AppState = Depends(get_ctx), db: ReadOnlyDB = Depends(require_db)) -> KpiSummary:
    bases = ctx.universe_bases()
    quotes = ctx.prices.get_prices(bases)
    prices = {b: pq.price for b, pq in quotes.items() if pq.price > 0}
    price_age = ctx.prices.max_age_seconds(bases)
    with db.conn() as c:
        return q.build_summary(c, ctx.cfg, prices, price_age)
