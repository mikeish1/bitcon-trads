"""
Carry risk manager (delta-neutral pairs), SQLite-backed.

Tracks one paired position per asset (long spot + short perp), enforces the
capital sleeve / per-asset / min-notional caps, accrues funding each poll, runs a
daily realized-loss circuit breaker, and exposes a kill switch. Lives in its OWN
tables in the SAME database file the spot bot uses, so nothing collides.

Capital model: a cross-venue carry needs ~N to buy spot AND N/leverage as futures
margin, so capital used per pair = N x (1 + 1/leverage). The sleeve is the TOTAL
capital across both venues. (See docs/CARRY_ARBITRAGE.md §5.)
"""
from __future__ import annotations

import os
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any, Optional

from loguru import logger

from .types import PairFill

_YEAR_SECONDS = 365.0 * 24.0 * 3600.0


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class CarryRiskManager:
    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg
        c = cfg["carry"]
        self.assets: list[str] = list(c["assets"])
        self.sleeve = float(c["capital"]["sleeve_usd"])
        self.per_asset_cap = float(c["capital"]["per_asset_cap_usd"])
        self.min_notional = float(c["capital"]["min_notional_usd"])
        self.target_leverage = max(float(c["risk"]["target_leverage"]), 0.01)
        self.daily_loss_limit = float(c["risk"]["daily_loss_limit_usd"])
        self.delta_tol = float(c["risk"]["delta_tolerance_pct"])
        self.mode = cfg["carry_runtime"]["mode"]

        db_path = cfg["runtime"]["db_path"]
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        if db_path != ":memory:":
            try:
                self.conn.execute("PRAGMA journal_mode=WAL")
                self.conn.execute("PRAGMA busy_timeout=5000")  # share the DB with sibling bots
            except sqlite3.OperationalError:
                pass
        self._init_db()

    # ------------------------------------------------------------------ #
    def _init_db(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS carry_state (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS carry_positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                asset TEXT, status TEXT,
                opened_at TEXT, closed_at TEXT,
                spot_qty REAL, spot_entry REAL,
                perp_qty REAL, perp_entry REAL,
                notional_usd REAL, capital_usd REAL,
                funding_accrued_usd REAL, fees_usd REAL, realized_pnl_usd REAL,
                low_reads INTEGER, last_accrual_ts REAL,
                mode TEXT, reason TEXT,
                -- per-leg unwind state so a retried unwind never re-hits a closed leg
                perp_closed INTEGER DEFAULT 0, spot_closed INTEGER DEFAULT 0,
                perp_exit_price REAL, perp_exit_fee REAL,
                spot_exit_price REAL, spot_exit_fee REAL
            );
            CREATE TABLE IF NOT EXISTS carry_funding (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT, asset TEXT, rate_apr REAL, notional_usd REAL, amount_usd REAL
            );
            """
        )
        # Migrate older carry DBs that predate the per-leg unwind columns.
        cols = [r["name"] for r in self.conn.execute("PRAGMA table_info(carry_positions)")]
        for col, decl in (("perp_closed", "INTEGER DEFAULT 0"), ("spot_closed", "INTEGER DEFAULT 0"),
                          ("perp_exit_price", "REAL"), ("perp_exit_fee", "REAL"),
                          ("spot_exit_price", "REAL"), ("spot_exit_fee", "REAL")):
            if col not in cols:
                self.conn.execute(f"ALTER TABLE carry_positions ADD COLUMN {col} {decl}")
        self.conn.commit()

    # --- tiny state helpers (mirror RiskManager) ---------------------- #
    def _get(self, k: str) -> Optional[str]:
        row = self.conn.execute("SELECT value FROM carry_state WHERE key=?", (k,)).fetchone()
        return row["value"] if row else None

    def _set(self, k: str, v: Any) -> None:
        self.conn.execute("INSERT INTO carry_state(key,value) VALUES(?,?) "
                          "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, str(v)))
        self.conn.commit()

    # ------------------------------------------------------------------ #
    # Positions                                                          #
    # ------------------------------------------------------------------ #
    def open_position(self, asset: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM carry_positions WHERE status='OPEN' AND asset=? ORDER BY id DESC LIMIT 1",
            (asset,)).fetchone()

    def open_positions(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM carry_positions WHERE status='OPEN'").fetchall())

    def capital_used(self) -> float:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(capital_usd),0) c FROM carry_positions WHERE status='OPEN'").fetchone()
        return float(row["c"])

    # ------------------------------------------------------------------ #
    # Gates + sizing                                                     #
    # ------------------------------------------------------------------ #
    def kill_active(self) -> bool:
        return self._get("carry_kill") == "1" or os.getenv("CARRY_KILL", "").strip() in ("1", "true", "yes")

    def set_kill(self, on: bool = True) -> None:
        self._set("carry_kill", "1" if on else "0")

    def _day_realized(self) -> float:
        today = _utcnow().date().isoformat()
        row = self.conn.execute(
            "SELECT COALESCE(SUM(realized_pnl_usd),0) p FROM carry_positions "
            "WHERE status='CLOSED' AND substr(closed_at,1,10)=?", (today,)).fetchone()
        return float(row["p"])

    def can_open(self, asset: str) -> tuple[bool, str]:
        if self.kill_active():
            return False, "kill switch active"
        if self.open_position(asset) is not None:
            return False, f"already holding {asset}"
        if self._day_realized() <= -self.daily_loss_limit:
            return False, f"daily loss limit (${self._day_realized():.2f})"
        if self.capital_used() >= self.sleeve:
            return False, "sleeve fully deployed"
        return True, "ok"

    def size(self, spot_price: float) -> dict[str, Any]:
        """Notional/qty/capital for a new pair, bounded by sleeve + per-asset cap."""
        if spot_price <= 0:
            return {"notional": 0.0, "qty": 0.0, "capital": 0.0, "viable": False}
        capital_mult = 1.0 + 1.0 / self.target_leverage
        remaining = max(0.0, self.sleeve - self.capital_used())
        notional = min(self.per_asset_cap, remaining / capital_mult)
        capital = notional * capital_mult
        return {
            "notional": notional,
            "qty": notional / spot_price,
            "capital": capital,
            "viable": notional >= self.min_notional,
        }

    # ------------------------------------------------------------------ #
    # Lifecycle                                                          #
    # ------------------------------------------------------------------ #
    def record_open(self, pair: PairFill, capital_usd: float, reason: str) -> int:
        entry_fees = pair.spot.fee + pair.perp.fee
        cur = self.conn.execute(
            "INSERT INTO carry_positions(asset, status, opened_at, spot_qty, spot_entry, "
            "perp_qty, perp_entry, notional_usd, capital_usd, funding_accrued_usd, fees_usd, "
            "realized_pnl_usd, low_reads, last_accrual_ts, mode, reason) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (pair.asset, "OPEN", _utcnow().isoformat(), pair.spot.qty, pair.spot.price,
             pair.perp.qty, pair.perp.price, pair.notional, capital_usd, 0.0, entry_fees,
             0.0, 0, time.time(), self.mode, reason))
        self.conn.commit()
        logger.info("CARRY OPEN {} notional ${:.2f} (spot {:.6f}@{:.2f} / short perp {:.6f}@{:.2f}) [{}]",
                    pair.asset, pair.notional, pair.spot.qty, pair.spot.price,
                    pair.perp.qty, pair.perp.price, self.mode)
        return int(cur.lastrowid)

    def accrue_funding(self, position: sqlite3.Row, funding_apr: float,
                       now: Optional[float] = None) -> float:
        """Add pro-rata funding income since the last poll. Positive APR = income
        (we are short the perp). Returns the amount accrued this poll."""
        now = time.time() if now is None else now
        last = float(position["last_accrual_ts"] or now)
        dt_years = max(0.0, (now - last)) / _YEAR_SECONDS
        amount = float(position["notional_usd"]) * funding_apr * dt_years
        new_total = float(position["funding_accrued_usd"]) + amount
        self.conn.execute(
            "UPDATE carry_positions SET funding_accrued_usd=?, last_accrual_ts=? WHERE id=?",
            (new_total, now, position["id"]))
        self.conn.execute(
            "INSERT INTO carry_funding(ts, asset, rate_apr, notional_usd, amount_usd) VALUES(?,?,?,?,?)",
            (_utcnow().isoformat(), position["asset"], funding_apr, position["notional_usd"], amount))
        self.conn.commit()
        return amount

    def update_low_reads(self, position_id: int, low_reads: int) -> None:
        self.conn.execute("UPDATE carry_positions SET low_reads=? WHERE id=?",
                          (low_reads, position_id))
        self.conn.commit()

    def unwind_in_progress(self, position: sqlite3.Row) -> bool:
        """True if exactly one leg has been closed (a resumable, half-done unwind)."""
        return bool(position["perp_closed"]) != bool(position["spot_closed"])

    def mark_perp_closed(self, position_id: int, exit_fill: "Any") -> None:
        """Persist the perp-cover fill immediately so a retry never re-covers it."""
        self.conn.execute(
            "UPDATE carry_positions SET perp_closed=1, perp_exit_price=?, perp_exit_fee=? WHERE id=?",
            (exit_fill.price, exit_fill.fee, position_id))
        self.conn.commit()

    def mark_spot_closed(self, position_id: int, exit_fill: "Any") -> None:
        """Persist the spot-sell fill immediately so a retry never re-sells it."""
        self.conn.execute(
            "UPDATE carry_positions SET spot_closed=1, spot_exit_price=?, spot_exit_fee=? WHERE id=?",
            (exit_fill.price, exit_fill.fee, position_id))
        self.conn.commit()

    def finalize_unwind(self, position: sqlite3.Row, reason: str) -> float:
        """Settle PnL once BOTH legs are closed. Reads the persisted exit fills so it
        is correct even when the two legs closed on different polls (resumed unwind).
        realized = both legs' price PnL (≈0 when neutral) + funding - all fees."""
        pos = self.conn.execute("SELECT * FROM carry_positions WHERE id=?",
                                (position["id"],)).fetchone()
        if not (pos["perp_closed"] and pos["spot_closed"]):
            raise ValueError(f"finalize_unwind before both legs closed (id={pos['id']})")
        qty_s, qty_p = float(pos["spot_qty"]), float(pos["perp_qty"])
        spot_pnl = (float(pos["spot_exit_price"]) - float(pos["spot_entry"])) * qty_s
        perp_pnl = (float(pos["perp_entry"]) - float(pos["perp_exit_price"])) * qty_p  # short
        total_fees = float(pos["fees_usd"]) + float(pos["spot_exit_fee"] or 0.0) \
            + float(pos["perp_exit_fee"] or 0.0)
        funding = float(pos["funding_accrued_usd"])
        realized = spot_pnl + perp_pnl - total_fees + funding
        self.conn.execute(
            "UPDATE carry_positions SET status='CLOSED', closed_at=?, fees_usd=?, "
            "realized_pnl_usd=?, reason=? WHERE id=?",
            (_utcnow().isoformat(), total_fees, realized, reason, pos["id"]))
        self.conn.commit()
        logger.info("CARRY UNWIND {} | funding ${:.2f} fees ${:.2f} basis/legs ${:.2f} "
                    "=> realized ${:.2f} | {}", pos["asset"], funding, total_fees,
                    spot_pnl + perp_pnl, realized, reason)
        return realized

    def record_unwind(self, position: sqlite3.Row, spot_exit: "Any", perp_exit: "Any",
                      reason: str) -> float:
        """Convenience: close both legs atomically then settle (used in sim/tests).
        The live loop uses mark_*_closed + finalize_unwind for resumable safety."""
        self.mark_perp_closed(int(position["id"]), perp_exit)
        self.mark_spot_closed(int(position["id"]), spot_exit)
        return self.finalize_unwind(position, reason)

    # ------------------------------------------------------------------ #
    def delta_breach(self, position: sqlite3.Row) -> bool:
        """True if spot vs perp quantities drifted past tolerance (partial fills)."""
        qs, qp = float(position["spot_qty"]), float(position["perp_qty"])
        ref = max(qs, qp, 1e-12)
        return abs(qs - qp) / ref > self.delta_tol

    def daily_stats(self) -> dict[str, Any]:
        today = _utcnow().date().isoformat()
        fund = self.conn.execute(
            "SELECT COALESCE(SUM(amount_usd),0) a FROM carry_funding WHERE substr(ts,1,10)=?",
            (today,)).fetchone()["a"]
        return {
            "date_utc": today,
            "mode": self.mode,
            "open_pairs": len(self.open_positions()),
            "capital_used": round(self.capital_used(), 2),
            "sleeve": self.sleeve,
            "funding_today_usd": round(float(fund), 4),
            "realized_today_usd": round(self._day_realized(), 2),
            "kill": self.kill_active(),
        }
