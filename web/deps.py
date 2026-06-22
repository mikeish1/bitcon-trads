"""
Dependency-injection wiring and shared application state.

`AppState` holds the process-wide singletons (config, read-only DB handle, public
price client, capital-settings service, regime cache). It is attached to
`app.state` in `web/server.py` and pulled into endpoints via the `Provide*`
FastAPI dependencies below.

Integration points with the existing `src/` package (all read-only, all safe):
  * `src.config.load_config`               - pure config loader (yaml + env merge)
  * `src.settings_service.CapitalSettingsService` - the audited capital-limit owner
  * `src.capital_policy.DeployableCapitalPolicy`  - pure value object for the cap

None of these construct a RiskManager/Executor or touch the trading write path.
"""
from __future__ import annotations

import time
from typing import Any, Optional

from fastapi import Depends, Header, HTTPException, Request, status
from loguru import logger

# --- Safe, minimal imports from the trading package (read-only helpers only) ---
from src.config import load_config
from src.settings_service import CapitalSettingsService

from web.db import ReadOnlyDB
from web.prices import MarketDataClient
from web.security import get_token


class AppState:
    """Container for process-wide singletons, attached to `app.state.ctx`."""

    def __init__(self, cfg: Optional[dict[str, Any]] = None) -> None:
        # load_config() is pure (reads yaml + env, calls load_dotenv once). We cache
        # the result for the process; a restart picks up env/yaml changes. The ONLY
        # thing we deliberately re-read live is the capital override file (hot path).
        self.cfg: dict[str, Any] = cfg if cfg is not None else load_config()
        self.db = ReadOnlyDB(self.cfg["runtime"]["db_path"])
        self.prices = MarketDataClient(quote_ccy=self.cfg.get("quote_ccy", "USDT"))
        self.settings = CapitalSettingsService(self.cfg)
        self.started_at = time.time()
        self.token_required = get_token() is not None
        logger.info("Dashboard AppState ready. db={} token_auth={} universe={}",
                    self.db.path, self.token_required, self.cfg.get("universe_symbols"))

    # --- helpers used by several routers ---------------------------------- #
    def universe_bases(self) -> list[str]:
        return [s.split("/")[0] for s in self.cfg.get("universe_symbols", [])]

    def regime_on(self) -> Optional[bool]:
        """Best-effort BTC-regime read for badges. The bot computes this live in
        memory (not persisted), so the dashboard derives it from the SAME public
        price feed + the configured MA period. Returns None when disabled or on any
        data shortfall (never raises)."""
        rc = self.cfg["strategy"].get("btc_regime", {})
        if not rc.get("enabled", False):
            return None
        # We only have spot prices here, not a candle history, so we cannot recompute
        # the MA exactly. Return None (UI shows "regime: unknown from dashboard") to
        # avoid asserting a state we cannot verify read-only without candles.
        return None

    def close(self) -> None:
        self.prices.close()


# --------------------------------------------------------------------------- #
# FastAPI dependencies                                                        #
# --------------------------------------------------------------------------- #
def get_ctx(request: Request) -> AppState:
    ctx: AppState = request.app.state.ctx
    return ctx


def require_db(ctx: AppState = Depends(get_ctx)) -> ReadOnlyDB:
    if not ctx.db.exists():
        # The bot creates the DB on first run; until then, be explicit (503) rather
        # than 500. The frontend renders a "waiting for the bot to start" state.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="trading database not found yet - is the bot running?",
        )
    return ctx.db


def auth_read(
    ctx: AppState = Depends(get_ctx),
    authorization: Optional[str] = Header(default=None),
    x_api_key: Optional[str] = Header(default=None),
) -> None:
    """Read auth. Open if no DASHBOARD_TOKEN is configured; otherwise require a
    matching bearer token or X-API-Key header."""
    token = get_token()
    if token is None:
        return
    if _presented(authorization, x_api_key) == token:
        return
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                        detail="missing or invalid API token",
                        headers={"WWW-Authenticate": "Bearer"})


def auth_write(
    authorization: Optional[str] = Header(default=None),
    x_api_key: Optional[str] = Header(default=None),
) -> str:
    """Write auth for the ONLY mutating route (capital PUT). FAIL-CLOSED: a token
    must be both configured and presented, even if reads are open. Returns a short
    actor id for the audit trail."""
    token = get_token()
    if token is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="capital-limit changes require DASHBOARD_TOKEN to be configured "
                   "on the server (fail-closed on the only mutating action).",
        )
    if _presented(authorization, x_api_key) != token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="missing or invalid API token",
                            headers={"WWW-Authenticate": "Bearer"})
    # Don't leak the token in the audit log; identify the actor generically.
    return "dashboard:token"


def _presented(authorization: Optional[str], x_api_key: Optional[str]) -> Optional[str]:
    if x_api_key:
        return x_api_key.strip()
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return None
