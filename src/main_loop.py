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
        balances = self.data.fetch_balances()
        snap: dict[str, dict] = {}   # symbol -> {frames, price, atr, candle_ts}
        prices: dict[str, float] = {}

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
            # Manage an open position for this symbol every tick.
            if self.risk.open_position(sym) is not None:
                self._manage(sym, price, atr, balances)

        equity = self.risk.current_equity(balances, prices)
        avail_quote = self.risk.available_quote(balances)
        open_value = self.risk.open_value(prices)
        n_open = len(self.risk.open_positions())

        # Daily summary once per UTC day (use the primary symbol's candle clock).
        any_ts = next((s["ts"] for s in snap.values()), None)
        if any_ts is not None:
            self._maybe_summary(any_ts, equity)

        # Entry phase: only flat symbols with a freshly closed candle.
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
    def _try_enter(self, sym, s, equity, avail_quote, open_value, n_open) -> float:
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

        sizing = self.risk.size_for_asset(equity, avail_quote, open_value)
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
