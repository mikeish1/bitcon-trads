"""
ETF risk manager (long-only, equal-weight top-K), SQLite-backed.

Equal-weights the held set (target 1/K of equity per name), bounded by a total
exposure cap and available cash. Own tables in the shared DB. SIM keeps an
internal paper-cash ledger; live reads equity from the broker balances passed in.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional

from loguru import logger

from src.settings_service import CapitalSettingsService


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


_QTY_DRIFT_TOLERANCE = 0.02   # |broker-ledger|/ledger above this = flag (split / external change)


class EtfRiskManager:
    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg
        e = cfg["etf"]
        self.sleeve = float(e["capital"]["sleeve_usd"])
        self.max_exposure = float(e["capital"]["max_total_exposure_pct"])
        self.min_notional = float(e["capital"]["min_notional_usd"])
        # Centralized deployable-capital envelope for the ETF sleeve (defaults to
        # the legacy equity * max_total_exposure_pct; user-adjustable like spot).
        self.capital_policy = CapitalSettingsService(cfg).policy("etf")
        self.top_k = int(e["selection"]["top_k"])
        rt = cfg["etf_runtime"]
        self.mode = rt["mode"]
        self.uses_broker = rt["place_orders"]
        self.quote = rt["quote"]

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
        self._seed()

    # ------------------------------------------------------------------ #
    def _init_db(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS etf_state (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS etf_positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT, status TEXT,
                opened_at TEXT, closed_at TEXT,
                qty REAL, entry_price REAL, cost_usd REAL, entry_fee REAL,
                exit_price REAL, exit_fee REAL, realized_pnl_usd REAL,
                mode TEXT, reason TEXT
            );
            """
        )
        self.conn.commit()

    def _seed(self) -> None:
        if self._get("paper_cash") is None:
            self._set("paper_cash", self.sleeve)
            logger.info("ETF state seeded. Paper cash ${:.2f}.", self.sleeve)

    def _get(self, k: str) -> Optional[str]:
        row = self.conn.execute("SELECT value FROM etf_state WHERE key=?", (k,)).fetchone()
        return row["value"] if row else None

    def _set(self, k: str, v: Any) -> None:
        self.conn.execute("INSERT INTO etf_state(key,value) VALUES(?,?) "
                          "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, str(v)))
        self.conn.commit()

    def _getf(self, k: str, d: float = 0.0) -> float:
        v = self._get(k)
        try:
            return float(v) if v is not None else d
        except ValueError:
            return d

    def state_get(self, key: str) -> Optional[str]:
        return self._get(key)

    def state_set(self, key: str, value: Any) -> None:
        self._set(key, value)

    # ------------------------------------------------------------------ #
    def open_position(self, symbol: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM etf_positions WHERE status='OPEN' AND symbol=? ORDER BY id DESC LIMIT 1",
            (symbol,)).fetchone()

    def open_positions(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM etf_positions WHERE status='OPEN'").fetchall())

    def held_symbols(self) -> list[str]:
        return [p["symbol"] for p in self.open_positions()]

    def holdings_value(self, prices: dict[str, float]) -> float:
        total = 0.0
        for p in self.open_positions():
            total += p["qty"] * prices.get(p["symbol"], p["entry_price"])
        return total

    def current_equity(self, balances: dict[str, float], prices: dict[str, float]) -> float:
        if self.uses_broker:
            return balances.get(self.quote, 0.0) + sum(
                balances.get(s, 0.0) * prices.get(s, 0.0) for s in prices)
        return self._getf("paper_cash", self.sleeve) + self.holdings_value(prices)

    def available_cash(self, balances: dict[str, float]) -> float:
        if self.uses_broker:
            return balances.get(self.quote, 0.0)
        return self._getf("paper_cash", self.sleeve)

    # ------------------------------------------------------------------ #
    def size(self, equity: float, available_cash: float, exposure_used: float) -> dict[str, Any]:
        """Equal-weight 1/K target, bounded by the exposure cap and free cash."""
        target = equity / max(self.top_k, 1)
        budget = float(self.capital_policy.remaining_capacity(equity, available_cash, exposure_used))
        spend = min(target, budget, available_cash)
        return {"spend_usd": spend, "viable": spend >= self.min_notional}

    def record_open(self, symbol: str, fill: dict[str, Any], reason: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO etf_positions(symbol, status, opened_at, qty, entry_price, cost_usd, "
            "entry_fee, mode, reason) VALUES(?,?,?,?,?,?,?,?,?)",
            (symbol, "OPEN", _utcnow().isoformat(), fill["qty"], fill["price"], fill["cost"],
             fill.get("fee", 0.0), self.mode, reason))
        if not self.uses_broker:
            self._set("paper_cash", self._getf("paper_cash") - (fill["cost"] + fill.get("fee", 0.0)))
        self.conn.commit()
        logger.info("ETF OPEN {} {:.4f} @ {:.2f} (${:.2f}) [{}]",
                    symbol, fill["qty"], fill["price"], fill["cost"], self.mode)
        return int(cur.lastrowid)

    def record_close(self, position: sqlite3.Row, fill: dict[str, Any], reason: str) -> float:
        proceeds = fill["price"] * fill["qty"] - fill.get("fee", 0.0)
        pnl = proceeds - (position["cost_usd"] + (position["entry_fee"] or 0.0))
        self.conn.execute(
            "UPDATE etf_positions SET status='CLOSED', closed_at=?, exit_price=?, exit_fee=?, "
            "realized_pnl_usd=?, reason=? WHERE id=?",
            (_utcnow().isoformat(), fill["price"], fill.get("fee", 0.0), pnl, reason, position["id"]))
        if not self.uses_broker:
            self._set("paper_cash", self._getf("paper_cash") + proceeds)
        self.conn.commit()
        logger.info("ETF CLOSE {} @ {:.2f} | PnL ${:.2f} | {}", position["symbol"],
                    fill["price"], pnl, reason)
        return pnl

    # --- partial adjustments (static-allocation rebalancing) ------------------ #
    def position_value(self, symbol: str, prices: dict[str, float]) -> float:
        pos = self.open_position(symbol)
        if pos is None:
            return 0.0
        return pos["qty"] * prices.get(symbol, pos["entry_price"])

    def add_to_position(self, symbol: str, fill: dict[str, Any], reason: str) -> int:
        """Add to an existing position (avg-cost), or open one if none. Used by the
        static allocator to top a holding up toward its target weight."""
        pos = self.open_position(symbol)
        if pos is None:
            return self.record_open(symbol, fill, reason)
        new_qty = pos["qty"] + fill["qty"]
        new_cost = pos["cost_usd"] + fill["cost"]
        new_entry = new_cost / new_qty if new_qty else fill["price"]
        self.conn.execute(
            "UPDATE etf_positions SET qty=?, cost_usd=?, entry_price=?, entry_fee=? WHERE id=?",
            (new_qty, new_cost, new_entry, (pos["entry_fee"] or 0.0) + fill.get("fee", 0.0), pos["id"]))
        if not self.uses_broker:
            self._set("paper_cash", self._getf("paper_cash") - (fill["cost"] + fill.get("fee", 0.0)))
        self.conn.commit()
        logger.info("ETF ADD {} +{:.4f} @ {:.2f} (${:.2f}) [{}]",
                    symbol, fill["qty"], fill["price"], fill["cost"], self.mode)
        return int(pos["id"])

    def trim_position(self, position: sqlite3.Row, qty: float, price: float,
                      fee: float = 0.0, reason: str = "rebalance trim") -> float:
        """Sell PART of a position at avg cost (realizes proportional PnL, accrued to
        etf_realized_pnl). Fully closes it if the residual is below dust. Returns the
        realized PnL on the trimmed shares."""
        qty = min(qty, position["qty"])
        if qty <= 0:
            return 0.0
        avg = position["entry_price"]
        proceeds = qty * price - fee
        realized = proceeds - qty * avg
        new_qty = position["qty"] - qty
        new_cost = position["cost_usd"] - qty * avg
        if not self.uses_broker:
            self._set("paper_cash", self._getf("paper_cash") + proceeds)
        self._set("etf_realized_pnl", self._getf("etf_realized_pnl") + realized)
        dust = self.min_notional / price if price else 0.0
        if new_qty <= dust:
            self.conn.execute(
                "UPDATE etf_positions SET status='CLOSED', closed_at=?, exit_price=?, exit_fee=?, "
                "realized_pnl_usd=?, reason=? WHERE id=?",
                (_utcnow().isoformat(), price, fee, realized, reason, position["id"]))
        else:
            self.conn.execute("UPDATE etf_positions SET qty=?, cost_usd=? WHERE id=?",
                              (new_qty, new_cost, position["id"]))
        self.conn.commit()
        logger.info("ETF TRIM {} -{:.4f} @ {:.2f} | realized ${:.2f} | {}",
                    position["symbol"], qty, price, realized, reason)
        return realized

    def opened_today(self, position: sqlite3.Row, today_iso: str) -> bool:
        """True if the position opened on the given calendar day (UTC). Drives the
        loop's PDT same-day guard, which prevents a same-day round-trip."""
        opened = position["opened_at"]
        return bool(opened) and str(opened)[:10] == today_iso[:10]

    def reconcile(self, broker_positions: dict[str, float],
                  prices: dict[str, float]) -> list[str]:
        """Reconcile the DB ledger against the broker (broker mode only). Returns a
        list of human-readable anomaly notes for alerting.

        Two cases handled:
          * 'position gone' - the broker no longer holds a symbol our ledger marks
            OPEN (external/manual close, full liquidation, delisting): close it.
          * 'qty drift' - the broker still holds the symbol but a materially different
            qty (a split or external partial change): FLAG only. We never auto-rewrite
            a cost basis - a split changes qty + price but not cost, while an external
            sale changes cost, and we cannot tell which apart - mirroring the spot
            bot's "leave it to a human" reconcile. A human resolves it.
        """
        if not self.uses_broker:
            return []
        notes: list[str] = []
        for pos in self.open_positions():
            sym = pos["symbol"]
            price = prices.get(sym, pos["entry_price"])
            dust = self.min_notional / price if price else 0.0
            broker_qty = broker_positions.get(sym, 0.0)
            if broker_qty < dust and pos["qty"] > dust:
                logger.warning("ETF reconcile: {} not held at broker - closing (external fill).", sym)
                self.record_close(pos, {"price": price, "qty": pos["qty"], "fee": 0.0},
                                  "reconcile: not held at broker")
                notes.append(f"{sym} closed (not held at broker)")
            elif broker_qty > dust and pos["qty"] > dust:
                drift = abs(broker_qty - pos["qty"]) / pos["qty"]
                if drift > _QTY_DRIFT_TOLERANCE:
                    logger.warning(
                        "ETF reconcile: {} qty drift ledger={:.6f} vs broker={:.6f} ({:.0%}) - "
                        "possible split/corporate action; review (basis left unchanged).",
                        sym, pos["qty"], broker_qty, drift)
                    notes.append(f"{sym} qty drift {pos['qty']:.4f}->{broker_qty:.4f} (possible split)")
        return notes

    def daily_stats(self, equity: float) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "equity": round(equity, 2),
            "held": self.held_symbols(),
            "open_positions": len(self.open_positions()),
            "paper_cash": round(self._getf("paper_cash", self.sleeve), 2),
        }
