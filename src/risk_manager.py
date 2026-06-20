"""
Risk manager: position sizing + safety rails + persistent state.

- Fractional Kelly position sizing with conservative caps.
- Hard safety rails: daily loss limit, weekly loss limit, consecutive-loss
  circuit breaker, post-trade cooldown, single-position rule.
- All state and a full trade log are persisted in SQLite so the bot survives
  restarts/redeploys (important on Railway).

Every decision is logged with its reasoning.

A basic user does not need to change anything here - tune the numbers in
config/trading_config.yaml instead.
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
        self.db_path = cfg["runtime"]["db_path"]
        risk, safety = cfg["risk"], cfg["safety"]

        self.max_risk_per_trade = risk["max_risk_per_trade"]
        self.kelly_fraction = risk["kelly_fraction"]
        self.kelly_payoff = risk["kelly_assumed_payoff"]
        self.min_trade_usd = risk["min_trade_usd"]
        self.max_position_pct = risk["max_position_pct"]
        self.stop_loss_pct = risk["stop_loss_pct"]
        self.take_profit_pct = risk["take_profit_pct"]

        self.daily_loss_limit = safety["daily_loss_limit_pct"]
        self.weekly_loss_limit = safety["weekly_loss_limit_pct"]
        self.max_consec_losses = safety["max_consecutive_losses"]
        self.cooldown_minutes = safety["cooldown_minutes"]
        self.max_open_positions = safety["max_open_positions"]

        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_db()
        self._seed_state(risk["starting_capital_usd"])

    # ------------------------------------------------------------------ #
    # Database setup                                                      #
    # ------------------------------------------------------------------ #
    def _init_db(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS state (
                key TEXT PRIMARY KEY,
                value TEXT
            );
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                opened_at TEXT,
                closed_at TEXT,
                side TEXT,
                entry_price REAL,
                exit_price REAL,
                qty REAL,
                notional_usd REAL,
                stop_price REAL,
                take_price REAL,
                pnl_usd REAL,
                status TEXT,           -- OPEN / CLOSED
                mode TEXT,             -- PAPER / LIVE
                reason TEXT
            );
            CREATE TABLE IF NOT EXISTS decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT,
                direction TEXT,
                agreement INTEGER,
                consulted_claude INTEGER,
                action TEXT,
                reasoning TEXT
            );
            """
        )
        self.conn.commit()

    def _seed_state(self, starting_capital: float) -> None:
        if self._get("equity") is None:
            now = _utcnow()
            self._set("equity", starting_capital)
            self._set("starting_equity", starting_capital)
            self._set("day_start_equity", starting_capital)
            self._set("day_date", now.date().isoformat())
            self._set("week_start_equity", starting_capital)
            self._set("week_number", f"{now.isocalendar().year}-{now.isocalendar().week}")
            self._set("consecutive_losses", 0)
            self._set("wins", 0)
            self._set("losses", 0)
            self._set("last_close_ts", "")
            logger.info("Initialised state with starting equity ${:.2f}", starting_capital)

    # ------------------------------------------------------------------ #
    # State helpers                                                       #
    # ------------------------------------------------------------------ #
    def _get(self, key: str) -> Optional[str]:
        row = self.conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def _set(self, key: str, value: Any) -> None:
        self.conn.execute(
            "INSERT INTO state(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )
        self.conn.commit()

    def _getf(self, key: str, default: float = 0.0) -> float:
        v = self._get(key)
        try:
            return float(v) if v is not None else default
        except ValueError:
            return default

    def _geti(self, key: str, default: int = 0) -> int:
        return int(self._getf(key, default))

    @property
    def equity(self) -> float:
        return self._getf("equity")

    # ------------------------------------------------------------------ #
    # Period rollover (daily / weekly)                                   #
    # ------------------------------------------------------------------ #
    def _roll_periods(self) -> None:
        now = _utcnow()
        today = now.date().isoformat()
        if self._get("day_date") != today:
            self._set("day_date", today)
            self._set("day_start_equity", self.equity)
            logger.info("New day - daily loss limit reset.")

        week_id = f"{now.isocalendar().year}-{now.isocalendar().week}"
        if self._get("week_number") != week_id:
            self._set("week_number", week_id)
            self._set("week_start_equity", self.equity)
            logger.info("New week - weekly loss limit reset.")

    # ------------------------------------------------------------------ #
    # Safety gate                                                         #
    # ------------------------------------------------------------------ #
    def can_open_trade(self) -> tuple[bool, str]:
        """Return (allowed, reason). Checks every safety rail."""
        self._roll_periods()

        if self.open_position() is not None:
            return False, "A position is already open (max_open_positions reached)."

        # Daily loss limit
        day_start = self._getf("day_start_equity", self.equity)
        if day_start > 0:
            day_dd = (self.equity - day_start) / day_start
            if day_dd <= -self.daily_loss_limit:
                return False, (
                    f"Daily loss limit hit ({day_dd:.2%} <= -{self.daily_loss_limit:.2%}). "
                    "Flat until tomorrow (UTC)."
                )

        # Weekly loss limit
        week_start = self._getf("week_start_equity", self.equity)
        if week_start > 0:
            week_dd = (self.equity - week_start) / week_start
            if week_dd <= -self.weekly_loss_limit:
                return False, (
                    f"Weekly loss limit hit ({week_dd:.2%} <= -{self.weekly_loss_limit:.2%}). "
                    "Flat until next week (UTC)."
                )

        # Consecutive-loss circuit breaker
        if self._geti("consecutive_losses") >= self.max_consec_losses:
            return False, (
                f"Circuit breaker: {self._geti('consecutive_losses')} consecutive losses "
                f">= {self.max_consec_losses}. Manual review recommended."
            )

        # Cooldown after the last close
        last_close = self._get("last_close_ts")
        if last_close:
            try:
                elapsed = (_utcnow() - datetime.fromisoformat(last_close)).total_seconds() / 60
                if elapsed < self.cooldown_minutes:
                    return False, (
                        f"Cooldown active ({elapsed:.0f}/{self.cooldown_minutes} min)."
                    )
            except ValueError:
                pass

        return True, "All safety checks passed."

    # ------------------------------------------------------------------ #
    # Position sizing (fractional Kelly)                                  #
    # ------------------------------------------------------------------ #
    def _dynamic_win_rate(self) -> float:
        wins, losses = self._geti("wins"), self._geti("losses")
        total = wins + losses
        # Bayesian-ish prior of 50% until we have a track record.
        if total < 10:
            return 0.5
        return wins / total

    def compute_position(self, price: float) -> dict[str, Any]:
        """
        Fractional-Kelly position sizing.

        risk_fraction = clamp( kelly_fraction * max(kelly*, 0), 0, max_risk_per_trade )
        notional      = equity * risk_fraction / stop_loss_pct   (so a stop ~= risk_fraction loss)
        capped by max_position_pct of equity.
        """
        equity = self.equity
        win = self._dynamic_win_rate()
        payoff = self.kelly_payoff
        kelly_star = win - (1.0 - win) / payoff      # classic Kelly fraction
        kelly_star = max(kelly_star, 0.0)

        risk_fraction = min(self.kelly_fraction * kelly_star, self.max_risk_per_trade)
        # Never risk literally zero just because the prior is exactly break-even;
        # use a tiny floor so a valid 28/31 signal can still take a measured position.
        risk_fraction = max(risk_fraction, self.max_risk_per_trade * 0.25)

        notional = equity * risk_fraction / max(self.stop_loss_pct, 1e-6)
        notional = min(notional, equity * self.max_position_pct)
        qty = notional / price if price > 0 else 0.0

        return {
            "equity": equity,
            "win_rate": win,
            "kelly_star": kelly_star,
            "risk_fraction": risk_fraction,
            "notional_usd": notional,
            "qty": qty,
            "viable": notional >= self.min_trade_usd,
        }

    def stop_and_target(self, side: str, entry: float) -> tuple[float, float]:
        if side == "LONG":
            return entry * (1 - self.stop_loss_pct), entry * (1 + self.take_profit_pct)
        return entry * (1 + self.stop_loss_pct), entry * (1 - self.take_profit_pct)

    # ------------------------------------------------------------------ #
    # Trade lifecycle                                                    #
    # ------------------------------------------------------------------ #
    def open_position(self) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM trades WHERE status='OPEN' ORDER BY id DESC LIMIT 1"
        ).fetchone()

    def record_open(
        self, side: str, entry: float, qty: float, notional: float,
        stop: float, take: float, mode: str, reason: str,
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO trades(opened_at, side, entry_price, qty, notional_usd, "
            "stop_price, take_price, status, mode, reason) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (_utcnow().isoformat(), side, entry, qty, notional, stop, take, "OPEN", mode, reason),
        )
        self.conn.commit()
        logger.info(
            "OPENED {} {:.6f} @ {:.2f} (notional ${:.2f}, stop {:.2f}, target {:.2f}) [{}]",
            side, qty, entry, notional, stop, take, mode,
        )
        return int(cur.lastrowid)

    def record_close(self, trade_id: int, exit_price: float, reason: str) -> float:
        row = self.conn.execute("SELECT * FROM trades WHERE id=?", (trade_id,)).fetchone()
        if row is None:
            return 0.0

        side, entry, qty = row["side"], row["entry_price"], row["qty"]
        fee_pct = self.cfg["execution"]["taker_fee_pct"]
        gross = (exit_price - entry) * qty if side == "LONG" else (entry - exit_price) * qty
        fees = (entry + exit_price) * qty * fee_pct
        pnl = gross - fees

        self.conn.execute(
            "UPDATE trades SET closed_at=?, exit_price=?, pnl_usd=?, status='CLOSED', "
            "reason=? WHERE id=?",
            (_utcnow().isoformat(), exit_price, pnl, reason, trade_id),
        )

        new_equity = self.equity + pnl
        self._set("equity", new_equity)
        self._set("last_close_ts", _utcnow().isoformat())

        if pnl >= 0:
            self._set("wins", self._geti("wins") + 1)
            self._set("consecutive_losses", 0)
        else:
            self._set("losses", self._geti("losses") + 1)
            self._set("consecutive_losses", self._geti("consecutive_losses") + 1)

        self.conn.commit()
        logger.info(
            "CLOSED {} @ {:.2f} | PnL ${:.2f} | equity ${:.2f} | reason: {}",
            side, exit_price, pnl, new_equity, reason,
        )
        return pnl

    def log_decision(
        self, direction: str, agreement: int, consulted: bool, action: str, reasoning: str
    ) -> None:
        self.conn.execute(
            "INSERT INTO decisions(ts, direction, agreement, consulted_claude, action, reasoning) "
            "VALUES(?,?,?,?,?,?)",
            (_utcnow().isoformat(), direction, agreement, int(consulted), action, reasoning),
        )
        self.conn.commit()

    # ------------------------------------------------------------------ #
    # Reporting                                                          #
    # ------------------------------------------------------------------ #
    def daily_stats(self) -> dict[str, Any]:
        self._roll_periods()
        day_start = self._getf("day_start_equity", self.equity)
        week_start = self._getf("week_start_equity", self.equity)
        start = self._getf("starting_equity", self.equity)
        today = self._get("day_date")
        closed_today = self.conn.execute(
            "SELECT COUNT(*) c, COALESCE(SUM(pnl_usd),0) p FROM trades "
            "WHERE status='CLOSED' AND substr(closed_at,1,10)=?",
            (today,),
        ).fetchone()
        return {
            "date_utc": today,
            "equity": round(self.equity, 2),
            "starting_equity": round(start, 2),
            "total_return_pct": round((self.equity / start - 1) * 100, 2) if start else 0,
            "day_pnl_usd": round(self.equity - day_start, 2),
            "day_return_pct": round((self.equity / day_start - 1) * 100, 2) if day_start else 0,
            "week_return_pct": round((self.equity / week_start - 1) * 100, 2) if week_start else 0,
            "trades_closed_today": closed_today["c"],
            "pnl_closed_today_usd": round(closed_today["p"], 2),
            "wins": self._geti("wins"),
            "losses": self._geti("losses"),
            "consecutive_losses": self._geti("consecutive_losses"),
            "win_rate": round(self._dynamic_win_rate() * 100, 1),
        }
