"""GET /api/decisions - the strategy decision log (paginated + filterable)."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query

from web.db import ReadOnlyDB
from web.deps import auth_read, require_db
from web.models import Decision, Page
from web import queries as q

router = APIRouter(prefix="/api", tags=["decisions"])


@router.get("/decisions", response_model=Page[Decision], dependencies=[Depends(auth_read)])
def list_decisions(
    db: ReadOnlyDB = Depends(require_db),
    limit: int = Query(50, ge=1, le=200),
    cursor: Optional[int] = Query(None),
    symbol: Optional[str] = Query(None, examples=["ETH/USDT"]),
    action: Optional[str] = Query(None, pattern="^(BUY|SELL|HOLD)$"),
) -> Page[Decision]:
    with db.conn() as c:
        return q.query_decisions(c, limit=limit, cursor=cursor, symbol=symbol, action=action)
