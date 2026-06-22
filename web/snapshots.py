"""
Equity-snapshots sampler - the ONLY component in the dashboard that writes.

The bot persists only *current* scalars in `state`; there is no historical equity
series, so an equity-curve / drawdown chart is impossible from existing data. This
sampler periodically computes the marked-to-market equity (exactly as the bot's
PAPER path does) and appends one row to a NEW, ISOLATED table `equity_snapshots`.

Safety (see docs/DASHBOARD_ARCHITECTURE.md §5.3):
  * It writes ONLY to `equity_snapshots`. It NEVER issues DML against `state`,
    `trades`, or `decisions` (asserted by tests). If this sampler dies, the live
    dashboard keeps working - only the historical curve stops extending.
  * It runs as a FastAPI background task (asyncio), off the request path.
  * Writing to the same DB file is safe under WAL (one writer at a time; the bot
    and this sampler rarely write simultaneously and `busy_timeout` covers it).

It also (idempotently, guarded) creates a few additive read-helper indices on the
existing tables. This is opt-out via DASHBOARD_CREATE_INDEXES=false; it changes no
data and is wrapped so a failure is non-fatal.
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional

from loguru import logger
from starlette.concurrency import run_in_threadpool

from web.db import rw_conn
from web.prices import MarketDataClient

_DDL = """
CREATE TABLE IF NOT EXISTS equity_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    equity REAL NOT NULL,
    open_value REAL NOT NULL,
    cash REAL NOT NULL,
    open_positions INTEGER NOT NULL,
    day_return_pct REAL,
    week_return_pct REAL,
    regime_on INTEGER,
    mode TEXT
);
CREATE INDEX IF NOT EXISTS idx_equity_ts ON equity_snapshots(ts);
"""

# Additive, data-neutral indices that speed up history/decision queries. Guarded.
_HELPER_INDICES = (
    "CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status)",
    "CREATE INDEX IF NOT EXISTS idx_trades_closed_at ON trades(closed_at)",
    "CREATE INDEX IF NOT EXISTS idx_decisions_ts ON decisions(ts)",
)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_schema(db_path: str) -> None:
    """Create the snapshots table (always) and helper indices (opt-out). Safe to
    call repeatedly; uses IF NOT EXISTS throughout."""
    with rw_conn(db_path) as c:
        c.executescript(_DDL)
        if os.getenv("DASHBOARD_CREATE_INDEXES", "true").strip().lower() in ("1", "true", "yes", "on"):
            for stmt in _HELPER_INDICES:
                try:
                    c.execute(stmt)
                except sqlite3.OperationalError as exc:  # table may not exist yet on a fresh DB
                    logger.debug("Helper index skipped ({}): {}", stmt, exc)
        c.commit()


def _state_float(c: sqlite3.Connection, key: str, default: float) -> float:
    row = c.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
    try:
        return float(row["value"]) if row else default
    except (TypeError, ValueError):
        return default


def take_snapshot(db_path: str, cfg: dict[str, Any], prices: dict[str, float],
                  regime_on: Optional[bool]) -> Optional[dict[str, Any]]:
    """Compute and INSERT one snapshot row. Returns the row dict (for SSE) or None
    if there is nothing to sample yet. Reads `state`/`trades`; writes ONLY
    `equity_snapshots`."""
    default_capital = cfg["risk"]["default_capital_usd"]
    with rw_conn(db_path) as c:
        # state may not be seeded until the bot's first cycle.
        seeded = c.execute("SELECT 1 FROM state WHERE key='paper_cash'").fetchone()
        if not seeded:
            return None
        open_rows = c.execute(
            "SELECT symbol, qty, entry_price FROM trades WHERE status='OPEN'").fetchall()
        ov = 0.0
        for r in open_rows:
            base = (r["symbol"] or "").split("/")[0]
            ov += float(r["qty"]) * prices.get(base, float(r["entry_price"]))
        cash = _state_float(c, "paper_cash", default_capital)
        equity = cash + ov
        day_start = _state_float(c, "day_start_equity", equity)
        week_start = _state_float(c, "week_start_equity", equity)
        day_ret = (equity / day_start - 1) * 100 if day_start else 0.0
        week_ret = (equity / week_start - 1) * 100 if week_start else 0.0
        mode = "LIVE" if cfg["runtime"]["real_money"] else (
            "PAPER-BROKER" if cfg["runtime"]["place_orders"] else "PAPER")
        row = {
            "ts": _utcnow_iso(), "equity": round(equity, 6), "open_value": round(ov, 6),
            "cash": round(cash, 6), "open_positions": len(open_rows),
            "day_return_pct": round(day_ret, 4), "week_return_pct": round(week_ret, 4),
            "regime_on": None if regime_on is None else int(regime_on), "mode": mode,
        }
        c.execute(
            "INSERT INTO equity_snapshots(ts, equity, open_value, cash, open_positions, "
            "day_return_pct, week_return_pct, regime_on, mode) VALUES(?,?,?,?,?,?,?,?,?)",
            (row["ts"], row["equity"], row["open_value"], row["cash"], row["open_positions"],
             row["day_return_pct"], row["week_return_pct"], row["regime_on"], row["mode"]))
        c.commit()
        return row


class SnapshotSampler:
    """Background asyncio task that samples equity on a fixed interval."""

    def __init__(self, db_path: str, cfg: dict[str, Any], prices: MarketDataClient,
                 regime_fn, interval_seconds: Optional[float] = None) -> None:
        self.db_path = db_path
        self.cfg = cfg
        self.prices = prices
        self.regime_fn = regime_fn  # callable -> Optional[bool]
        self.interval = interval_seconds or float(os.getenv("EQUITY_SNAPSHOT_SECONDS", "60"))
        self.bases = [s.split("/")[0] for s in cfg.get("universe_symbols", [])]
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self.last_row: Optional[dict[str, Any]] = None

    async def start(self) -> None:
        try:
            await run_in_threadpool(ensure_schema, self.db_path)
        except Exception as exc:  # never let schema setup take down the web server
            logger.warning("Snapshot schema setup failed (charts disabled): {}", exc)
            return
        self._task = asyncio.create_task(self._run(), name="equity-snapshot-sampler")
        logger.info("Equity-snapshot sampler started (every {}s).", self.interval)

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                price_map = await run_in_threadpool(self.prices.price_map, self.bases)
                regime = self.regime_fn()
                row = await run_in_threadpool(
                    take_snapshot, self.db_path, self.cfg, price_map, regime)
                if row:
                    self.last_row = row
            except Exception as exc:  # sampler must be resilient; log and continue
                logger.warning("Snapshot tick failed (continuing): {}", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                pass

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
        logger.info("Equity-snapshot sampler stopped.")
