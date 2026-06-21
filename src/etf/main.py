"""
ETF momentum heartbeat loop (sibling of src/main_loop.py).

Low-frequency: on the rebalance clock it holds the strongest-K eligible ETFs
(active Donchian trend, ranked by momentum), rotating drop-outs for new leaders.
Long-only, equal-weight. SIM (paper ledger, live prices) by default; live needs
the two-key tripwire + ETF_ENABLED + etf.execution.mode=live.

Exits are rotation-driven: a symbol that loses its trend falls out of the
candidate set and is sold at the next rebalance (no intraday stop in v1).
"""
from __future__ import annotations

import signal
import sys
import time
from datetime import timezone
from typing import Any

import pandas as pd
from loguru import logger

from src.notifications import Notifier
from .brokers import build_broker
from .config_etf import load_etf_config
from .data import EtfData
from .executor import EtfExecutor
from .risk import EtfRiskManager
from .selector import EtfMomentumSelector

_MODE_LABEL = {"live": "LIVE (REAL MONEY)", "paper-broker": "PAPER-BROKER (Alpaca paper, no money)",
               "sim": "SIM (paper ledger, live data)"}


class EtfBot:
    def __init__(self) -> None:
        self.cfg = load_etf_config()
        self._configure_logging()
        self.rt = self.cfg["etf_runtime"]
        self.tf = self.cfg["etf"]["primary_timeframe"]
        self.poll_seconds = int(self.cfg["etf"]["poll_seconds"])
        self.universe = list(self.cfg["etf"]["universe"])

        self.broker = build_broker(self.cfg)
        self.data = EtfData(self.cfg, self.broker)
        self.selector = EtfMomentumSelector(self.cfg)
        self.risk = EtfRiskManager(self.cfg)
        self.executor = EtfExecutor(self.cfg, self.broker)
        self.notifier = Notifier(self.cfg)

        self.running = True
        self._mode = _MODE_LABEL.get(self.rt["mode"], self.rt["mode"])
        logger.info("=" * 70)
        logger.info("ETF Momentum Bot | venue={} | mode={} | top-{} of {} ETFs, rotate every {}d",
                    self.rt["venue"], self._mode, self.selector.top_k, len(self.universe),
                    self.cfg["etf"]["selection"]["rebalance_days"])
        logger.info("=" * 70)
        signal.signal(signal.SIGINT, self._sig)
        signal.signal(signal.SIGTERM, self._sig)

    def _configure_logging(self) -> None:
        logger.remove()
        logger.add(sys.stdout, level=self.cfg["logging"]["level"],
                   format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
                          "<level>{level: <7}</level> | <level>{message}</level>")

    def _sig(self, signum, _frame) -> None:
        logger.warning("Signal {} - shutting down (positions persist).", signum)
        self.running = False

    # ------------------------------------------------------------------ #
    def run(self) -> None:
        self.symbols = self.data.available_symbols(self.universe)
        if not self.symbols:
            logger.error("No tradable ETF symbols on {}. Exiting.", self.rt["venue"])
            return
        logger.info("Universe ({}): {}", len(self.symbols), ", ".join(self.symbols))
        self.notifier.message(f"📈ETF momentum bot started\nMode: {self._mode}\n"
                              f"Universe: {', '.join(self.symbols)}")
        while self.running:
            try:
                self._cycle()
            except Exception as exc:
                logger.exception("ETF cycle error (continuing): {}", exc)
                self.notifier.error(f"📈ETF cycle error: {exc}")
            self._sleep(self.poll_seconds)
        logger.info("ETF loop stopped.")

    def _sleep(self, seconds: int) -> None:
        for _ in range(seconds):
            if not self.running:
                return
            time.sleep(1)

    def _balances(self) -> dict[str, float]:
        if not self.rt["place_orders"]:
            return {}
        bal = {self.rt["quote"]: self.broker.cash()}
        bal.update(self.broker.positions())
        return bal

    # ------------------------------------------------------------------ #
    def _cycle(self) -> None:
        frames_by_symbol: dict[str, dict[str, pd.DataFrame]] = {}
        prices: dict[str, float] = {}
        latest_ts = None
        for sym in self.symbols:
            try:
                frames = self.data.frames(sym)
            except Exception as exc:
                logger.warning("{} data fetch failed: {}", sym, exc)
                continue
            frames_by_symbol[sym] = frames
            prices[sym] = self.data.last_price(frames)
            ts = frames[self.tf].iloc[-1]["timestamp"]
            latest_ts = ts if latest_ts is None else max(latest_ts, ts)
        if not frames_by_symbol or latest_ts is None:
            return

        # Live: never place orders when the equities market is closed.
        if self.rt["place_orders"] and not self.broker.is_market_open():
            logger.info("ETF market closed - holding; no rebalance this poll.")
            return

        today = pd.Timestamp(latest_ts).tz_convert(timezone.utc).date().isoformat()
        balances = self._balances()
        equity = self.risk.current_equity(balances, prices)

        if not self.selector.is_due(self.risk.state_get("etf_last_rebalance"), today):
            logger.info("ETF hold (not a rebalance day). Equity ${:.2f}, held {}.",
                        equity, self.risk.held_symbols())
            return

        held = self.risk.held_symbols()
        plan = self.selector.plan(frames_by_symbol, held)
        if plan["exit"] or plan["enter"]:
            logger.info("ETF rotation: hold {} | exit {} | enter {}",
                        sorted(plan["target"]), plan["exit"], plan["enter"])

        # 1) Exits: sell symbols that fell out of the target set.
        for sym in plan["exit"]:
            pos = self.risk.open_position(sym)
            if pos is None or sym not in prices:
                continue
            fill = self.executor.market_sell(sym, pos["qty"], prices[sym], "rotation: out of top-K")
            if fill is None:
                logger.error("{} exit sell failed - will retry next rebalance.", sym)
                continue
            pnl = self.risk.record_close(pos, fill, "rotation: out of top-K")
            self.notifier.message(f"{'✅' if pnl >= 0 else '🔻'} 📈ETF SELL {sym}\nPnL: ${pnl:,.2f}")

        # 2) Entries: buy new leaders, equal-weight, after refreshing cash/exposure.
        balances = self._balances()
        equity = self.risk.current_equity(balances, prices)
        cash = self.risk.available_cash(balances)
        exposure = self.risk.holdings_value(prices)
        for sym in plan["enter"]:
            if sym not in prices:
                continue
            sizing = self.risk.size(equity, cash, exposure)
            if not sizing["viable"]:
                logger.info("{} entry skipped: size ${:.2f} below min / exposure cap.",
                            sym, sizing["spend_usd"])
                continue
            fill = self.executor.market_buy(sym, sizing["spend_usd"], prices[sym])
            if fill is None:
                continue
            self.risk.record_open(sym, fill, "momentum rotation: top-K entry")
            exposure += fill["cost"]
            cash -= fill["cost"] + fill.get("fee", 0.0)
            self.notifier.message(f"🟢 📈ETF BUY {sym}\nSize: ${fill['cost']:,.2f}")

        self.risk.state_set("etf_last_rebalance", today)
        stats = self.risk.daily_stats(self.risk.current_equity(self._balances(), prices))
        logger.info("ETF REBALANCED {} | {}", today, stats)


def main() -> None:
    EtfBot().run()


if __name__ == "__main__":
    main()
