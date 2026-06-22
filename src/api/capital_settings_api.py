"""
Optional REST surface for the deployable-capital limit (frontend-ready).

This is a thin, OPTIONAL adapter over :class:`CapitalSettingsService`. It is NOT
imported by any trading bot and adds NO hard dependency: FastAPI is imported
lazily inside ``create_app`` so the bots run on the lean base requirements. The
settings service already returns structured, machine-readable results, so this
layer is deliberately trivial - a future frontend can also talk to the service
directly if a different framework is preferred.

Endpoints
---------
  GET    /capital-limits                -> effective policy for every sleeve
  GET    /capital-limits/schema         -> field metadata for rendering a form
  GET    /capital-limits/{sleeve}       -> effective policy for one sleeve
  PUT    /capital-limits/{sleeve}       -> validate + persist a new policy

Run (after `pip install fastapi uvicorn`):
    from src.config import load_config
    from src.api.capital_settings_api import create_app
    app = create_app(load_config())
    # uvicorn module:app  (or uvicorn.run(app, ...))
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

from src.capital_policy import CAPITAL_POLICY_SCHEMA
from src.settings_service import SLEEVES, CapitalSettingsService


def create_app(cfg: Mapping[str, Any], *, service: Optional[CapitalSettingsService] = None):
    """Build a FastAPI app exposing the capital-limit settings. Raises a clear
    error if FastAPI is not installed."""
    try:
        from fastapi import FastAPI, HTTPException
        from pydantic import BaseModel
    except ModuleNotFoundError as exc:  # pragma: no cover - optional dependency
        raise RuntimeError(
            "The capital-settings REST API needs FastAPI. Install it with "
            "`pip install fastapi uvicorn` (it is intentionally not a base "
            "requirement, so the trading bots stay dependency-light)."
        ) from exc

    svc = service or CapitalSettingsService(cfg)
    app = FastAPI(title="Deployable Capital Settings", version="1.0")

    class PolicyUpdate(BaseModel):
        # All optional: a partial update merges over the current effective policy.
        max_pct: Optional[float] = None
        max_usd: Optional[float] = None
        basis: Optional[str] = None
        precedence: Optional[str] = None

    def _require_sleeve(sleeve: str) -> None:
        if sleeve not in SLEEVES:
            raise HTTPException(status_code=404, detail=f"unknown sleeve '{sleeve}'")

    @app.get("/capital-limits")
    def get_all() -> dict[str, Any]:
        return svc.get_all()

    @app.get("/capital-limits/schema")
    def get_schema() -> dict[str, Any]:
        return CAPITAL_POLICY_SCHEMA

    @app.get("/capital-limits/{sleeve}")
    def get_one(sleeve: str) -> dict[str, Any]:
        _require_sleeve(sleeve)
        return svc.get(sleeve)

    @app.put("/capital-limits/{sleeve}")
    def update_one(sleeve: str, body: "PolicyUpdate") -> dict[str, Any]:
        _require_sleeve(sleeve)
        # exclude_unset so a field the client omitted is NOT treated as "clear".
        payload = body.model_dump(exclude_unset=True)
        result = svc.update(payload, sleeve=sleeve, actor="rest-api")
        if not result.get("ok"):
            # 422 with the structured field errors a frontend can render inline.
            raise HTTPException(status_code=422, detail=result["errors"])
        return result

    return app
