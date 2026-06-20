"""
Autonomous heartbeat loop - Binance.US spot, long-only, high-conviction.

Lifecycle:   flat (USDT)  ->  BUY  ->  manage (trailing stop / target)  ->  SELL  ->  flat

Each cycle (~60s):
  * refresh multi-timeframe candles + balances
  * if holding BTC: manage the position (ratchet trailing stop, check exits)
  * if flat and a new 5m candle closed: ask the strategy for a high-conviction BUY
  * once/day: log a plain-English summary

Safety:
  * PAPER_TRADING=true (default) simulates everything - no real orders.
  * Real orders require BOTH PAPER_TRADING=false AND LIVE_TRADING_ENABLED=true.
  * Live buys also place an exchange-side stop-limit so a crash/outage can't leave
    you unprotected.

Ctrl+C (local) or SIGTERM (Railway) shuts down cleanly.
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
from src.risk_manager import RiskManager
from src.strategy import Strategy


class TradingBot:
    def __init__(self) -> None:
        self.cfg = load_config()
        self._configure_logging()

        rt = self.cfg["runtime"]
        mode = "LIVE (REAL ORDERS)" if rt["really_live"] else "PAPER (simulated)"
        logger.info("=" * 66)
        logger.info("Binance.US Spot Long-Only Bot | mode = {}", mode)
        if not rt["really_live"] and not rt["paper_trading"]:
            logger.warning("PAPER_TRADING=false but LIVE_TRADING_ENABLED=false -> still PAPER. "
                           "Set BOTH switches to go live.")
        logger.info("=" * 66)

        self.exchange = build_exchange(self.cfg)
        self.data = DataPipeline(self.cfg, self.exchange)
        self.claude = ClaudeOrchestrator(self.cfg)
        self.strategy = Strategy(self.cfg, claude_orchestrator=self.claude)
        self.risk = RiskManager(self.cfg)
        self.executor = SpotExecutor(self.cfg, self.exchange)

        self.primary_tf = self.cfg["market"]["primary_timeframe"]
        self.poll_seconds = self.cfg["market"]["poll_seconds"]
        self.use_exchange_stop = self.cfg["exits"]["use_exchange_stop"]
        self.running = True
        self._last_candle_ts: Optional[Any] = None
        self._last_summary_day: Optional[str] = None

        signal.signal(signal.SIGINT, self._sig)
        signal.signal(signal.SIGTERM, self._sig)

    def _configure_logging(self) -> None:
        logger.remove()
        logger.add(sys.stdout, level=self.cfg["logging"]["level"],
                   format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
                          "<level>{level: <7}</level> | <level>{message}</level>")

    def _sig(self, signum, _frame) -> None:
        logger.warning("Signal {} received - shutting down gracefully...", signum)
        self.running = False

    # ------------------------------------------------------------------ #
    def run(self) -> None:
        try:
            frames = self.data.get_frames()
            price = self.data.last_price(frames)
            balances = self.data.fetch_balances()
            self.risk.reconcile(balances, price)
        except Exception as exc:
            logger.error("Startup data/reconcile failed: {}. Exiting.", exc)
            return

        logger.info("Ready. Need ALL gates + >= {} of 8 triggers to buy. Risk/trade {:.2%}, "
                    "default capital ${:.0f}.",
                    self.cfg["strategy"]["triggers"]["min_required"],
                    self.cfg["risk"]["risk_per_trade_pct"], self.cfg["risk"]["default_capital_usd"])

        while self.running:
            try:
                self._cycle()
            except Exception as exc:
                logger.exception("Cycle error (continuing): {}", exc)
            self._sleep(self.poll_seconds)

        logger.info("Loop stopped.")

    def _sleep(self, seconds: int) -> None:
        for _ in range(seconds):
            if not self.running:
                return
            time.sleep(1)

    # ------------------------------------------------------------------ #
    def _cycle(self) -> None:
        frames = self.data.get_frames()
        if self.primary_tf not in frames or frames[self.primary_tf].empty:
            return
        last = frames[self.primary_tf].iloc[-1]
        price = float(last["close"])
        atr = float(last["atr"]) if "atr" in last and last["atr"] == last["atr"] else 0.0
        balances = self.data.fetch_balances()
        equity = self.risk.current_equity(balances, price)

        # 1) Manage an open position every tick (tight exit handling).
        if self.risk.open_position() is not None:
            self._manage(price, atr, balances)

        # 2) New entries only once per freshly closed primary candle.
        candle_ts = last["timestamp"]
        if candle_ts != self._last_candle_ts:
            self._last_candle_ts = candle_ts
            self._maybe_summary(candle_ts, equity)
            if self.risk.open_position() is None:
                self._maybe_enter(frames, price, atr, equity, balances)

    # ------------------------------------------------------------------ #
    def _maybe_enter(self, frames, price: float, atr: float,
                     equity: float, balances: dict[str, float]) -> None:
        decision = self.strategy.decide(frames)
        logger.info("Decision: {} | conviction {}/{}{} | {}",
                    decision.action, decision.conviction, decision.triggers_required,
                    " | Claude" if decision.consulted_claude else "", decision.reasoning)
        self.risk.log_decision(decision.action, decision.conviction,
                               decision.consulted_claude, decision.reasoning)

        if decision.action != "BUY":
            return
        if atr <= 0:
            logger.info("Skipping buy: ATR not ready.")
            return

        allowed, why = self.risk.can_open_trade(equity)
        if not allowed:
            logger.info("BUY suppressed by safety rails: {}", why)
            return

        available_usdt = balances.get("USDT", 0.0) if self.cfg["runtime"]["really_live"] else equity
        sizing = self.risk.size_buy(equity, available_usdt, price, atr)
        if not sizing["viable"]:
            logger.info("BUY skipped: size ${:.2f} below minimum.", sizing["spend_usd"])
            return

        fill = self.executor.market_buy(sizing["spend_usd"], price)
        if fill is None:
            return

        stop_order_id = None
        if self.use_exchange_stop:
            stop_order_id = self.executor.place_stop_limit_sell(
                fill["qty"], sizing["stop_price"], self.risk.stop_limit_price(sizing["stop_price"]))

        reason = (f"conviction {decision.conviction}/{decision.triggers_required}, "
                  f"risk {sizing['risk_fraction']:.2%}")
        self.risk.record_open(fill, sizing["stop_price"], sizing["take_price"],
                              stop_order_id, reason)

    # ------------------------------------------------------------------ #
    def _manage(self, price: float, atr: float, balances: dict[str, float]) -> None:
        pos = self.risk.open_position()
        if pos is None:
            return

        # Live: if our BTC vanished, the exchange stop filled while we were busy/offline.
        if self.cfg["runtime"]["really_live"]:
            dust = self.cfg["risk"]["min_notional_usd"] / price if price else 0
            if balances.get("BTC", 0.0) < dust and pos["qty"] > dust:
                self.risk.record_close(pos, pos["current_stop"] or price, 0.0, "exchange stop filled")
                return

        current_stop = pos["current_stop"] or pos["stop_price"]

        # Ratchet the trailing stop upward (never down).
        if atr > 0:
            new_trail = self.risk.trailing_stop(price, atr)
            if new_trail > current_stop * 1.001:  # meaningful move only, avoids churn
                if self.cfg["runtime"]["really_live"] and self.use_exchange_stop:
                    self.executor.cancel(pos["stop_order_id"])
                    new_id = self.executor.place_stop_limit_sell(
                        pos["qty"], new_trail, self.risk.stop_limit_price(new_trail))
                    self.risk.update_stop(pos["id"], new_trail, new_id)
                else:
                    self.risk.update_stop(pos["id"], new_trail, pos["stop_order_id"])
                logger.info("Trailing stop raised {:.2f} -> {:.2f}", current_stop, new_trail)
                current_stop = new_trail

        # Exit checks (priority: stop, then target).
        exit_reason = None
        if price <= current_stop:
            exit_reason = "trailing/stop hit"
        elif price >= pos["take_price"]:
            exit_reason = "take-profit"

        if exit_reason:
            self._exit(pos, price, exit_reason)

    def _exit(self, pos, price: float, reason: str) -> None:
        # Cancel any resting exchange stop before we market-sell (avoid a double sell).
        if self.cfg["runtime"]["really_live"] and self.use_exchange_stop:
            self.executor.cancel(pos["stop_order_id"])
        fill = self.executor.market_sell(pos["qty"], price, reason)
        if fill is None:
            logger.error("Exit sell failed - position left open, will retry next cycle.")
            return
        self.risk.record_close(pos, fill["price"], fill.get("fee", 0.0), reason)

    # ------------------------------------------------------------------ #
    def _maybe_summary(self, candle_ts, equity: float) -> None:
        ts = candle_ts.tz_convert(timezone.utc) if candle_ts.tzinfo else candle_ts
        day = ts.date().isoformat()
        if day == self._last_summary_day or ts.hour < self.cfg["claude"]["daily_summary_hour_utc"]:
            return
        self._last_summary_day = day
        stats = self.risk.daily_stats(equity)
        logger.info("DAILY SUMMARY ({})\n{}\n{}", day, self.claude.daily_summary(stats), stats)


def main() -> None:
    TradingBot().run()


if __name__ == "__main__":
    main()
