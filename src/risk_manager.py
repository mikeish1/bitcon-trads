"""
Risk manager (multi-asset spot, long-only).

- Per-asset positions (one open position per coin), tracked in SQLite so they
  survive restarts. State + a full trade log persist across redeploys.
- Portfolio-level controls: max concurrent positions, max total exposure across
  all coins, and a per-asset allocation cap.
- Per-coin cooldown; global daily/weekly loss limits and a consecutive-loss
  circuit breaker (across the whole portfolio).
- Donchian sizing (binary long/flat with a chandelier trail) plus the legacy
  ATR-stop Kelly sizing for high_conviction mode.

Equity:
  * broker venues (Alpaca paper / live): real account value = quote cash +
    sum(base holdings x price).
  * internal simulation: a cash ledger (paper_cash) + open positions marked to
    market.

Tune numbers in config/trading_config.yaml.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional

from loguru import logger

from src.settings_service import CapitalSettingsService


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _base_of(symbol: str) -> str:
    return symbol.split("/")[0]


class RiskManager:
    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg
        self.uses_broker = cfg["runtime"]["uses_broker"]
        self.real_money = cfg["runtime"]["real_money"]
        r, s, e = cfg["risk"], cfg["safety"], cfg["exits"]
        pf = cfg.get("portfolio", {})

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
        self.chandelier_mult = cfg.get("strategy", {}).get("donchian", {}).get(
            "atr_trail_mult", self.atr_trail_mult)

        self.daily_loss_limit = s["daily_loss_limit_pct"]
        self.weekly_loss_limit = s["weekly_loss_limit_pct"]
        self.max_consec_losses = s["max_consecutive_losses"]
        self.cooldown_minutes = s["cooldown_minutes"]
        self.max_trades_per_day = s["max_trades_per_day"]

        self.max_concurrent = pf.get("max_concurrent_positions", 3)
        self.max_total_exposure = pf.get("max_total_exposure_pct", 0.90)
        self.per_asset_alloc = pf.get("per_asset_alloc_pct", 0.30)

        # Centralized, user-adjustable deployable-capital limit. This is the SINGLE
        # cap on the total dollar envelope the spot bot may have committed at once.
        # It subsumes the legacy `portfolio.max_total_exposure_pct` (which remains
        # the default), and can be tightened to a fixed USD amount, a % of equity
        # or cash, or a combination - via YAML, env vars, or the settings service.
        self.settings = CapitalSettingsService(cfg)
        self.capital_policy = self.settings.policy("spot")
        logger.info("Spot {} (source: {}).", self.capital_policy.describe(),
                    self.settings.resolve_mapping("spot")[1])

        vt = cfg.get("strategy", {}).get("vol_target", {})
        self.vol_target_enabled = vt.get("enabled", False)
        self.vol_target_daily = vt.get("target_daily_vol", 0.04)

        # --- Explicit per-trade risk budgeting + global vol targeting (opt-in) ---
        # Defaults reproduce the legacy sizing exactly (disabled), so existing
        # configs/tests are untouched until risk_budget.enabled is set.
        rb = r.get("risk_budget", {})
        self.risk_budget_enabled = bool(rb.get("enabled", False))
        self.rb_risk_per_trade = float(rb.get("risk_per_trade_pct", self.risk_per_trade))
        self.rb_atr_stop_mult = float(rb.get("atr_stop_mult", self.atr_stop_mult))
        self.rb_target_vol = float(rb.get("target_portfolio_vol", 0.0) or 0.0)
        self.rb_scalar_min = float(rb.get("vol_scalar_min", 0.5))
        self.rb_scalar_max = float(rb.get("vol_scalar_max", 2.0))
        self.rb_vol_source = str(rb.get("vol_source", "proxy")).lower()
        self.rb_vol_lookback = int(rb.get("vol_lookback_days", 20))

        # --- Staged profit-taking / ratcheting exits (opt-in) --------------------
        pt = cfg.get("strategy", {}).get("profit_taking", {})
        self.profit_taking_enabled = bool(pt.get("enabled", False))
        raw_tiers = pt.get("tiers", []) or []
        # Keep only well-formed, ascending tiers; clamp scale fractions to (0, 1].
        self.pt_tiers: list[dict[str, float]] = []
        for t in raw_tiers:
            try:
                pa = float(t["profit_atr"]); sp = float(t["scale_pct"])
            except (KeyError, TypeError, ValueError):
                continue
            if pa > 0 and 0 < sp <= 1:
                self.pt_tiers.append({"profit_atr": pa, "scale_pct": sp})
        self.pt_tiers.sort(key=lambda t: t["profit_atr"])
        self.pt_breakeven_after = int(pt.get("breakeven_after_tier", 1))
        self.pt_breakeven_buffer = float(pt.get("breakeven_buffer_atr", 0.5))
        self.pt_ratchet_mults = [float(m) for m in (pt.get("ratchet_trail_mults") or [])]
        self.pt_time_stop_days = int(pt.get("time_stop_days", 0) or 0)

        self.conn = sqlite3.connect(cfg["runtime"]["db_path"], check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        # WAL + a busy timeout so this bot can share one DB file with the carry/ETF
        # sibling bots (run together via src.run_all) without "database is locked".
        if cfg["runtime"]["db_path"] != ":memory:":
            try:
                self.conn.execute("PRAGMA journal_mode=WAL")
                self.conn.execute("PRAGMA busy_timeout=5000")
            except sqlite3.OperationalError:
                pass
        self._init_db()
        self._seed()

    # ------------------------------------------------------------------ #
    def _init_db(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT,
                opened_at TEXT, closed_at TEXT,
                entry_price REAL, exit_price REAL,
                qty REAL, cost_usd REAL, entry_fee REAL, exit_fee REAL,
                stop_price REAL, take_price REAL, current_stop REAL, peak_price REAL,
                stop_order_id TEXT,
                pnl_usd REAL, status TEXT, mode TEXT, reason TEXT,
                entry_atr REAL, orig_qty REAL, tranches_done INTEGER DEFAULT 0,
                scaled_pnl REAL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT, symbol TEXT, action TEXT, conviction INTEGER,
                consulted_claude INTEGER, reasoning TEXT
            );
            CREATE TABLE IF NOT EXISTS equity_history (
                day TEXT PRIMARY KEY, equity REAL, ts TEXT
            );
            """
        )
        # Migrate older single-asset DBs.
        cols = [r["name"] for r in self.conn.execute("PRAGMA table_info(trades)")]
        for col in ("peak_price", "symbol"):
            if col not in cols:
                self.conn.execute(f"ALTER TABLE trades ADD COLUMN {col} {'REAL' if col=='peak_price' else 'TEXT'}")
        # Staged profit-taking bookkeeping (added when scaled exits shipped).
        for col, ddl in (("entry_atr", "REAL"), ("orig_qty", "REAL"),
                         ("tranches_done", "INTEGER DEFAULT 0"), ("scaled_pnl", "REAL DEFAULT 0")):
            if col not in cols:
                self.conn.execute(f"ALTER TABLE trades ADD COLUMN {col} {ddl}")
        dcols = [r["name"] for r in self.conn.execute("PRAGMA table_info(decisions)")]
        if "symbol" not in dcols:
            self.conn.execute("ALTER TABLE decisions ADD COLUMN symbol TEXT")
        self.conn.commit()

    def _seed(self) -> None:
        if self._get("paper_cash") is None:
            now = _utcnow()
            self._set("paper_cash", self.default_capital)
            self._set("day_date", now.date().isoformat())
            self._set("day_start_equity", self.default_capital)
            self._set("week_id", f"{now.isocalendar().year}-{now.isocalendar().week}")
            self._set("week_start_equity", self.default_capital)
            self._set("consecutive_losses", 0)
            self._set("wins", 0)
            self._set("losses", 0)
            logger.info("State seeded. Paper cash ${:.2f}.", self.default_capital)

    # ------------------------------------------------------------------ #
    def _get(self, k: str) -> Optional[str]:
        row = self.conn.execute("SELECT value FROM state WHERE key=?", (k,)).fetchone()
        return row["value"] if row else None

    def _set(self, k: str, v: Any) -> None:
        self.conn.execute("INSERT INTO state(key,value) VALUES(?,?) "
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
    # Positions                                                          #
    # ------------------------------------------------------------------ #
    def open_position(self, symbol: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM trades WHERE status='OPEN' AND symbol=? ORDER BY id DESC LIMIT 1",
            (symbol,)).fetchone()

    def open_positions(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM trades WHERE status='OPEN'").fetchall())

    def open_value(self, prices: dict[str, float]) -> float:
        """Mark-to-market value of all open positions (prices keyed by base asset)."""
        total = 0.0
        for p in self.open_positions():
            base = _base_of(p["symbol"] or "")
            total += p["qty"] * prices.get(base, p["entry_price"])
        return total

    # ------------------------------------------------------------------ #
    # Equity                                                             #
    # ------------------------------------------------------------------ #
    def current_equity(self, balances: dict[str, float], prices: dict[str, float]) -> float:
        if self.uses_broker:
            quote = balances.get(self.cfg.get("quote_ccy", "USDT"), 0.0)
            holdings = sum(balances.get(b, 0.0) * pr for b, pr in prices.items())
            return quote + holdings
        return self._getf("paper_cash", self.default_capital) + self.open_value(prices)

    def available_quote(self, balances: dict[str, float]) -> float:
        if self.uses_broker:
            return balances.get(self.cfg.get("quote_ccy", "USDT"), 0.0)
        return self._getf("paper_cash", self.default_capital)

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

    def _trades_today(self) -> int:
        today = _utcnow().date().isoformat()
        return int(self.conn.execute(
            "SELECT COUNT(*) c FROM trades WHERE substr(opened_at,1,10)=?", (today,)).fetchone()["c"])

    # ------------------------------------------------------------------ #
    # Safety gate (per asset + portfolio)                                #
    # ------------------------------------------------------------------ #
    def can_open_trade(self, symbol: str, equity: float, n_open: int) -> tuple[bool, str]:
        self._roll_periods(equity)
        if self.open_position(symbol) is not None:
            return False, f"already in {symbol}"
        if n_open >= self.max_concurrent:
            return False, f"max concurrent positions ({self.max_concurrent})"
        day_start = self._getf("day_start_equity", equity)
        if day_start > 0 and (equity - day_start) / day_start <= -self.daily_loss_limit:
            return False, f"daily loss limit ({(equity/day_start-1):.2%})"
        week_start = self._getf("week_start_equity", equity)
        if week_start > 0 and (equity - week_start) / week_start <= -self.weekly_loss_limit:
            return False, f"weekly loss limit ({(equity/week_start-1):.2%})"
        if self._geti("consecutive_losses") >= self.max_consec_losses:
            return False, f"circuit breaker ({self._geti('consecutive_losses')} losses in a row)"
        if self._trades_today() >= self.max_trades_per_day:
            return False, f"max trades/day ({self.max_trades_per_day})"
        last = self._get(f"last_close_ts:{symbol}")
        if last:
            try:
                mins = (_utcnow() - datetime.fromisoformat(last)).total_seconds() / 60
                if mins < self.cooldown_minutes:
                    return False, f"{symbol} cooldown ({mins:.0f}/{self.cooldown_minutes} min)"
            except ValueError:
                pass
        return True, "ok"

    # ------------------------------------------------------------------ #
    # Sizing                                                             #
    # ------------------------------------------------------------------ #
    def portfolio_vol_scalar(self, portfolio_vol: float | None) -> float:
        """Global exposure scalar that nudges total exposure toward
        `risk_budget.target_portfolio_vol`, clamped to [vol_scalar_min, vol_scalar_max].

        portfolio_vol is an estimate of current daily portfolio/strategy vol (the
        caller's responsibility - e.g. mean ATR% across held/active coins, which for
        a ~0.8-correlated crypto book is a reasonable expected-vol proxy). Returns
        1.0 (no-op) when targeting is disabled or the estimate is unusable. The
        scalar only ever moves size between `vol_scalar_min` and `vol_scalar_max` of
        the risk-budget base; hard caps applied afterwards still bind."""
        if not self.risk_budget_enabled or self.rb_target_vol <= 0:
            return 1.0
        if not portfolio_vol or portfolio_vol != portfolio_vol or portfolio_vol <= 0:
            return 1.0
        return float(min(self.rb_scalar_max, max(self.rb_scalar_min,
                                                 self.rb_target_vol / portfolio_vol)))

    def record_equity(self, equity: float) -> None:
        """Snapshot today's portfolio equity (one row per UTC day, last write wins).
        Builds the equity curve the `realized` vol-target source reads from. Cheap
        no-op-ish upsert; harmless when risk budgeting is off."""
        if equity is None or equity != equity:
            return
        now = _utcnow()
        self.conn.execute(
            "INSERT INTO equity_history(day, equity, ts) VALUES(?,?,?) "
            "ON CONFLICT(day) DO UPDATE SET equity=excluded.equity, ts=excluded.ts",
            (now.date().isoformat(), float(equity), now.isoformat()))
        self.conn.commit()

    def realized_portfolio_vol(self, lookback: int | None = None) -> float | None:
        """Realized DAILY portfolio vol = stdev of daily equity returns over the last
        `lookback` days. Returns None until at least 3 daily snapshots exist (so the
        caller can fall back to the ATR proxy during warm-up)."""
        lb = lookback or self.rb_vol_lookback
        rows = self.conn.execute(
            "SELECT equity FROM equity_history ORDER BY day DESC LIMIT ?", (lb + 1,)).fetchall()
        eq = [r["equity"] for r in reversed(rows) if r["equity"] and r["equity"] > 0]
        if len(eq) < 3:
            return None
        rets = [eq[i] / eq[i - 1] - 1.0 for i in range(1, len(eq))]
        mean = sum(rets) / len(rets)
        var = sum((x - mean) ** 2 for x in rets) / len(rets)
        return var ** 0.5

    def effective_portfolio_vol(self, proxy_vol: float | None) -> float | None:
        """Resolve the portfolio-vol estimate the global scalar should use, honouring
        `risk_budget.vol_source`: realized equity-curve vol when configured and
        available, otherwise the caller-supplied ATR proxy."""
        if self.rb_vol_source == "realized":
            realized = self.realized_portfolio_vol()
            if realized is not None:
                return realized
        return proxy_vol

    def size_for_asset(self, equity: float, available_quote: float, open_value: float,
                       atr_pct: float | None = None, portfolio_vol: float | None = None,
                       regime_factor: float = 1.0) -> dict[str, Any]:
        """Portfolio-aware allocation for one new position (trend-follower).

        Layers (each only shrinks toward the hard caps; none of them can raise risk
        above what the caps already permit):
          1. base per-asset cap (`per_asset_alloc` x equity),
          2. legacy vol_target shrink for coins more volatile than target,
          3. risk-budget cap so an ATR stop-out costs ~`risk_per_trade_pct` of equity,
          4. global vol-target scalar toward `target_portfolio_vol`,
          5. regime size factor (1.0 risk-on .. e.g. 0.0/0.2 risk-off),
        then the hard caps: deployable-capital envelope and available cash.
        """
        per_asset_cap = equity * self.per_asset_alloc
        if self.vol_target_enabled and atr_pct and atr_pct > 0:
            # Shrink size for coins more volatile than target (clamp 0.2x..1x).
            per_asset_cap *= min(1.0, max(0.2, self.vol_target_daily / atr_pct))

        base = per_asset_cap
        risk_notional = None
        if self.risk_budget_enabled and atr_pct and atr_pct > 0 and self.rb_atr_stop_mult > 0:
            # Notional s.t. notional * stop_distance_pct == equity * risk_per_trade.
            stop_distance_pct = atr_pct * self.rb_atr_stop_mult
            risk_notional = (equity * self.rb_risk_per_trade) / stop_distance_pct
            base = min(base, risk_notional)

        vol_scalar = self.portfolio_vol_scalar(portfolio_vol)
        regime_factor = max(0.0, float(regime_factor))
        base = base * vol_scalar * regime_factor

        # Total-envelope cap comes from the centralized deployable-capital policy;
        # relative allocation (per-asset cap, available-cash limit) is unchanged.
        exposure_budget = float(self.capital_policy.remaining_capacity(
            equity, available_quote, open_value))
        spend = min(base, exposure_budget, available_quote * self.max_position_pct)
        return {"spend_usd": spend, "viable": spend >= self.min_notional,
                "per_asset_cap": per_asset_cap, "risk_notional": risk_notional,
                "vol_scalar": vol_scalar, "regime_factor": regime_factor,
                "exposure_budget": exposure_budget}

    def size_rotation(self, equity: float, available_quote: float, open_value: float,
                      top_k: int, portfolio_vol: float | None = None,
                      regime_factor: float = 1.0) -> dict[str, Any]:
        """Equal-weight (1/K of equity) sizing for a momentum-rotation entry,
        still bounded by the portfolio exposure cap and available cash. The optional
        global vol scalar and regime factor apply the same way as size_for_asset."""
        target = equity / max(top_k, 1)
        target *= self.portfolio_vol_scalar(portfolio_vol) * max(0.0, float(regime_factor))
        exposure_budget = float(self.capital_policy.remaining_capacity(
            equity, available_quote, open_value))
        spend = min(target, exposure_budget, available_quote * self.max_position_pct)
        return {"spend_usd": spend, "viable": spend >= self.min_notional}

    def maybe_reload_policy(self) -> bool:
        """Hot-reload the deployable-capital limit if the override file changed on
        disk. Lets the user (or a frontend) re-cap capital WITHOUT a restart. Falls
        back to the existing policy if the new mapping is invalid. Returns True if
        the policy changed."""
        if not self.settings.override_changed_on_disk():
            return False
        try:
            new_policy = self.settings.policy("spot")
        except Exception as exc:  # keep the last-known-good policy on bad input
            logger.warning("Capital-limit reload rejected (keeping current): {}", exc)
            return False
        if new_policy == self.capital_policy:
            return False
        logger.warning("Deployable-capital limit reloaded: {} -> {}",
                       self.capital_policy.describe(), new_policy.describe())
        self.capital_policy = new_policy
        return True

    # Small state passthrough (used by the rotation clock in the loop).
    def state_get(self, key: str) -> Optional[str]:
        return self._get(key)

    def state_set(self, key: str, value: Any) -> None:
        self._set(key, value)

    def chandelier_stop(self, peak: float, atr: float, mult: float | None = None) -> float:
        """Chandelier trail = peak - mult*ATR. `mult` overrides the default donchian
        trail multiple (used by staged profit-taking to tighten the runner)."""
        return peak - (self.chandelier_mult if mult is None else mult) * atr

    def runner_trail_mult(self, tranches_done: int) -> float:
        """Chandelier multiple for the residual runner after `tranches_done` tiers
        have scaled out. Tightens per `ratchet_trail_mults` (clamped to the last
        entry); falls back to the default donchian trail when not configured."""
        if not self.pt_ratchet_mults:
            return self.chandelier_mult
        idx = min(max(tranches_done, 0), len(self.pt_ratchet_mults) - 1)
        return self.pt_ratchet_mults[idx]

    def profit_taking_plan(self, pos: sqlite3.Row, price: float, atr: float) -> dict[str, Any]:
        """Decide staged-exit actions for an OPEN position at the current price.

        Profit is measured in multiples of the ATR captured AT ENTRY (entry_atr) so
        a moving ATR can't retroactively change which tiers have fired. Returns:
          scale_fraction  : fraction of the ORIGINAL position to sell now (0 if none),
          new_tranches    : tranches_done after this action,
          trail_mult      : chandelier multiple to use on the remainder,
          breakeven_floor : a stop floor (entry + buffer*ATR) once enough tiers fired,
                            else None,
          reason          : short human string for logs/exit records.
        No-op (scale 0, default trail, no floor) when profit-taking is disabled, ATR
        or entry data is missing, or all tiers have already fired.
        """
        out: dict[str, Any] = {"scale_fraction": 0.0, "new_tranches": int(pos["tranches_done"] or 0),
                               "trail_mult": self.runner_trail_mult(int(pos["tranches_done"] or 0)),
                               "breakeven_floor": None, "reason": ""}
        if not self.profit_taking_enabled or not self.pt_tiers:
            return out
        entry_atr = pos["entry_atr"] if pos["entry_atr"] and pos["entry_atr"] > 0 else atr
        if not entry_atr or entry_atr != entry_atr or entry_atr <= 0:
            return out
        entry_price = pos["entry_price"]
        done = int(pos["tranches_done"] or 0)
        profit_atr = (price - entry_price) / entry_atr

        # Fire every tier whose threshold has been crossed but not yet taken.
        scale_fraction = 0.0
        fired = []
        while done < len(self.pt_tiers) and profit_atr >= self.pt_tiers[done]["profit_atr"]:
            scale_fraction += self.pt_tiers[done]["scale_pct"]
            fired.append(self.pt_tiers[done]["profit_atr"])
            done += 1
        scale_fraction = min(scale_fraction, 1.0)

        out["new_tranches"] = done
        out["trail_mult"] = self.runner_trail_mult(done)
        if done >= self.pt_breakeven_after > 0:
            out["breakeven_floor"] = entry_price + self.pt_breakeven_buffer * entry_atr
        if scale_fraction > 0:
            out["scale_fraction"] = scale_fraction
            out["reason"] = (f"scale-out {scale_fraction:.0%} at "
                             f"+{'/'.join(f'{x:g}' for x in fired)}xATR (profit {profit_atr:.1f}R)")
        return out

    def stop_limit_price(self, stop_price: float) -> float:
        return stop_price * (1 - self.stop_limit_offset)

    def trailing_stop(self, price: float, atr: float) -> float:
        return price - max(self.atr_trail_mult * atr, price * self.min_stop_pct)

    def _win_rate(self) -> float:
        w, l = self._geti("wins"), self._geti("losses")
        return 0.5 if (w + l) < 10 else w / (w + l)

    def size_buy(self, equity: float, available_usdt: float, price: float, atr: float) -> dict[str, Any]:
        """Legacy high_conviction ATR-stop Kelly sizing (single asset)."""
        stop_distance = max(self.atr_stop_mult * atr, price * self.min_stop_pct)
        win = self._win_rate()
        kelly_star = max(win - (1.0 - win) / self.kelly_payoff, 0.0)
        rf = max(min(self.kelly_fraction * kelly_star, self.risk_per_trade), self.risk_per_trade * 0.25)
        spend = min(equity * rf * price / stop_distance, available_usdt * self.max_position_pct)
        return {"spend_usd": spend, "stop_price": price - stop_distance,
                "take_price": price + self.take_profit_R * stop_distance,
                "risk_fraction": rf, "viable": spend >= self.min_notional}

    # ------------------------------------------------------------------ #
    # Lifecycle                                                          #
    # ------------------------------------------------------------------ #
    def record_open(self, symbol: str, fill: dict[str, Any], stop_price: float, take_price: float,
                    stop_order_id: Optional[str], reason: str, peak_price: Optional[float] = None,
                    entry_atr: Optional[float] = None) -> int:
        mode = "LIVE" if self.real_money else "PAPER"
        peak = peak_price if peak_price is not None else fill["price"]
        cur = self.conn.execute(
            "INSERT INTO trades(symbol, opened_at, entry_price, qty, cost_usd, entry_fee, "
            "stop_price, take_price, current_stop, peak_price, stop_order_id, status, mode, reason, "
            "entry_atr, orig_qty, tranches_done, scaled_pnl) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (symbol, _utcnow().isoformat(), fill["price"], fill["qty"], fill["cost"], fill["fee"],
             stop_price, take_price, stop_price, peak, stop_order_id, "OPEN", mode, reason,
             entry_atr, fill["qty"], 0, 0.0))
        if not self.uses_broker:  # sim cash ledger
            self._set("paper_cash", self._getf("paper_cash") - (fill["cost"] + fill["fee"]))
        self.conn.commit()
        logger.info("OPENED {} {:.6f} @ {:.4f} | stop {:.4f} [{}]",
                    symbol, fill["qty"], fill["price"], stop_price, mode)
        return int(cur.lastrowid)

    def reduce_position(self, trade: sqlite3.Row, fill: dict[str, Any], new_tranches: int,
                        reason: str) -> float:
        """Book a PARTIAL scale-out: sell `fill['qty']` of an open position, leaving
        it OPEN with proportionally reduced qty / cost basis / entry fee. Realized
        PnL on the sold slice is accumulated in `scaled_pnl` (folded into the final
        close so win/loss + circuit-breaker stats are only updated once, at close).
        Returns the realized PnL of this slice."""
        qty_before = trade["qty"]
        sell_qty = min(fill["qty"], qty_before)
        if qty_before <= 0 or sell_qty <= 0:
            return 0.0
        frac = sell_qty / qty_before
        sold_cost = (trade["cost_usd"] or 0.0) * frac
        sold_entry_fee = (trade["entry_fee"] or 0.0) * frac
        proceeds = fill.get("proceeds", fill["price"] * sell_qty)
        exit_fee = fill.get("fee", 0.0)
        realized = proceeds - exit_fee - sold_cost - sold_entry_fee
        new_scaled = (trade["scaled_pnl"] or 0.0) + realized
        self.conn.execute(
            "UPDATE trades SET qty=?, cost_usd=?, entry_fee=?, tranches_done=?, scaled_pnl=? WHERE id=?",
            (qty_before - sell_qty, (trade["cost_usd"] or 0.0) - sold_cost,
             (trade["entry_fee"] or 0.0) - sold_entry_fee, int(new_tranches), new_scaled, trade["id"]))
        if not self.uses_broker:  # sim cash ledger gets the proceeds back
            self._set("paper_cash", self._getf("paper_cash") + (proceeds - exit_fee))
        self.conn.commit()
        logger.info("SCALED {} -{:.6f} @ {:.4f} | realized ${:.2f} | {}",
                    trade["symbol"], sell_qty, fill["price"], realized, reason)
        return realized

    def update_trail(self, trade_id: int, peak: float, stop: float, stop_order_id: Optional[str]) -> None:
        self.conn.execute("UPDATE trades SET peak_price=?, current_stop=?, stop_order_id=? WHERE id=?",
                          (peak, stop, stop_order_id, trade_id))
        self.conn.commit()

    def record_close(self, trade: sqlite3.Row, exit_price: float, exit_fee: float, reason: str) -> float:
        entry, qty = trade["entry_price"], trade["qty"]
        entry_fee = trade["entry_fee"] or 0.0
        proceeds = exit_price * qty - exit_fee
        # `cost_usd`/`entry_fee` already reflect prior scale-outs; add the realized
        # PnL booked on those tranches so the trade's total PnL is correct.
        pnl = proceeds - (trade["cost_usd"] + entry_fee) + (trade["scaled_pnl"] or 0.0)
        self.conn.execute(
            "UPDATE trades SET closed_at=?, exit_price=?, exit_fee=?, pnl_usd=?, status='CLOSED', "
            "reason=? WHERE id=?",
            (_utcnow().isoformat(), exit_price, exit_fee, pnl, reason, trade["id"]))
        if not self.uses_broker:
            self._set("paper_cash", self._getf("paper_cash") + proceeds)
        self._set(f"last_close_ts:{trade['symbol']}", _utcnow().isoformat())
        if pnl >= 0:
            self._set("wins", self._geti("wins") + 1)
            self._set("consecutive_losses", 0)
        else:
            self._set("losses", self._geti("losses") + 1)
            self._set("consecutive_losses", self._geti("consecutive_losses") + 1)
        self.conn.commit()
        logger.info("CLOSED {} @ {:.4f} | PnL ${:.2f} | {}", trade["symbol"], exit_price, pnl, reason)
        return pnl

    def log_decision(self, symbol: str, action: str, conviction: int, consulted: bool, reasoning: str) -> None:
        self.conn.execute(
            "INSERT INTO decisions(ts, symbol, action, conviction, consulted_claude, reasoning) "
            "VALUES(?,?,?,?,?,?)",
            (_utcnow().isoformat(), symbol, action, conviction, int(consulted), reasoning))
        self.conn.commit()

    def reconcile(self, balances: dict[str, float], prices: dict[str, float],
                  atrs: Optional[dict[str, float]] = None) -> None:
        """Broker-only reconciliation, BOTH directions:

          1. CLOSE DB positions whose coins are no longer in the account (the stop
             likely filled while we were offline).
          2. ADOPT account holdings that have NO open DB row, immediately attaching a
             protective stop. These are orphans from a crash between the live fill and
             `record_open` (see main_loop._try_enter): without adoption such a position
             runs with no software stop and no trail - unbounded downside on real
             money. `atrs` (base -> ATR) lets the adopted stop use the chandelier;
             when absent we fall back to a `min_stop_pct` floor until the next cycle
             recomputes a proper trail.
        """
        if not self.uses_broker:
            return
        tracked = {_base_of(p["symbol"] or "") for p in self.open_positions()}

        # 1) Coins gone from the account -> close the stale DB row.
        for pos in self.open_positions():
            base = _base_of(pos["symbol"] or "")
            price = prices.get(base, pos["entry_price"])
            dust = self.min_notional / price if price else 0.0
            if balances.get(base, 0.0) < dust and pos["qty"] > dust:
                logger.warning("Reconcile: {} gone from account - closing (stop likely filled).",
                               pos["symbol"])
                self.record_close(pos, pos["current_stop"] or price, 0.0, "offline stop fill")

        # 2) Untracked holdings in our universe -> adopt + protect (crash recovery).
        # Assumes a DEDICATED account (the bot already marks the WHOLE account as its
        # equity). Disable via reconcile.adopt_orphans if the account is shared.
        if not (self.cfg.get("reconcile", {}) or {}).get("adopt_orphans", True):
            return
        quote = self.cfg.get("quote_ccy", "USDT")
        universe = set(self.cfg.get("universe_symbols") or [])
        for base, qty in balances.items():
            if base == quote or base in tracked or not qty:
                continue
            symbol = f"{base}/{quote}"
            if symbol not in universe:
                continue                     # only adopt coins this bot actually trades
            price = prices.get(base)
            if not price or qty * price < self.min_notional:
                continue
            atr = (atrs or {}).get(base, 0.0)
            stop = (self.chandelier_stop(price, atr) if atr and atr > 0
                    else price * (1 - self.min_stop_pct))
            logger.warning("Reconcile: ADOPTING untracked holding {} {:.6f} @ {:.4f} "
                           "(orphaned fill); protective stop {:.4f}.", symbol, qty, price, stop)
            self.record_open(symbol, {"price": price, "qty": qty, "cost": qty * price, "fee": 0.0},
                             stop, 0.0, None, "adopted orphaned holding (reconcile)",
                             peak_price=price, entry_atr=atr or None)

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
            "mode": "LIVE" if self.real_money else "PAPER",
            "equity": round(equity, 2),
            "open_positions": len(self.open_positions()),
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
