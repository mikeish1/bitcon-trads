"""
Slippage instrumentation: intended-vs-actual fill quality, per fill and aggregate.

Every executor fill (paper or live, buy or sell, limit or market) is recorded to a
dedicated `fills` table in the shared SQLite file, so real-world execution can be
measured against the backtest's idealized assumptions and audited after the fact.

Slippage convention (ADVERSE is positive):
    side_mult   = +1 for a buy, -1 for a sell
    slippage_bps = (fill_price - intended_price) / intended_price * 1e4 * side_mult
    slippage_usd = (fill_price - intended_price) * qty * side_mult

So paying more than intended on a buy, or receiving less than intended on a sell,
both yield POSITIVE slippage (a cost). A passive limit that improves on the signal
price yields NEGATIVE slippage (a gain).

The recorder owns its own connection to the shared DB (WAL + busy_timeout), the
same pattern the carry/ETF risk managers use, so it never contends with the spot
RiskManager's writes. Disable cheaply via execution.slippage_logging_enabled.

Query aggregates from the CLI:
    python -m src.slippage                 # whole history
    python -m src.slippage --days 7        # last 7 days
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional

from loguru import logger


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class SlippageRecorder:
    """Computes per-fill slippage and (when enabled) persists it. Always returns the
    computed bps/USD so the caller can attach them to the fill dict even with
    logging off."""

    def __init__(self, db_path: str, enabled: bool = True, tolerance_bps: float = 50.0):
        self.db_path = db_path
        self.enabled = bool(enabled)
        self.tolerance_bps = float(tolerance_bps)
        self.conn: Optional[sqlite3.Connection] = None
        if self.enabled:
            self.conn = sqlite3.connect(db_path, check_same_thread=False)
            self.conn.row_factory = sqlite3.Row
            if db_path != ":memory:":
                try:
                    self.conn.execute("PRAGMA journal_mode=WAL")
                    self.conn.execute("PRAGMA busy_timeout=5000")
                except sqlite3.OperationalError:
                    pass
            self._init_db()

    def _init_db(self) -> None:
        assert self.conn is not None
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS fills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT, symbol TEXT, side TEXT, order_type TEXT,
                intended_price REAL, fill_price REAL, qty REAL,
                slippage_bps REAL, slippage_usd REAL, fee_usd REAL,
                mode TEXT, reason TEXT
            );
            """
        )
        self.conn.commit()

    @staticmethod
    def compute(side: str, intended_price: float, fill_price: float, qty: float) -> tuple[float, float]:
        """Return (slippage_bps, slippage_usd); adverse is positive. Safe on bad input."""
        if not intended_price or intended_price <= 0 or fill_price <= 0:
            return 0.0, 0.0
        side_mult = 1.0 if side == "buy" else -1.0
        bps = (fill_price - intended_price) / intended_price * 1e4 * side_mult
        usd = (fill_price - intended_price) * qty * side_mult
        return bps, usd

    def record(self, symbol: str, side: str, order_type: str, intended_price: float,
               fill_price: float, qty: float, fee_usd: float = 0.0, mode: str = "",
               reason: str = "") -> dict[str, float]:
        """Compute slippage, persist it (if enabled), warn past tolerance. Returns
        {slippage_bps, slippage_usd}."""
        bps, usd = self.compute(side, intended_price, fill_price, qty)
        if self.enabled and self.conn is not None:
            try:
                self.conn.execute(
                    "INSERT INTO fills(ts, symbol, side, order_type, intended_price, fill_price, "
                    "qty, slippage_bps, slippage_usd, fee_usd, mode, reason) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (_utcnow_iso(), symbol, side, order_type, intended_price, fill_price, qty,
                     bps, usd, fee_usd, mode, reason))
                self.conn.commit()
            except sqlite3.OperationalError as exc:   # never let logging break a fill
                logger.warning("Slippage log write failed (continuing): {}", exc)
        if bps > self.tolerance_bps:
            logger.warning("High slippage {} {} {}: {:+.1f} bps (${:+.2f}) intended {:.4f} -> fill {:.4f}",
                           order_type, side, symbol, bps, usd, intended_price, fill_price)
        return {"slippage_bps": round(bps, 2), "slippage_usd": round(usd, 4)}


# --------------------------------------------------------------------------- #
# Aggregation                                                                  #
# --------------------------------------------------------------------------- #
def slippage_summary(db_path: str, since_iso: Optional[str] = None) -> dict[str, Any]:
    """Aggregate slippage stats overall + per symbol + per order type. Empty dict
    if the `fills` table does not exist yet."""
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.OperationalError:
        return {}
    conn.row_factory = sqlite3.Row
    where, params = "", []
    if since_iso:
        where, params = "WHERE ts >= ?", [since_iso]
    try:
        overall = conn.execute(
            f"SELECT COUNT(*) n, AVG(slippage_bps) avg_bps, MAX(slippage_bps) max_adverse_bps, "
            f"MIN(slippage_bps) best_bps, SUM(slippage_usd) total_usd, SUM(fee_usd) fees_usd "
            f"FROM fills {where}", params).fetchone()
        by_symbol = conn.execute(
            f"SELECT symbol, COUNT(*) n, ROUND(AVG(slippage_bps),2) avg_bps, "
            f"ROUND(MAX(slippage_bps),2) max_adverse_bps, ROUND(SUM(slippage_usd),4) total_usd "
            f"FROM fills {where} GROUP BY symbol ORDER BY avg_bps DESC", params).fetchall()
        by_type = conn.execute(
            f"SELECT order_type, COUNT(*) n, ROUND(AVG(slippage_bps),2) avg_bps "
            f"FROM fills {where} GROUP BY order_type ORDER BY n DESC", params).fetchall()
    except sqlite3.OperationalError:
        conn.close()
        return {}
    conn.close()
    if not overall or overall["n"] == 0:
        return {"fills": 0}
    return {
        "fills": overall["n"],
        "avg_slippage_bps": round(overall["avg_bps"] or 0.0, 2),
        "max_adverse_bps": round(overall["max_adverse_bps"] or 0.0, 2),
        "best_bps": round(overall["best_bps"] or 0.0, 2),
        "total_slippage_usd": round(overall["total_usd"] or 0.0, 4),
        "total_fees_usd": round(overall["fees_usd"] or 0.0, 4),
        "by_symbol": [dict(r) for r in by_symbol],
        "by_order_type": [dict(r) for r in by_type],
    }


def _main() -> None:
    import argparse
    import os

    from src.config import load_config

    ap = argparse.ArgumentParser(description="Slippage report from the fills log.")
    ap.add_argument("--db", type=str, default=None, help="DB path (default: DB_PATH / config).")
    ap.add_argument("--days", type=int, default=0, help="Only the last N days (0 = all).")
    args = ap.parse_args()

    db = args.db or os.getenv("DB_PATH") or load_config()["runtime"]["db_path"]
    since = None
    if args.days > 0:
        from datetime import timedelta
        since = (datetime.now(timezone.utc) - timedelta(days=args.days)).isoformat()
    s = slippage_summary(db, since)
    if not s or s.get("fills", 0) == 0:
        print(f"No fills recorded in {db}" + (f" (last {args.days}d)" if args.days else "") + ".")
        return
    print(f"\nSLIPPAGE SUMMARY ({db}{f', last {args.days}d' if args.days else ''})")
    print("=" * 72)
    print(f"  fills={s['fills']}  avg={s['avg_slippage_bps']:+.2f}bps  "
          f"max_adverse={s['max_adverse_bps']:+.2f}bps  best={s['best_bps']:+.2f}bps")
    print(f"  total slippage ${s['total_slippage_usd']:+.4f}  total fees ${s['total_fees_usd']:.4f}")
    print("  by order type: " + ", ".join(f"{r['order_type']}={r['avg_bps']:+.1f}bps(n={r['n']})"
                                           for r in s["by_order_type"]))
    print("  by symbol:")
    for r in s["by_symbol"]:
        print(f"    {r['symbol']:<12} n={r['n']:<4} avg={r['avg_bps']:+.2f}bps  "
              f"max_adverse={r['max_adverse_bps']:+.2f}bps  ${r['total_usd']:+.4f}")
    print("=" * 72)


if __name__ == "__main__":
    import os
    import sys
    _ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _ROOT not in sys.path:
        sys.path.insert(0, _ROOT)
    _main()
