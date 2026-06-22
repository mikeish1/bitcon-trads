"""
GET /api/config - read-only effective configuration (secrets redacted).

Loads the SAME merged config the bot uses (`src.config.load_config`, already on
`AppState`) and strips every secret-looking value before serving (web/security.py).
A unit test asserts no secret substring ever appears in this response.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from web.deps import AppState, auth_read, get_ctx
from web.models import ConfigView
from web.queries import mode_badge
from web.security import redact_mapping

router = APIRouter(prefix="/api", tags=["config"])


@router.get("/config", response_model=ConfigView, dependencies=[Depends(auth_read)])
def get_config(ctx: AppState = Depends(get_ctx)) -> ConfigView:
    cfg = ctx.cfg
    safe_runtime, redacted = redact_mapping(cfg.get("runtime", {}), _prefix="runtime.")
    # Capital limits resolved through the audited service (shows source: env/override/yaml).
    capital = ctx.settings.get_all()
    return ConfigView(
        mode=mode_badge(cfg),
        universe=list(cfg.get("universe_symbols", [])),
        strategy=_safe(cfg.get("strategy", {})),
        risk=_safe(cfg.get("risk", {})),
        exits=_safe(cfg.get("exits", {})),
        safety=_safe(cfg.get("safety", {})),
        portfolio=_safe(cfg.get("portfolio", {})),
        market=_safe(cfg.get("market", {})),
        capital_limits=capital,
        redacted_keys=redacted,
    )


def _safe(section: dict) -> dict:
    cleaned, _ = redact_mapping(section)
    return cleaned
