"""
Risk manager for spot, long-only trading.

- Dynamic position sizing: risk a small % of portfolio equity per trade, scaled
  by a conservative fractional-Kelly factor, sized off the ATR-based stop
  distance. Default capital $250 in paper mode; in live mode equity is your real
  portfolio value (USDT + BTC).
- ATR-based initial stop, R-multiple take-profit, and ratcheting trailing stop.
- Safety rails: daily/weekly loss limits, consecutive-loss circuit breaker,
  post-trade cooldown, max trades/day, single position.
- SQLite persistence so positions and stats survive restarts; plus a
  reconcile() that compares the DB to your real balances on startup.

Tune numbers in config/trading_config.yaml.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional

from loguru import logger


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class RiskManager:
    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg
        self.really_live = cfg["runtime"]["really_live"]
        r, s, e = cfg["risk"], cfg["safety"], cfg["exits"]

        self.default_capital = r["default_capital_usd"]
        self.risk_per_trade = r["risk_per_trade_pct"]
        self.max_position_pct = r["max_position_pct"]
        self.min_notional = r["min_notional_usd"]
        self.kelly_fraction = r["kelly_fraction"]
        self.kelly_payoff = r["kelly_assumed_payoff"]

        self.atr_stop_mult = e["atr_stop_mult"]
        self.min_stop_pct = e.get("min_stop_pct", 0.01)
        self.atr_trail_mult = e["atr_trail_mult"]
        self.take_profit_R = e["take_profit_R"]
        self.stop_limit_offset = e["stop_limit_offset_pct"]

        self.daily_loss_limit = s["daily_loss_limit_pct"]
        self.weekly_loss_limit = s["weekly_loss_limit_pct"]
        self.max_consec_losses = s["max_consecutive_losses"]
        self.cooldown_minutes = s["cooldown_minutes"]
        self.max_trades_per_day = s["max_trades_per_day"]

        self.conn = sqlite3.connect(cfg["runtime"]["db_path"], check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_db()
        self._seed()

    # ------------------------------------------------------------------ #
    def _init_db(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                opened_at TEXT, closed_at TEXT,
                entry_price REAL, exit_price REAL,
                qty REAL, cost_usd REAL, entry_fee REAL, exit_fee REAL,
                stop_price REAL, take_price REAL, current_stop REAL,
                stop_order_id TEXT,
                pnl_usd REAL, status TEXT, mode TEXT, reason TEXT
            );
            CREATE TABLE IF NOT EXISTS decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT, action TEXT, conviction INTEGER,
                consulted_claude INTEGER, reasoning TEXT
            );
            """
        )
        self.conn.commit()

    def _seed(self) -> None:
        if self._get("paper_equity") is None:
            now = _utcnow()
            self._set("paper_equity", self.default_capital)
            self._set("day_date", now.date().isoformat())
            self._set("day_start_equity", self.default_capital)
            self._set("week_id", f"{now.isocalendar().year}-{now.isocalendar().week}")
            self._set("week_start_equity", self.default_capital)
            self._set("consecutive_losses", 0)
            self._set("wins", 0)
            self._set("losses", 0)
            self._set("last_close_ts", "")
            logger.info("State seeded. Paper equity ${:.2f}.", self.default_capital)

    # ------------------------------------------------------------------ #
    def _get(self, k: str) -> Optional[str]:
        row = self.conn.execute("SELECT value FROM state WHERE key=?", (k,)).fetchone()
        return row["value"] if row else None

    def _set(self, k: str, v: Any) -> None:
        self.conn.execute(
            "INSERT INTO state(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, str(v)))
        self.conn.commit()

    def _getf(self, k: str, d: float = 0.0) -> float:
        v = self._get(k)
        try:
            return float(v) if v is not None else d
        except ValueError:
            return d

    def _geti(self, k: str, d: int = 0) -> int:
        return int(self._getf(k, d))

    # ------------------------------------------------------------------ #
    # Equity                                                            #
    # ------------------------------------------------------------------ #
    def current_equity(self, balances: dict[str, float], price: float) -> float:
        """Live: real portfolio value (USDT + BTC). Paper: simulated equity."""
        if self.really_live:
            return balances.get("USDT", 0.0) + balances.get("BTC", 0.0) * price
        return self._getf("paper_equity", self.default_capital)

    def _roll_periods(self, equity: float) -> None:
        now = _utcnow()
        today = now.date().isoformat()
        if self._get("day_date") != today:
            self._set("day_date", today)
            self._set("day_start_equity", equity)
            logger.info("New UTC day - daily limit + trade count reset.")
        wk = f"{now.isocalendar().year}-{now.isocalendar().week}"
        if self._get("week_id") != wk:
            self._set("week_id", wk)
            self._set("week_start_equity", equity)
            logger.info("New ISO week - weekly limit reset.")

    def _trades_today(self) -> int:
        today = _utcnow().date().isoformat()
        row = self.conn.execute(
            "SELECT COUNT(*) c FROM trades WHERE substr(opened_at,1,10)=?", (today,)).fetchone()
        return int(row["c"])

    # ------------------------------------------------------------------ #
    # Safety gate                                                        #
    # ------------------------------------------------------------------ #
    def can_open_trade(self, equity: float) -> tuple[bool, str]:
        self._roll_periods(equity)
        if self.open_position() is not None:
            return False, "position already open"

        day_start = self._getf("day_start_equity", equity)
        if day_start > 0 and (equity - day_start) / day_start <= -self.daily_loss_limit:
            return False, f"daily loss limit ({(equity/day_start-1):.2%})"
        week_start = self._getf("week_start_equity", equity)
        if week_start > 0 and (equity - week_start) / week_start <= -self.weekly_loss_limit:
            return False, f"weekly loss limit ({(equity/week_start-1):.2%})"
        if self._geti("consecutive_losses") >= self.max_consec_losses:
            return False, f"circuit breaker ({self._geti('consecutive_losses')} losses in a row)"
        if self._trades_today() >= self.max_trades_per_day:
            return False, f"max trades/day reached ({self.max_trades_per_day})"
        last = self._get("last_close_ts")
        if last:
            try:
                mins = (_utcnow() - datetime.fromisoformat(last)).total_seconds() / 60
                if mins < self.cooldown_minutes:
                    return False, f"cooldown ({mins:.0f}/{self.cooldown_minutes} min)"
            except ValueError:
                pass
        return True, "ok"

    # ------------------------------------------------------------------ #
    # Sizing                                                             #
    # ------------------------------------------------------------------ #
    def _win_rate(self) -> float:
        w, l = self._geti("wins"), self._geti("losses")
        return 0.5 if (w + l) < 10 else w / (w + l)

    def size_buy(self, equity: float, available_usdt: float,
                 price: float, atr: float) -> dict[str, Any]:
        """
        Dynamic fractional-Kelly sizing off the ATR stop distance.

        stop_distance = atr_stop_mult * ATR
        risk_amount   = equity * risk_fraction   (~loss if the stop is hit)
        spend         = risk_amount * price / stop_distance, capped by USDT.
        """
        # ATR-based, but floored at min_stop_pct so a tiny 5m ATR can't create a
        # noise-tight stop that whipsaws us out instantly.
        stop_distance = max(self.atr_stop_mult * atr, price * self.min_stop_pct)
        stop_price = price - stop_distance

        win = self._win_rate()
        kelly_star = max(win - (1.0 - win) / self.kelly_payoff, 0.0)
        risk_fraction = min(self.kelly_fraction * kelly_star, self.risk_per_trade)
        risk_fraction = max(risk_fraction, self.risk_per_trade * 0.25)  # small floor

        risk_amount = equity * risk_fraction
        spend = risk_amount * price / stop_distance

        # In live mode we can't spend more USDT than we actually hold.
        cap = available_usdt * self.max_position_pct if self.really_live else equity * self.max_position_pct
        spend = min(spend, cap)

        take_price = price + self.take_profit_R * stop_distance
        return {
            "spend_usd": spend,
            "stop_price": stop_price,
            "take_price": take_price,
            "stop_distance": stop_distance,
            "risk_fraction": risk_fraction,
            "win_rate": win,
            "viable": spend >= self.min_notional,
        }

    def trailing_stop(self, price: float, atr: float) -> float:
        return price - max(self.atr_trail_mult * atr, price * self.min_stop_pct)

    def stop_limit_price(self, stop_price: float) -> float:
        """Limit price sits just below the stop trigger to improve fill odds."""
        return stop_price * (1 - self.stop_limit_offset)

    # ------------------------------------------------------------------ #
    # Position lifecycle                                                 #
    # ------------------------------------------------------------------ #
    def open_position(self) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM trades WHERE status='OPEN' ORDER BY id DESC LIMIT 1").fetchone()

    def record_open(self, fill: dict[str, Any], stop_price: float, take_price: float,
                    stop_order_id: Optional[str], reason: str) -> int:
        mode = "LIVE" if self.really_live else "PAPER"
        cur = self.conn.execute(
            "INSERT INTO trades(opened_at, entry_price, qty, cost_usd, entry_fee, "
            "stop_price, take_price, current_stop, stop_order_id, status, mode, reason) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (_utcnow().isoformat(), fill["price"], fill["qty"], fill["cost"], fill["fee"],
             stop_price, take_price, stop_price, stop_order_id, "OPEN", mode, reason))
        self.conn.commit()
        logger.info("OPENED LONG {:.6f} BTC @ {:.2f} | stop {:.2f} | target {:.2f} [{}]",
                    fill["qty"], fill["price"], stop_price, take_price, mode)
        return int(cur.lastrowid)

    def update_stop(self, trade_id: int, new_stop: float, stop_order_id: Optional[str]) -> None:
        self.conn.execute("UPDATE trades SET current_stop=?, stop_order_id=? WHERE id=?",
                          (new_stop, stop_order_id, trade_id))
        self.conn.commit()

    def record_close(self, trade: sqlite3.Row, exit_price: float,
                     exit_fee: float, reason: str) -> float:
        entry, qty = trade["entry_price"], trade["qty"]
        entry_fee = trade["entry_fee"] or 0.0
        pnl = (exit_price - entry) * qty - entry_fee - exit_fee
        self.conn.execute(
            "UPDATE trades SET closed_at=?, exit_price=?, exit_fee=?, pnl_usd=?, "
            "status='CLOSED', reason=? WHERE id=?",
            (_utcnow().isoformat(), exit_price, exit_fee, pnl, reason, trade["id"]))

        if not self.really_live:
            self._set("paper_equity", self._getf("paper_equity") + pnl)
        self._set("last_close_ts", _utcnow().isoformat())
        if pnl >= 0:
            self._set("wins", self._geti("wins") + 1)
            self._set("consecutive_losses", 0)
        else:
            self._set("losses", self._geti("losses") + 1)
            self._set("consecutive_losses", self._geti("consecutive_losses") + 1)
        self.conn.commit()
        logger.info("CLOSED LONG @ {:.2f} | PnL ${:.2f} | reason: {}", exit_price, pnl, reason)
        return pnl

    def log_decision(self, action: str, conviction: int, consulted: bool, reasoning: str) -> None:
        self.conn.execute(
            "INSERT INTO decisions(ts, action, conviction, consulted_claude, reasoning) "
            "VALUES(?,?,?,?,?)",
            (_utcnow().isoformat(), action, conviction, int(consulted), reasoning))
        self.conn.commit()

    # ------------------------------------------------------------------ #
    # Startup reconciliation                                             #
    # ------------------------------------------------------------------ #
    def reconcile(self, balances: dict[str, float], price: float) -> None:
        """Compare the DB's idea of our position to real balances (live only)."""
        if not self.really_live:
            return
        pos = self.open_position()
        btc = balances.get("BTC", 0.0)
        dust = self.min_notional / price if price else 0.0

        if pos is not None and btc < dust:
            # We thought we held BTC, but it's gone - the exchange stop likely
            # filled while we were offline. Close the books at the recorded stop.
            logger.warning("Reconcile: open trade in DB but no BTC on exchange - "
                           "closing it (stop likely filled offline).")
            self.record_close(pos, pos["current_stop"] or price, 0.0, "offline stop fill")
        elif pos is None and btc >= dust:
            logger.warning("Reconcile: {:.6f} BTC on exchange but no tracked position. "
                           "Leaving it untouched - sell manually if unexpected.", btc)

    # ------------------------------------------------------------------ #
    def daily_stats(self, equity: float) -> dict[str, Any]:
        self._roll_periods(equity)
        day_start = self._getf("day_start_equity", equity)
        week_start = self._getf("week_start_equity", equity)
        today = self._get("day_date")
        closed = self.conn.execute(
            "SELECT COUNT(*) c, COALESCE(SUM(pnl_usd),0) p FROM trades "
            "WHERE status='CLOSED' AND substr(closed_at,1,10)=?", (today,)).fetchone()
        return {
            "date_utc": today,
            "mode": "LIVE" if self.really_live else "PAPER",
            "equity": round(equity, 2),
            "day_return_pct": round((equity / day_start - 1) * 100, 2) if day_start else 0,
            "week_return_pct": round((equity / week_start - 1) * 100, 2) if week_start else 0,
            "trades_today": self._trades_today(),
            "closed_today": closed["c"],
            "pnl_today_usd": round(closed["p"], 2),
            "wins": self._geti("wins"),
            "losses": self._geti("losses"),
            "consecutive_losses": self._geti("consecutive_losses"),
            "win_rate_pct": round(self._win_rate() * 100, 1),
        }
