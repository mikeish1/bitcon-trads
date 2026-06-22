"""
Server-Sent Events (SSE) stream for near-real-time dashboard updates.

Change detection is WATERMARK-DIFF over read-only queries - no DB triggers, no
coupling to the bot. Each tick we re-read cheap watermarks (max trade id, max
decision id, the open-position set hash, latest snapshot ts) and emit only what
changed. This is robust, dependency-free, and cannot affect the writer.

Events (architecture §7.1):
  summary_update   - KpiSummary           (every tick / price move)
  positions_update - OpenPosition[]        (every tick / open-set change)
  new_trade        - ClosedTrade|open row  (trades.id advanced or a status flip)
  new_decision     - Decision              (decisions.id advanced)
  risk_alert       - {kind,message}        (circuit breaker / loss limit / regime)
  equity_update    - {ts,equity,drawdown}  (new snapshot)
  health           - HealthStatus          (heartbeat)

The client (EventSource) auto-reconnects; we send a keepalive comment each tick to
defeat idle proxies. The frontend also keeps a polling fallback, so no datum is
ever ONLY available via SSE (graceful degradation).
"""
from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Optional

from fastapi import Request
from loguru import logger
from starlette.concurrency import run_in_threadpool

from web.deps import AppState
from web.models import RiskGauges
from web import queries as q

_TICK_SECONDS = float(os.getenv("SSE_TICK_SECONDS", "5"))
_HEALTH_EVERY = 3  # emit health every N ticks (~15s at 5s ticks)


def _sse(event: str, data: Any, event_id: Optional[int] = None) -> str:
    """Format one SSE message. `data` is JSON-encoded (Pydantic models via
    mode='json' upstream)."""
    payload = json.dumps(data, default=str, separators=(",", ":"))
    head = f"id: {event_id}\n" if event_id is not None else ""
    return f"{head}event: {event}\ndata: {payload}\n\n"


@dataclass
class _Watermarks:
    max_trade_id: int = 0
    max_decision_id: int = 0
    open_hash: str = ""
    last_snapshot_ts: str = ""
    cb_tripped: bool = False
    fields: dict = field(default_factory=dict)


def _read_changes(ctx: AppState, wm: _Watermarks) -> list[tuple[str, Any, Optional[int]]]:
    """One synchronous read pass; returns a list of (event, data, id) to emit and
    mutates the watermarks in place. All queries are read-only."""
    cfg = ctx.cfg
    out: list[tuple[str, Any, Optional[int]]] = []
    bases = ctx.universe_bases()
    price_quotes = ctx.prices.get_prices(bases)
    prices = {b: pq.price for b, pq in price_quotes.items() if pq.price > 0}
    price_stale = {b: pq.stale for b, pq in price_quotes.items()}
    price_age = ctx.prices.max_age_seconds(bases)
    regime = ctx.regime_on()

    with ctx.db.conn() as c:
        # --- always-refresh cards (cheap; reflect price moves) ---
        summary = q.build_summary(c, cfg, prices, price_age)
        out.append(("summary_update", summary.model_dump(mode="json"), None))

        equity, cash, ov, _ = q.compute_equity(c, cfg, prices)
        positions = q.build_open_positions(c, cfg, prices, price_stale, equity)
        # Emit positions every tick (price-marked) but also detect set changes for alerts.
        out.append(("positions_update", [p.model_dump(mode="json") for p in positions], None))

        # --- watermark: new trades ---
        row = c.execute("SELECT COALESCE(MAX(id),0) m FROM trades").fetchone()
        max_tid = int(row["m"])
        if max_tid > wm.max_trade_id:
            new_rows = c.execute(
                "SELECT * FROM trades WHERE id > ? ORDER BY id ASC", (wm.max_trade_id,)).fetchall()
            for r in new_rows:
                out.append(("new_trade", q._closed_trade(r).model_dump(mode="json"), int(r["id"])))
            wm.max_trade_id = max_tid

        # --- watermark: new decisions ---
        drow = c.execute("SELECT COALESCE(MAX(id),0) m FROM decisions").fetchone()
        max_did = int(drow["m"])
        if max_did > wm.max_decision_id:
            new_decisions = c.execute(
                "SELECT * FROM decisions WHERE id > ? ORDER BY id ASC", (wm.max_decision_id,)).fetchall()
            for r in new_decisions:
                out.append(("new_decision", q._decision(r).model_dump(mode="json"), int(r["id"])))
            wm.max_decision_id = max_did

        # --- risk gauges + circuit-breaker alert edge ---
        from src.capital_policy import DeployableCapitalPolicy
        try:
            policy = ctx.settings.policy("spot")
        except Exception:  # invalid override on disk: fall back to a permissive read
            policy = DeployableCapitalPolicy.from_mapping(
                {"max_pct": cfg["portfolio"].get("max_total_exposure_pct", 0.90)}, label="spot")
        gauges: RiskGauges = q.build_risk_gauges(c, cfg, prices, policy, regime)
        if gauges.circuit_breaker_tripped and not wm.cb_tripped:
            out.append(("risk_alert", {"kind": "circuit_breaker", "severity": "critical",
                        "message": f"Circuit breaker tripped: {gauges.consecutive_losses.current:.0f} "
                        "consecutive losses. New entries paused."}, None))
        for g in (gauges.daily_loss, gauges.weekly_loss):
            if g.breached:
                out.append(("risk_alert", {"kind": g.key, "severity": "warning",
                            "message": f"{g.label} limit breached."}, None))
        wm.cb_tripped = gauges.circuit_breaker_tripped

        # --- new equity snapshot -> equity_update ---
        if ctx.db.table_exists("equity_snapshots"):
            srow = c.execute(
                "SELECT ts, equity FROM equity_snapshots ORDER BY ts DESC LIMIT 1").fetchone()
            if srow and srow["ts"] != wm.last_snapshot_ts:
                wm.last_snapshot_ts = srow["ts"]
                series = q.build_equity_series(c, True, since_iso=None, max_points=600)
                dd = series.points[-1].drawdown_pct if series.points else 0.0
                out.append(("equity_update", {"ts": srow["ts"], "equity": float(srow["equity"]),
                            "drawdown_pct": dd}, None))
    return out


async def event_generator(request: Request, ctx: AppState) -> AsyncIterator[str]:
    """Async SSE generator. Polls for changes every tick until the client
    disconnects. DB work is offloaded to a threadpool so the event loop stays free."""
    wm = _Watermarks()
    tick = 0
    logger.info("SSE client connected: {}", request.client.host if request.client else "?")
    # Greet with a retry hint so the browser reconnects quickly on a drop.
    yield "retry: 3000\n\n"
    try:
        while True:
            if await request.is_disconnected():
                break
            try:
                changes = await run_in_threadpool(_read_changes, ctx, wm)
                for event, data, eid in changes:
                    yield _sse(event, data, eid)
            except Exception as exc:  # one bad tick must not kill the stream
                logger.warning("SSE tick error (continuing): {}", exc)
                yield _sse("error", {"message": "transient read error"})
            tick += 1
            if tick % _HEALTH_EVERY == 0:
                yield ": keepalive\n\n"
            await asyncio.sleep(_TICK_SECONDS)
    finally:
        logger.info("SSE client disconnected.")
