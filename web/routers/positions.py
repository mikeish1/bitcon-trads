"""GET /api/positions - open positions marked to market with live risk fields."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from web.db import ReadOnlyDB
from web.deps import AppState, auth_read, get_ctx, require_db
from web.models import OpenPosition
from web import queries as q

router = APIRouter(prefix="/api", tags=["positions"])


@router.get("/positions", response_model=list[OpenPosition], dependencies=[Depends(auth_read)])
def get_positions(ctx: AppState = Depends(get_ctx),
                  db: ReadOnlyDB = Depends(require_db)) -> list[OpenPosition]:
    bases = ctx.universe_bases()
    quotes = ctx.prices.get_prices(bases)
    prices = {b: pq.price for b, pq in quotes.items() if pq.price > 0}
    stale = {b: pq.stale for b, pq in quotes.items()}
    with db.conn() as c:
        equity, _cash, _ov, _ = q.compute_equity(c, ctx.cfg, prices)
        return q.build_open_positions(c, ctx.cfg, prices, stale, equity)
