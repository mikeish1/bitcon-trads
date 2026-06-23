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
from .selector import build_selector

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
        self.selector = build_selector(self.cfg)
        self.risk = EtfRiskManager(self.cfg)
        self.executor = EtfExecutor(self.cfg, self.broker)
        self.notifier = Notifier(self.cfg)
        self.sel_mode = str(self.cfg["etf"]["selection"].get("mode", "rotation"))
        self.pdt_guard = bool(self.cfg["etf"].get("pdt_guard", True))

        self.running = True
        self._mode = _MODE_LABEL.get(self.rt["mode"], self.rt["mode"])
        logger.info("=" * 70)
        logger.info("ETF Bot | venue={} | mode={} | selector={} | {} of {} ETFs, rebalance every {}d",
                    self.rt["venue"], self._mode, self.sel_mode, self.selector.top_k,
                    len(self.universe), self.selector.rebalance_days)
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

        # Startup reconcile (live): close DB positions the broker no longer holds
        # (external/manual close, full liquidation, delisting) so the ledger can't
        # drift from the account. No-op in sim.
        if self.rt["place_orders"]:
            try:
                for note in self.risk.reconcile(self.broker.positions(), {}):
                    self.notifier.message(f"📈ETF reconcile: {note}")
            except Exception as exc:
                logger.warning("ETF startup reconcile skipped: {}", exc)

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

    def _note_regime(self, regime: str | None) -> None:
        """Alert + log on a risk-on/off flip (dual-momentum mode emits a regime)."""
        if not regime:
            return
        prev = self.risk.state_get("etf_regime")
        if prev != regime:
            self.risk.state_set("etf_regime", regime)
            logger.info("ETF regime flip: {} -> {}", prev or "?", regime)
            self.notifier.message(f"📈ETF regime: {prev or '?'} -> {regime}")

    def _rebalance_static(self, prices: dict[str, float], today: str) -> None:
        """Rebalance the fixed-weight sleeve toward target weights: trim/add only the
        symbols whose weight has drifted beyond the band (low turnover). Sells run
        before buys so cash is freed first."""
        weights = self.selector.target_weights()
        band = self.selector.drift_band
        equity = self.risk.current_equity(self._balances(), prices)
        deployable = equity * self.risk.max_exposure
        targets = {s: weights.get(s, 0.0) * deployable
                   for s in set(weights) | set(self.risk.held_symbols())}
        traded = False

        for sym in sorted(targets):                       # 1) trims (free cash first)
            if sym not in prices:
                continue
            pos = self.risk.open_position(sym)
            cur = pos["qty"] * prices[sym] if pos else 0.0
            if pos and equity > 0 and abs(cur - targets[sym]) / equity < band:
                continue
            over = cur - targets[sym]
            if pos and over > self.risk.min_notional:
                qty = min(pos["qty"], over / prices[sym])
                fill = self.executor.market_sell(sym, qty, prices[sym], "static rebalance trim")
                if fill:
                    pnl = self.risk.trim_position(pos, fill["qty"], fill["price"],
                                                  fill.get("fee", 0.0))
                    traded = True
                    self.notifier.message(f"🔻 📈ETF TRIM {sym}\nrealized ${pnl:,.2f}")

        cash = self.risk.available_cash(self._balances())
        for sym in sorted(weights):                       # 2) adds toward target
            if sym not in prices:
                continue
            pos = self.risk.open_position(sym)
            cur = pos["qty"] * prices[sym] if pos else 0.0
            if pos and equity > 0 and abs(cur - targets[sym]) / equity < band:
                continue
            spend = min(targets[sym] - cur, cash)
            if spend > self.risk.min_notional:
                fill = self.executor.market_buy(sym, spend, prices[sym])
                if fill:
                    self.risk.add_to_position(sym, fill, "static rebalance add")
                    cash -= fill["cost"] + fill.get("fee", 0.0)
                    traded = True
                    self.notifier.message(f"🟢 📈ETF ADD {sym}\nSize: ${fill['cost']:,.2f}")

        self.risk.state_set("etf_last_rebalance", today)
        stats = self.risk.daily_stats(self.risk.current_equity(self._balances(), prices))
        logger.info("ETF static {} {} | {}", "rebalanced" if traded else "in-band (no trades)",
                    today, stats)

    # ------------------------------------------------------------------ #
    def _cycle(self) -> None:
        frames_by_symbol: dict[str, dict[str, pd.DataFrame]] = {}
        prices: dict[str, float] = {}
        latest_ts = None
        market_open = self.broker.is_market_open()   # once per cycle (one clock call)
        for sym in self.symbols:
            try:
                frames = self.data.frames(sym)
            except Exception as exc:
                logger.warning("{} data fetch failed: {}", sym, exc)
                continue
            # LIVE price (current session bar) for marking / sizing / orders; SIGNALS
            # decide on the last CONFIRMED-CLOSED bar so live matches the backtest.
            prices[sym] = self.data.last_price(frames)
            sig = self.data.closed_view(frames, market_open)
            frames_by_symbol[sym] = sig
            ts = sig[self.tf].iloc[-1]["timestamp"]
            latest_ts = ts if latest_ts is None else max(latest_ts, ts)
        if not frames_by_symbol or latest_ts is None:
            return

        # Live: never place orders when the equities market is closed.
        if self.rt["place_orders"] and not market_open:
            logger.info("ETF market closed - holding; no rebalance this poll.")
            return

        today = pd.Timestamp(latest_ts).tz_convert(timezone.utc).date().isoformat()
        balances = self._balances()
        equity = self.risk.current_equity(balances, prices)

        if not self.selector.is_due(self.risk.state_get("etf_last_rebalance"), today):
            logger.info("ETF hold (not a rebalance day). Equity ${:.2f}, held {}.",
                        equity, self.risk.held_symbols())
            return

        # Static allocation rebalances to target WEIGHTS (partial trims/adds), not the
        # momentum whole-position enter/exit - a separate path.
        if self.sel_mode == "static_allocation":
            self._rebalance_static(prices, today)
            return

        held = self.risk.held_symbols()
        plan = self.selector.plan(frames_by_symbol, held)
        self._note_regime(plan.get("regime"))
        if plan["exit"] or plan["enter"]:
            logger.info("ETF {}: hold {} | exit {} | enter {}",
                        self.sel_mode, sorted(plan["target"]), plan["exit"], plan["enter"])

        # 1) Exits: sell symbols that fell out of the target set.
        for sym in plan["exit"]:
            pos = self.risk.open_position(sym)
            if pos is None or sym not in prices:
                continue
            # PDT guard: never round-trip a symbol the same calendar day it opened
            # (keeps a <$25k margin account clear of the day-trade rule).
            if self.pdt_guard and self.risk.opened_today(pos, today):
                logger.info("{} exit deferred: opened today (PDT same-day guard).", sym)
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
