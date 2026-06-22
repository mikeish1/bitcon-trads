"""
GET /api/health - liveness + bot-activity heartbeat (also the Railway healthcheck).

A read-only observer cannot see the bot's in-memory cycle clock, so the only
liveness signal is the AGE of the newest DB write across `trades`/`decisions`. The
bot uses daily candles and a 15-min poll, and it only logs a decision on a *new*
candle, so writes are legitimately sparse - thresholds are generous and configurable
(HEALTH_STALE_HOURS). This endpoint never requires auth (it must answer the platform
healthcheck) and never 500s.
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends

from web.deps import AppState, get_ctx
from web.models import HealthStatus

router = APIRouter(prefix="/api", tags=["health"])

_STALE_HOURS = float(os.getenv("HEALTH_STALE_HOURS", "26"))   # ~1 daily cycle + margin
_DEGRADED_HOURS = float(os.getenv("HEALTH_DEGRADED_HOURS", "50"))


def _parse(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


@router.get("/health", response_model=HealthStatus)
def health(ctx: AppState = Depends(get_ctx)) -> HealthStatus:
    cfg = ctx.cfg
    now = datetime.now(timezone.utc)
    mode = "LIVE" if cfg["runtime"]["real_money"] else (
        "PAPER-BROKER" if cfg["runtime"]["place_orders"] else "PAPER")
    poll = int(cfg["market"]["poll_seconds"])

    if not ctx.db.exists():
        return HealthStatus(status="starting", db_ok=False, mode=mode, open_positions=0,
                            last_bot_activity_at=None, last_bot_activity_age_seconds=None,
                            last_decision_at=None, last_trade_opened_at=None, poll_seconds=poll,
                            regime_enabled=bool(cfg["strategy"].get("btc_regime", {}).get("enabled")),
                            regime_on=None, circuit_breaker_tripped=False, snapshot_count=0,
                            db_size_bytes=0, server_time=now)

    last_decision = last_trade = None
    n_open = consec = max_consec = snap_count = 0
    db_ok = True
    try:
        with ctx.db.conn() as c:
            row = c.execute("SELECT MAX(ts) m FROM decisions").fetchone()
            last_decision = _parse(row["m"] if row else None)
            row = c.execute(
                "SELECT MAX(COALESCE(closed_at, opened_at)) m FROM trades").fetchone()
            last_trade = _parse(row["m"] if row else None)
            n_open = c.execute("SELECT COUNT(*) c FROM trades WHERE status='OPEN'").fetchone()["c"]
            cr = c.execute("SELECT value FROM state WHERE key='consecutive_losses'").fetchone()
            consec = int(float(cr["value"])) if cr else 0
            if ctx.db.table_exists("equity_snapshots"):
                snap_count = c.execute("SELECT COUNT(*) c FROM equity_snapshots").fetchone()["c"]
    except sqlite3.OperationalError:
        db_ok = False

    max_consec = cfg["safety"]["max_consecutive_losses"]
    candidates = [t for t in (last_decision, last_trade) if t is not None]
    last_activity = max(candidates) if candidates else None
    age = (now - last_activity).total_seconds() if last_activity else None

    if not db_ok:
        status = "stale"
    elif age is None:
        status = "starting"   # bot present but hasn't written yet
    elif age <= _STALE_HOURS * 3600:
        status = "healthy"
    elif age <= _DEGRADED_HOURS * 3600:
        status = "degraded"
    else:
        status = "stale"

    try:
        db_size = Path(ctx.db.path).stat().st_size if ctx.db.path != ":memory:" else 0
    except OSError:
        db_size = 0

    return HealthStatus(
        status=status, db_ok=db_ok, mode=mode, open_positions=int(n_open),
        last_bot_activity_at=last_activity,
        last_bot_activity_age_seconds=round(age, 1) if age is not None else None,
        last_decision_at=last_decision, last_trade_opened_at=last_trade, poll_seconds=poll,
        regime_enabled=bool(cfg["strategy"].get("btc_regime", {}).get("enabled")),
        regime_on=ctx.regime_on(), circuit_breaker_tripped=consec >= max_consec,
        snapshot_count=int(snap_count), db_size_bytes=int(db_size), server_time=now)
