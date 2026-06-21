"""
Autonomous heartbeat loop - multi-asset spot, long-only.

Dynamically trades the whole configured universe (BTC, ETH, SOL, ...). Each cycle
it manages every open position and looks for fresh breakouts, subject to
portfolio-level caps (max concurrent positions, max total exposure).

Active strategy: daily Donchian breakout entry + ATR chandelier trailing exit
(the validated one). `strategy.mode` selects the entry-signal generator; exits
and sizing use the trend-follower model.

Safety:
  * PAPER by default. Real orders need BOTH PAPER_TRADING=false AND
    LIVE_TRADING_ENABLED=true (Alpaca live also needs ALPACA_PAPER=false).
  * Coins the venue doesn't list are skipped at startup.
  * Optional exchange-side stops, Telegram alerts, daily/weekly loss limits,
    circuit breaker, per-coin cooldown.

Ctrl+C / SIGTERM shut down cleanly.
"""
from __future__ import annotations

import signal
import sys
import time
from datetime import timezone
from typing import Any, Optional

from loguru import logger

from src.claude_orchestrator import ClaudeOrchestrator
from src.config import load_config
from src.data_pipeline import DataPipeline, build_exchange
from src.executor import SpotExecutor
from src.momentum_allocator import MomentumRotation
from src.notifications import Notifier
from src.risk_manager import RiskManager
from src.strategy import Strategy, DonchianStrategy


def _base_of(symbol: str) -> str:
    return symbol.split("/")[0]


class TradingBot:
    def __init__(self) -> None:
        self.cfg = load_config()
        self._configure_logging()

        rt = self.cfg["runtime"]
        if rt["real_money"]:
            mode = "LIVE (REAL MONEY)"
        elif rt["place_orders"]:
            mode = "PAPER-BROKER (Alpaca paper - no real money)"
        else:
            mode = "PAPER (internal simulation)"
        self._mode = mode

        self.exchange = build_exchange(self.cfg)
        self.data = DataPipeline(self.cfg, self.exchange)
        self.claude = ClaudeOrchestrator(self.cfg)
        self.strategy_mode = self.cfg["strategy"].get("mode", "donchian")
        self.strategy = (DonchianStrategy(self.cfg) if self.strategy_mode == "donchian"
                         else Strategy(self.cfg, claude_orchestrator=self.claude))
        self.risk = RiskManager(self.cfg)
        self.executor = SpotExecutor(self.cfg, self.exchange)
        self.notifier = Notifier(self.cfg)

        self.primary_tf = self.cfg["market"]["primary_timeframe"]
        self.poll_seconds = self.cfg["market"]["poll_seconds"]
        self.use_exchange_stop = self.cfg["exits"]["use_exchange_stop"]
        self.overrides = self.cfg["universe"].get("overrides", {}) or {}
        sd = self.cfg["strategy"]
        self.regime_enabled = sd.get("btc_regime", {}).get("enabled", False)
        self.regime_ma = sd.get("btc_regime", {}).get("ma_period", 100)
        self._btc_risk_on = True

        # Allocation mode: first_come (default) or momentum_rotation.
        self.alloc_mode = sd.get("allocation", {}).get("mode", "first_come")
        self.rotation: Optional[MomentumRotation] = None
        if self.alloc_mode == "momentum_rotation":
            if self.strategy_mode != "donchian":
                logger.warning("momentum_rotation needs donchian entries; using first_come.")
                self.alloc_mode = "first_come"
            else:
                self.rotation = MomentumRotation(self.cfg)
                if self.risk.max_concurrent < self.rotation.top_k:
                    logger.warning("Raising max_concurrent_positions {} -> {} for rotation.",
                                   self.risk.max_concurrent, self.rotation.top_k)
                    self.risk.max_concurrent = self.rotation.top_k
        self._quote = self.cfg.get("quote_ccy", "USDT")
        self.running = True
        self._last_candle_ts: dict[str, Any] = {}
        self._last_summary_day: Optional[str] = None
        self._cb_notified = False

        logger.info("=" * 70)
        logger.info("Multi-Asset Spot Bot | venue={} | mode={} | strategy={}",
                    rt["exchange_id"], mode, self.strategy_mode)
        logger.info("=" * 70)

        signal.signal(signal.SIGINT, self._sig)
        signal.signal(signal.SIGTERM, self._sig)

    def _configure_logging(self) -> None:
        logger.remove()
        logger.add(sys.stdout, level=self.cfg["logging"]["level"],
                   format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
                          "<level>{level: <7}</level> | <level>{message}</level>")

    def _sig(self, signum, _frame) -> None:
        logger.warning("Signal {} - shutting down gracefully...", signum)
        self.running = False

    # ------------------------------------------------------------------ #
    def run(self) -> None:
        wanted = self.cfg["universe_symbols"]
        self.symbols = self.data.available_symbols(wanted)
        if not self.symbols:
            logger.error("No tradable symbols on this venue. Exiting.")
            return
        logger.info("Universe ({}): {}", len(self.symbols), ", ".join(self.symbols))

        # Startup reconcile (broker venues): close DB positions whose coins are gone.
        try:
            balances = self.data.fetch_balances()
            prices = self._all_prices()
            self.risk.reconcile(balances, prices)
        except Exception as exc:
            logger.warning("Startup reconcile skipped: {}", exc)

        pf = self.cfg["portfolio"]
        logger.info("Ready. Donchian {}-day breakout; {}x ATR trail. Max {} positions, "
                    "<= {:.0%} total exposure, <= {:.0%}/asset.",
                    self.cfg["strategy"]["donchian"]["entry_period"],
                    self.cfg["strategy"]["donchian"]["atr_trail_mult"],
                    pf["max_concurrent_positions"], pf["max_total_exposure_pct"], pf["per_asset_alloc_pct"])
        if self.alloc_mode == "momentum_rotation" and self.rotation is not None:
            logger.info("Allocation: MOMENTUM ROTATION - hold top-{} by {}-day momentum, "
                        "rotate every {}d (keep-band {}).", self.rotation.top_k,
                        self.rotation.lookback_days, self.rotation.rebalance_days,
                        self.rotation.keep_band)
        else:
            logger.info("Allocation: FIRST-COME (each breakout sized independently).")
        self.notifier.startup(self.cfg["runtime"]["exchange_id"],
                              ", ".join(self.symbols), self._mode)

        while self.running:
            try:
                self._cycle()
            except Exception as exc:
                logger.exception("Cycle error (continuing): {}", exc)
                self.notifier.error(f"Cycle error (continuing): {exc}")
            self._sleep(self.poll_seconds)
        logger.info("Loop stopped.")

    def _sleep(self, seconds: int) -> None:
        for _ in range(seconds):
            if not self.running:
                return
            time.sleep(1)

    def _all_prices(self) -> dict[str, float]:
        """Latest price per base asset (best-effort; used for reconcile/equity)."""
        prices: dict[str, float] = {}
        for sym in getattr(self, "symbols", self.cfg["universe_symbols"]):
            try:
                frames = self.data.get_frames(sym)
                prices[_base_of(sym)] = self.data.last_price(frames, self.primary_tf)
            except Exception:
                pass
        return prices

    # ------------------------------------------------------------------ #
    def _cycle(self) -> None:
        # Pick up a changed deployable-capital limit without a restart (no-op if
        # the override file is untouched).
        self.risk.maybe_reload_policy()
        balances = self.data.fetch_balances()
        snap: dict[str, dict] = {}   # symbol -> {frames, price, atr, candle_ts}
        prices: dict[str, float] = {}

        # Pass 1: gather data for every symbol (no trading yet).
        for sym in self.symbols:
            try:
                frames = self.data.get_frames(sym)
            except Exception as exc:
                logger.warning("{} data fetch failed: {}", sym, exc)
                continue
            last = frames[self.primary_tf].iloc[-1]
            price = float(last["close"])
            atr = float(last["atr"]) if "atr" in last and last["atr"] == last["atr"] else 0.0
            snap[sym] = {"frames": frames, "price": price, "atr": atr, "ts": last["timestamp"]}
            prices[_base_of(sym)] = price

        # Market regime: is BTC in an uptrend? (gates entries + forces risk-off exits)
        self._update_regime(snap)

        # Pass 2: manage open positions (now that regime is known).
        for sym, s in snap.items():
            if self.risk.open_position(sym) is not None:
                self._manage(sym, s["price"], s["atr"], balances)

        equity = self.risk.current_equity(balances, prices)
        avail_quote = self.risk.available_quote(balances)
        open_value = self.risk.open_value(prices)
        n_open = len(self.risk.open_positions())

        # Daily summary once per UTC day (use the primary symbol's candle clock).
        any_ts = next((s["ts"] for s in snap.values()), None)
        if any_ts is not None:
            self._maybe_summary(any_ts, equity)

        # Entry phase. Two allocation modes:
        if self.alloc_mode == "momentum_rotation":
            self._rotate(snap, equity, avail_quote, open_value, n_open)
        else:
            # first_come: each flat symbol with a freshly closed candle is sized alone.
            for sym, s in snap.items():
                if self.risk.open_position(sym) is not None:
                    continue
                if s["ts"] == self._last_candle_ts.get(sym):
                    continue
                self._last_candle_ts[sym] = s["ts"]
                if s["atr"] <= 0:
                    continue
                opened = self._try_enter(sym, s, equity, avail_quote, open_value, n_open)
                if opened:
                    n_open += 1
                    open_value += opened
                    avail_quote -= opened

    # ------------------------------------------------------------------ #
    def _update_regime(self, snap: dict[str, dict]) -> None:
        if not self.regime_enabled:
            self._btc_risk_on = True
            return
        btc_sym = f"BTC/{self._quote}"
        try:
            bf = (snap[btc_sym]["frames"][self.primary_tf] if btc_sym in snap
                  else self.data.get_frames(btc_sym)[self.primary_tf])
            ma = bf["close"].rolling(self.regime_ma).mean().iloc[-1]
            on = True if ma != ma else bool(bf.iloc[-1]["close"] > ma)
        except Exception as exc:
            logger.warning("BTC regime check failed ({}); assuming risk-on.", exc)
            on = True
        if on != self._btc_risk_on:
            state = "RISK-ON (BTC uptrend)" if on else "RISK-OFF (BTC below MA)"
            logger.warning("Market regime -> {}", state)
            self.notifier.error(f"Market regime: {state}")
        self._btc_risk_on = on

    def _try_enter(self, sym, s, equity, avail_quote, open_value, n_open) -> float:
        if self.regime_enabled and not self._btc_risk_on:
            return 0.0  # market risk-off: no new entries
        ep = self.overrides.get(_base_of(sym), {}).get("entry_period")
        if self.strategy_mode == "donchian":
            decision = self.strategy.decide(s["frames"], entry_period=ep)
        else:
            decision = self.strategy.decide(s["frames"])
        self.risk.log_decision(sym, decision.action, decision.conviction,
                               decision.consulted_claude, decision.reasoning)
        if decision.action != "BUY":
            return 0.0

        allowed, why = self.risk.can_open_trade(sym, equity, n_open)
        if not allowed:
            logger.info("{} BUY suppressed: {}", sym, why)
            if "circuit breaker" in why and not self._cb_notified:
                self.notifier.error(f"Circuit breaker active - trading paused. ({why})")
                self._cb_notified = True
            return 0.0

        atr_pct = (s["atr"] / s["price"]) if s["price"] else None
        sizing = self.risk.size_for_asset(equity, avail_quote, open_value, atr_pct=atr_pct)
        if not sizing["viable"]:
            logger.info("{} BUY skipped: size ${:.2f} below min / exposure cap.", sym, sizing["spend_usd"])
            return 0.0

        price = s["price"]
        fill = self.executor.market_buy(sym, sizing["spend_usd"], price)
        if fill is None:
            return 0.0
        stop0 = self.risk.chandelier_stop(fill["price"], s["atr"])
        stop_order_id = None
        if self.use_exchange_stop:
            stop_order_id = self.executor.place_stop_limit_sell(
                sym, fill["qty"], stop0, self.risk.stop_limit_price(stop0))
        self.risk.record_open(sym, fill, stop0, 0.0, stop_order_id, decision.reasoning,
                              peak_price=fill["price"])
        self._cb_notified = False
        self.notifier.entry(fill["price"], fill["qty"], fill["cost"], stop0, 0.0,
                            f"{sym}: {decision.reasoning}")
        return fill["cost"]

    # ------------------------------------------------------------------ #
    def _rotate(self, snap: dict[str, dict], equity: float, avail_quote: float,
                open_value: float, n_open: int) -> None:
        """Momentum-rotation allocation: on the rotation clock, hold the K
        strongest active coins. RISK exits already happened in _manage; here we
        only rotate (exit drop-outs, enter new strongest). Whole-position rotation
        - existing winners are left to run rather than re-weighted each period."""
        any_ts = next((s["ts"] for s in snap.values()), None)
        if any_ts is None:
            return
        today = (any_ts.tz_convert(timezone.utc) if any_ts.tzinfo else any_ts).date().isoformat()
        if not self.rotation.is_due(self.risk.state_get("last_rotation_day"), today):
            return

        # Market risk-off: take no new exposure (open positions are flattened in _manage).
        if self.regime_enabled and not self._btc_risk_on:
            self.risk.state_set("last_rotation_day", today)
            return

        # Candidates = coins in an active Donchian trend with momentum defined.
        cands: dict[str, float] = {}
        for sym, s in snap.items():
            ep = self.overrides.get(_base_of(sym), {}).get("entry_period")
            if self.strategy.active_state(s["frames"], entry_period=ep):
                mom = self.rotation.momentum(s["frames"])
                if mom is not None:
                    cands[sym] = mom
        held = [p["symbol"] for p in self.risk.open_positions()]
        plan = self.rotation.plan(cands, held)
        if plan["exit"] or plan["enter"]:
            logger.info("Rotation: hold {} | exit {} | enter {}",
                        sorted(plan["target"]), plan["exit"], plan["enter"])

        # 1) Rotation exits: coins no longer in the target set.
        for sym in plan["exit"]:
            pos = self.risk.open_position(sym)
            if pos is not None and sym in snap:
                self._exit(sym, pos, snap[sym]["price"], "rotation: out of top-K")

        # 2) Rotation entries: strongest target coins we don't yet hold. Refresh
        #    equity/cash after the exits (paper updates instantly; a broker may lag
        #    a cycle - acceptable in paper, which is the default for this mode).
        balances = self.data.fetch_balances()
        prices = {_base_of(sym): s["price"] for sym, s in snap.items()}
        equity = self.risk.current_equity(balances, prices)
        avail_quote = self.risk.available_quote(balances)
        open_value = self.risk.open_value(prices)
        n_open = len(self.risk.open_positions())
        for sym in plan["enter"]:
            if sym not in snap:
                continue
            opened = self._enter_rotation(sym, snap[sym], equity, avail_quote, open_value, n_open)
            if opened:
                n_open += 1
                open_value += opened
                avail_quote -= opened

        self.risk.state_set("last_rotation_day", today)

    def _enter_rotation(self, sym, s, equity, avail_quote, open_value, n_open) -> float:
        allowed, why = self.risk.can_open_trade(sym, equity, n_open)
        if not allowed:
            logger.info("{} rotation entry suppressed: {}", sym, why)
            if "circuit breaker" in why and not self._cb_notified:
                self.notifier.error(f"Circuit breaker active - trading paused. ({why})")
                self._cb_notified = True
            return 0.0
        if s["atr"] <= 0:
            return 0.0
        sizing = self.risk.size_rotation(equity, avail_quote, open_value, self.rotation.top_k)
        if not sizing["viable"]:
            logger.info("{} rotation entry skipped: size ${:.2f} below min / exposure cap.",
                        sym, sizing["spend_usd"])
            return 0.0
        price = s["price"]
        fill = self.executor.market_buy(sym, sizing["spend_usd"], price)
        if fill is None:
            return 0.0
        stop0 = self.risk.chandelier_stop(fill["price"], s["atr"])
        stop_order_id = None
        if self.use_exchange_stop:
            stop_order_id = self.executor.place_stop_limit_sell(
                sym, fill["qty"], stop0, self.risk.stop_limit_price(stop0))
        reason = f"momentum rotation: top-{self.rotation.top_k} entry"
        self.risk.record_open(sym, fill, stop0, 0.0, stop_order_id, reason, peak_price=fill["price"])
        self.risk.log_decision(sym, "BUY", 1, False, reason)
        self._cb_notified = False
        self.notifier.entry(fill["price"], fill["qty"], fill["cost"], stop0, 0.0, f"{sym}: {reason}")
        return fill["cost"]

    # ------------------------------------------------------------------ #
    def _manage(self, sym: str, price: float, atr: float, balances: dict[str, float]) -> None:
        pos = self.risk.open_position(sym)
        if pos is None:
            return
        base = _base_of(sym)
        if self.cfg["runtime"]["uses_broker"]:
            dust = self.cfg["risk"]["min_notional_usd"] / price if price else 0
            if balances.get(base, 0.0) < dust and pos["qty"] > dust:
                self.risk.record_close(pos, pos["current_stop"] or price, 0.0, "exchange stop filled")
                self.notifier.exit(pos["current_stop"] or price, 0.0, f"{sym}: exchange stop filled")
                return

        # Market risk-off: BTC below its regime MA -> exit everything.
        if self.regime_enabled and not self._btc_risk_on:
            self._exit(sym, pos, price, "BTC risk-off")
            return

        prev_stop = pos["current_stop"] or pos["stop_price"]
        peak = max(pos["peak_price"] or pos["entry_price"], price)
        new_stop = self.risk.chandelier_stop(peak, atr) if atr > 0 else prev_stop
        current_stop = max(prev_stop, new_stop)
        sid = pos["stop_order_id"]
        if (self.cfg["runtime"]["place_orders"] and self.use_exchange_stop
                and current_stop > prev_stop * 1.001):
            self.executor.cancel(sym, pos["stop_order_id"])
            sid = self.executor.place_stop_limit_sell(
                sym, pos["qty"], current_stop, self.risk.stop_limit_price(current_stop))
            logger.info("{} chandelier raised {:.4f} -> {:.4f}", sym, prev_stop, current_stop)
        self.risk.update_trail(pos["id"], peak, current_stop, sid)

        if price <= current_stop:
            self._exit(sym, pos, price, "chandelier trail")

    def _exit(self, sym: str, pos, price: float, reason: str) -> None:
        if self.cfg["runtime"]["place_orders"] and self.use_exchange_stop:
            self.executor.cancel(sym, pos["stop_order_id"])
        fill = self.executor.market_sell(sym, pos["qty"], price, reason)
        if fill is None:
            logger.error("{} exit sell failed - will retry next cycle.", sym)
            self.notifier.error(f"{sym} exit sell FAILED - position still open.")
            return
        pnl = self.risk.record_close(pos, fill["price"], fill.get("fee", 0.0), reason)
        self.notifier.exit(fill["price"], pnl, f"{sym}: {reason}")

    # ------------------------------------------------------------------ #
    def _maybe_summary(self, candle_ts, equity: float) -> None:
        ts = candle_ts.tz_convert(timezone.utc) if candle_ts.tzinfo else candle_ts
        day = ts.date().isoformat()
        if day == self._last_summary_day or ts.hour < self.cfg["claude"]["daily_summary_hour_utc"]:
            return
        self._last_summary_day = day
        stats = self.risk.daily_stats(equity)
        note = self.claude.daily_summary(stats)
        logger.info("DAILY SUMMARY ({})\n{}\n{}", day, note, stats)
        self.notifier.summary(stats, note)


def main() -> None:
    TradingBot().run()


if __name__ == "__main__":
    main()
