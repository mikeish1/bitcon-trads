"""
Autonomous heartbeat loop.

Ties everything together:
  data_pipeline  -> candles + indicators
  ensemble_engine -> 31-path consensus (with sparing Claude tie-breaks)
  risk_manager    -> Kelly sizing + safety rails + persistent state
  claude_orchestrator -> borderline validation + daily summary

Runs as a single process (Railway-friendly). Every ~60s it manages any open
position (stop/target); on each new closed 5-minute candle it asks the ensemble
whether to open a trade.

PAPER_TRADING=true  -> simulates fills with small slippage (DEFAULT, safe).
PAPER_TRADING=false -> places real Binance orders (only after you trust it).

Stop with Ctrl+C locally; Railway sends SIGTERM on redeploy - both shut down
cleanly without leaving work half-done.
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
from src.ensemble_engine import EnsembleDecision, EnsembleEngine
from src.risk_manager import RiskManager

TICK_SECONDS = 60  # manage positions every minute; decide on each new 5m candle


class TradingBot:
    def __init__(self) -> None:
        self.cfg = load_config()
        self._configure_logging()

        self.paper = self.cfg["runtime"]["paper_trading"]
        mode = "PAPER (simulated)" if self.paper else "LIVE (real orders)"
        logger.info("=" * 64)
        logger.info("Bitcoin Ensemble Trading System starting | mode = {}", mode)
        logger.info("=" * 64)

        self.exchange = build_exchange(self.cfg)
        self.data = DataPipeline(self.cfg, self.exchange)
        self.claude = ClaudeOrchestrator(self.cfg)
        self.ensemble = EnsembleEngine(self.cfg, claude_orchestrator=self.claude)
        self.risk = RiskManager(self.cfg)

        self.symbol = self.cfg["market"]["symbol"]
        self.running = True
        self._last_decision_ts: Optional[Any] = None
        self._last_summary_day: Optional[str] = None

        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

    def _configure_logging(self) -> None:
        logger.remove()
        logger.add(
            sys.stdout,
            level=self.cfg["logging"]["level"],
            format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
                   "<level>{level: <7}</level> | <level>{message}</level>",
        )

    def _handle_signal(self, signum, _frame) -> None:
        logger.warning("Received signal {} - shutting down gracefully...", signum)
        self.running = False

    # ------------------------------------------------------------------ #
    # Lifecycle                                                          #
    # ------------------------------------------------------------------ #
    def run(self) -> None:
        try:
            self.data.backfill_history()
        except Exception as exc:
            logger.error("Backfill failed: {}. Exiting.", exc)
            return

        self.data.start_websocket()  # falls back to polling automatically
        logger.info(
            "Ready. Consensus rule: >= {} of {} paths to trade; "
            "Kelly fraction {}; risk/trade cap {:.2%}.",
            self.cfg["ensemble"]["trade_threshold"],
            self.cfg["ensemble"]["total_paths"],
            self.cfg["risk"]["kelly_fraction"],
            self.cfg["risk"]["max_risk_per_trade"],
        )

        while self.running:
            try:
                self._cycle()
            except Exception as exc:
                logger.exception("Cycle error (continuing): {}", exc)
            self._sleep(TICK_SECONDS)

        logger.info("Loop stopped. Final equity: ${:.2f}", self.risk.equity)

    def _sleep(self, seconds: int) -> None:
        """Interruptible sleep so shutdown is responsive."""
        for _ in range(seconds):
            if not self.running:
                return
            time.sleep(1)

    # ------------------------------------------------------------------ #
    # One cycle                                                          #
    # ------------------------------------------------------------------ #
    def _cycle(self) -> None:
        df = self.data.poll_latest()
        df = self.data.add_indicators(df)
        if df.empty:
            return
        last = df.iloc[-1]
        price = float(last["close"])

        # 1) Manage any open position every tick (tight stop/target handling).
        self._manage_open_position(df, price)

        # 2) Only make a NEW decision once per freshly closed candle.
        candle_ts = last["timestamp"]
        if candle_ts == self._last_decision_ts:
            return
        self._last_decision_ts = candle_ts

        # 3) Daily summary (once per UTC day, at/after the configured hour).
        self._maybe_daily_summary(candle_ts)

        # 4) If flat, evaluate the ensemble for a new entry.
        if self.risk.open_position() is None:
            self._evaluate_entry(df, price)

    # ------------------------------------------------------------------ #
    # Entry                                                              #
    # ------------------------------------------------------------------ #
    def _evaluate_entry(self, df, price: float) -> None:
        decision: EnsembleDecision = self.ensemble.decide(df)
        logger.info(
            "Decision: {} | agree {}/{} (det {}/28){} | {}",
            decision.direction, decision.agreement, decision.total_paths,
            decision.deterministic_agreement,
            " | Claude consulted" if decision.consulted_claude else "",
            decision.reasoning,
        )

        if decision.direction == "FLAT":
            self.risk.log_decision(
                decision.direction, decision.agreement, decision.consulted_claude,
                "STAY_FLAT", decision.reasoning,
            )
            return

        allowed, reason = self.risk.can_open_trade()
        if not allowed:
            logger.info("Signal {} suppressed by safety rails: {}", decision.direction, reason)
            self.risk.log_decision(
                decision.direction, decision.agreement, decision.consulted_claude,
                "BLOCKED", reason,
            )
            return

        sizing = self.risk.compute_position(price)
        if not sizing["viable"]:
            logger.info("Position too small (${:.2f}) - skipping.", sizing["notional_usd"])
            self.risk.log_decision(
                decision.direction, decision.agreement, decision.consulted_claude,
                "SKIPPED_DUST", f"notional ${sizing['notional_usd']:.2f}",
            )
            return

        self._open_trade(decision, price, sizing)

    def _open_trade(self, decision: EnsembleDecision, price: float, sizing: dict) -> None:
        side = decision.direction
        qty = sizing["qty"]
        slippage = self.cfg["execution"]["paper_slippage_pct"]

        if self.paper:
            entry = price * (1 + slippage) if side == "LONG" else price * (1 - slippage)
        else:
            entry = self._live_market_order(side, qty)
            if entry is None:
                logger.error("Live order failed - not recording a phantom trade.")
                return

        stop, take = self.risk.stop_and_target(side, entry)
        mode = "PAPER" if self.paper else "LIVE"
        reason = (
            f"{decision.agreement}/{decision.total_paths} agree "
            f"(Kelly risk {sizing['risk_fraction']:.2%}, win-rate {sizing['win_rate']:.0%})"
        )
        self.risk.record_open(side, entry, qty, sizing["notional_usd"], stop, take, mode, reason)
        self.risk.log_decision(
            decision.direction, decision.agreement, decision.consulted_claude, "OPEN", reason
        )

    # ------------------------------------------------------------------ #
    # Exit                                                               #
    # ------------------------------------------------------------------ #
    def _manage_open_position(self, df, price: float) -> None:
        pos = self.risk.open_position()
        if pos is None:
            return

        last = df.iloc[-1]
        high, low = float(last["high"]), float(last["low"])
        side = pos["side"]
        stop, take = pos["stop_price"], pos["take_price"]

        exit_price: Optional[float] = None
        exit_reason = ""

        # Stop / target using the latest candle's extremes (stop takes priority).
        if side == "LONG":
            if low <= stop:
                exit_price, exit_reason = stop, "stop-loss"
            elif high >= take:
                exit_price, exit_reason = take, "take-profit"
        else:  # SHORT
            if high >= stop:
                exit_price, exit_reason = stop, "stop-loss"
            elif low <= take:
                exit_price, exit_reason = take, "take-profit"

        # Optional: exit if the ensemble flips hard against us.
        if exit_price is None:
            decision = self.ensemble.decide(df)
            opposite = "SHORT" if side == "LONG" else "LONG"
            if decision.direction == opposite:
                exit_price, exit_reason = price, "ensemble reversal"

        if exit_price is None:
            return

        if not self.paper:
            close_side = "SHORT" if side == "LONG" else "LONG"
            filled = self._live_market_order(close_side, pos["qty"], reduce_only=True)
            if filled is not None:
                exit_price = filled

        self.risk.record_close(pos["id"], exit_price, exit_reason)

    # ------------------------------------------------------------------ #
    # Live order helper                                                  #
    # ------------------------------------------------------------------ #
    def _live_market_order(
        self, side: str, qty: float, reduce_only: bool = False
    ) -> Optional[float]:
        """Place a live market order. Returns the average fill price, or None on failure."""
        try:
            ccxt_side = "buy" if side == "LONG" else "sell"
            params: dict[str, Any] = {}
            if reduce_only:
                params["reduceOnly"] = True
            try:
                self.exchange.set_leverage(self.cfg["risk"]["leverage"], self.symbol)
            except Exception:
                pass
            order = self.exchange.create_order(self.symbol, "market", ccxt_side, qty, None, params)
            fill = order.get("average") or order.get("price")
            logger.info("LIVE order filled: {} {} @ {}", ccxt_side, qty, fill)
            return float(fill) if fill else None
        except Exception as exc:
            logger.error("Live order error: {}", exc)
            return None

    # ------------------------------------------------------------------ #
    # Daily summary                                                      #
    # ------------------------------------------------------------------ #
    def _maybe_daily_summary(self, candle_ts) -> None:
        ts = candle_ts.tz_convert(timezone.utc) if candle_ts.tzinfo else candle_ts
        day = ts.date().isoformat()
        hour = self.cfg["claude"]["daily_summary_hour_utc"]
        if day == self._last_summary_day or ts.hour < hour:
            return
        self._last_summary_day = day
        stats = self.risk.daily_stats()
        summary = self.claude.daily_summary(stats)
        logger.info("DAILY SUMMARY ({})\n{}\n{}", day, summary, stats)


def main() -> None:
    bot = TradingBot()
    bot.run()


if __name__ == "__main__":
    main()
