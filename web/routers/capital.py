"""
Deployable-capital limit endpoints - the ONLY mutating surface in the dashboard.

  GET  /api/capital-limits             - effective policy for every sleeve
  GET  /api/capital-limits/schema      - field metadata for rendering a form
  GET  /api/capital-limits/{sleeve}    - effective policy for one sleeve
  POST /api/capital-limits/{sleeve}/simulate - DRY-RUN: resulting capacity, no write
  PUT  /api/capital-limits/{sleeve}    - validate + persist (token REQUIRED)

The GET/PUT logic delegates verbatim to the pre-existing, audited
`CapitalSettingsService` (src/settings_service.py): atomic JSON override write +
audit-log append + machine-readable validation errors. The running bot picks up a
saved change on its next cycle via `RiskManager.maybe_reload_policy()` - no restart.
This router adds ONLY a read-only "simulate" helper so the UI can preview a change
before committing it.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from src.capital_policy import CAPITAL_POLICY_SCHEMA, CapitalPolicyError, DeployableCapitalPolicy
from src.settings_service import SLEEVES

from web.db import ReadOnlyDB
from web.deps import AppState, auth_read, auth_write, get_ctx, require_db
from web.models import CapitalSimulation
from web.security import WRITE_LIMIT, client_ip, limiter
from web import queries as q

router = APIRouter(prefix="/api/capital-limits", tags=["capital"])


class PolicyUpdate(BaseModel):
    """Partial update; omitted fields keep their current effective value. Pass a
    field as null to explicitly CLEAR that cap."""
    max_pct: Optional[float] = None
    max_usd: Optional[float] = None
    basis: Optional[str] = None
    precedence: Optional[str] = None


def _require_sleeve(sleeve: str) -> None:
    if sleeve not in SLEEVES:
        raise HTTPException(status_code=404, detail=f"unknown sleeve '{sleeve}' (valid: {SLEEVES})")


@router.get("", dependencies=[Depends(auth_read)])
def get_all(ctx: AppState = Depends(get_ctx)) -> dict[str, Any]:
    return ctx.settings.get_all()


@router.get("/schema", dependencies=[Depends(auth_read)])
def get_schema() -> dict[str, Any]:
    return CAPITAL_POLICY_SCHEMA


@router.get("/{sleeve}", dependencies=[Depends(auth_read)])
def get_one(sleeve: str, ctx: AppState = Depends(get_ctx)) -> dict[str, Any]:
    _require_sleeve(sleeve)
    return ctx.settings.get(sleeve)


@router.post("/{sleeve}/simulate", response_model=CapitalSimulation,
             dependencies=[Depends(auth_read)])
def simulate(sleeve: str, body: PolicyUpdate, ctx: AppState = Depends(get_ctx),
             db: ReadOnlyDB = Depends(require_db)) -> CapitalSimulation:
    """Preview a candidate policy against current equity/cash/committed WITHOUT
    writing anything. Lets the Config page show 'remaining capacity' before the user
    commits the change."""
    _require_sleeve(sleeve)
    # Merge the candidate over the current effective mapping (same logic the service
    # uses on update), but never persist.
    current, _src = ctx.settings.resolve_mapping(sleeve)
    payload = body.model_dump(exclude_unset=True)
    candidate = dict(current)
    for k in ("max_pct", "max_usd", "basis", "precedence"):
        if k in payload:
            candidate[k] = payload[k]

    bases = ctx.universe_bases()
    prices = {b: pq.price for b, pq in ctx.prices.get_prices(bases).items() if pq.price > 0}
    with db.conn() as c:
        equity, cash, committed, _ = q.compute_equity(c, ctx.cfg, prices)

    try:
        pol = DeployableCapitalPolicy.from_mapping(candidate, label=sleeve)
    except CapitalPolicyError as exc:
        return CapitalSimulation(sleeve=sleeve, valid=False, errors=exc.errors,
                                 equity=round(equity, 2), available_cash=round(cash, 2),
                                 committed=round(committed, 2), deployable_capital=0.0,
                                 remaining_capacity=0.0)
    deployable = float(pol.deployable_capital(equity, cash))
    remaining = float(pol.remaining_capacity(equity, cash, committed))
    exposure_pct = (committed / deployable * 100.0) if deployable > 0 else None
    return CapitalSimulation(
        sleeve=sleeve, valid=True, policy=pol.to_public_dict(), description=pol.describe(),
        equity=round(equity, 2), available_cash=round(cash, 2), committed=round(committed, 2),
        deployable_capital=round(deployable, 2), remaining_capacity=round(remaining, 2),
        current_exposure_pct=round(exposure_pct, 2) if exposure_pct is not None else None)


@router.put("/{sleeve}")
def update_one(sleeve: str, body: PolicyUpdate, request: Request,
               ctx: AppState = Depends(get_ctx), actor: str = Depends(auth_write)) -> dict[str, Any]:
    """Persist a new policy. Token REQUIRED (fail-closed) + rate-limited."""
    _require_sleeve(sleeve)
    client = client_ip(request)   # proxy-aware so the write limiter keys on the real client
    if not limiter.allow(f"capital_put:{client}", *WRITE_LIMIT):
        raise HTTPException(status_code=429, detail="too many capital-limit changes; slow down")

    payload = body.model_dump(exclude_unset=True)
    result = ctx.settings.update(payload, sleeve=sleeve, actor=actor)
    if not result.get("ok"):
        # 422 with the service's structured, field-level errors for inline rendering.
        raise HTTPException(status_code=422, detail=result["errors"])
    return result
