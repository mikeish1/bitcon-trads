"""GET /api/risk - all circuit-breaker / loss-limit gauges (mirrors can_open_trade)."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from src.capital_policy import DeployableCapitalPolicy

from web.db import ReadOnlyDB
from web.deps import AppState, auth_read, get_ctx, require_db
from web.models import RiskGauges
from web import queries as q

router = APIRouter(prefix="/api", tags=["risk"])


@router.get("/risk", response_model=RiskGauges, dependencies=[Depends(auth_read)])
def get_risk(ctx: AppState = Depends(get_ctx), db: ReadOnlyDB = Depends(require_db)) -> RiskGauges:
    bases = ctx.universe_bases()
    prices = {b: pq.price for b, pq in ctx.prices.get_prices(bases).items() if pq.price > 0}
    # Use the SAME effective capital policy the bot resolves (env > override > yaml >
    # legacy). On an invalid on-disk override, fall back to the legacy % so the gauge
    # still renders instead of 500-ing.
    try:
        policy = ctx.settings.policy("spot")
    except Exception:
        policy = DeployableCapitalPolicy.from_mapping(
            {"max_pct": ctx.cfg["portfolio"].get("max_total_exposure_pct", 0.90)}, label="spot")
    with db.conn() as c:
        return q.build_risk_gauges(c, ctx.cfg, prices, policy, ctx.regime_on())
