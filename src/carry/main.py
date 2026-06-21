"""
Funding-carry heartbeat loop (delta-neutral; sibling of src/main_loop.py).

Each poll, per asset:
  1. read a FundingQuote (smoothed funding APR, prices, basis, staleness);
  2. accrue funding on any open pair (pro-rata since the last poll);
  3. if held: UNWIND when carry has decayed (confirmed), else HOLD; check margin
     (live) and delta drift;
  4. if flat: OPEN a long-spot / short-perp pair when NET carry clears the entry
     bar and the risk gates pass.

SIM by default: real funding/prices, simulated fills, nothing sent. Real orders
need CARRY_ENABLED=true AND the two-key tripwire AND execution.mode=live.
Ctrl+C / SIGTERM drain cleanly; delta-neutral pairs persist in SQLite (not
force-closed) so a restart resumes them.
"""
from __future__ import annotations

import signal
import sys
import time
from datetime import timezone
from typing import Any

from loguru import logger

from src.notifications import Notifier
from .config_carry import build_carry_exchanges, build_carry_params, load_carry_config
from .data import CarryData
from .executor import CarryExecutionError, CarryExecutor
from .risk import CarryRiskManager
from .signal import evaluate


class CarryBot:
    def __init__(self) -> None:
        self.cfg = load_carry_config()
        self._configure_logging()
        self.params = build_carry_params(self.cfg)
        self.rt = self.cfg["carry_runtime"]
        self.assets = list(self.cfg["carry"]["assets"])
        self.poll_seconds = int(self.cfg["carry"]["poll_seconds"])
        self.leverage = float(self.cfg["carry"]["risk"]["target_leverage"])
        self.margin_alert = float(self.cfg["carry"]["risk"]["margin_alert_ratio"])
        self.heartbeat_every = 3600

        self.spot_ex, self.perp_ex = build_carry_exchanges(self.cfg)
        self.data = CarryData(self.cfg, self.spot_ex, self.perp_ex)
        self.risk = CarryRiskManager(self.cfg)
        self.executor = CarryExecutor(self.cfg, self.spot_ex, self.perp_ex)
        self.notifier = Notifier(self.cfg)

        self.running = True
        self._last_summary_day: str | None = None
        self._last_heartbeat = 0.0
        self._stale_strikes = 0

        mode = "LIVE (REAL MONEY)" if self.rt["real_money"] else "SIM (paper - live data)"
        self._mode = mode
        logger.info("=" * 70)
        logger.info("Funding-Carry Bot | spot={} perp={} | mode={} | assets={}",
                    self.rt["spot_id"], self.rt["perp_id"], mode, ", ".join(self.assets))
        logger.info("Entry >= {:.1%} net APR | unwind < {:.1%} x{} reads | sleeve ${:.0f}",
                    self.params.min_entry_apr, self.cfg["carry"]["signal"]["min_hold_apr"],
                    self.params.flip_confirm_reads, self.risk.sleeve)
        logger.info("=" * 70)

        signal.signal(signal.SIGINT, self._sig)
        signal.signal(signal.SIGTERM, self._sig)

    def _configure_logging(self) -> None:
        logger.remove()
        logger.add(sys.stdout, level=self.cfg["logging"]["level"],
                   format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
                          "<level>{level: <7}</level> | <level>{message}</level>")

    def _sig(self, signum, _frame) -> None:
        logger.warning("Signal {} - shutting down (open pairs persist, not closed).", signum)
        self.running = False

    # ------------------------------------------------------------------ #
    def run(self) -> None:
        try:
            self.data.validate(self.assets)
        except Exception as exc:
            logger.warning("Startup validation skipped: {}", exc)
        if self.rt["place_orders"]:
            for asset in self.assets:
                try:
                    _, perp_sym = self.data.resolve(asset)
                    self.executor.set_leverage(perp_sym, self.leverage)
                except Exception as exc:
                    logger.warning("{} leverage setup skipped: {}", asset, exc)
        self.notifier.message(f"🪢CARRY bot started\nMode: {self._mode}\n"
                              f"Assets: {', '.join(self.assets)}")

        while self.running:
            try:
                self._cycle()
            except Exception as exc:
                logger.exception("Carry cycle error (continuing): {}", exc)
                self.notifier.error(f"🪢CARRY cycle error: {exc}")
            self._sleep(self.poll_seconds)
        logger.info("Carry loop stopped.")

    def _sleep(self, seconds: int) -> None:
        for _ in range(seconds):
            if not self.running:
                return
            time.sleep(1)

    # ------------------------------------------------------------------ #
    def _cycle(self) -> None:
        any_quote = None
        for asset in self.assets:
            try:
                quote = self.data.funding_quote(asset)
            except Exception as exc:
                logger.warning("{} quote failed (skipping this poll): {}", asset, exc)
                continue
            any_quote = quote
            pos = self.risk.open_position(asset)
            if pos is None:
                self._maybe_open(asset, quote)
                continue
            # A half-done unwind takes priority: finish the remaining leg, no accrual.
            if self.risk.unwind_in_progress(pos):
                logger.warning("{} resuming a partial unwind.", asset)
                self._unwind(asset, quote, pos, "resume partial unwind")
                continue
            self.risk.accrue_funding(pos, quote.funding_apr)
            pos = self.risk.open_position(asset)  # refresh accrued totals
            self._manage(asset, quote, pos)

        self._maybe_heartbeat(any_quote)

    def _manage(self, asset: str, quote, pos) -> None:
        decision = evaluate(quote, held=True, low_reads=int(pos["low_reads"]),
                            params=self.params)
        self.risk.update_low_reads(int(pos["id"]), decision.low_reads)

        if self.risk.delta_breach(pos):
            logger.warning("{} delta drift beyond tolerance (partial fill?) - alerting.", asset)
            self.notifier.error(f"🪢CARRY {asset} delta drift - check legs.")

        if self.rt["place_orders"]:
            _, perp_sym = self.data.resolve(asset)
            mr = self.data.perp_margin_ratio(perp_sym)
            if mr is not None and mr < self.margin_alert:
                logger.warning("{} margin ratio {:.2f} < alert {:.2f}.", asset, mr, self.margin_alert)
                self.notifier.error(f"🪢CARRY {asset} margin low ({mr:.2f}) - consider de-risking.")

        if decision.action != "UNWIND":
            logger.info("{} HOLD | funding {:.2%}/yr | {}", asset, decision.gross_apr, decision.reason)
            return
        self._unwind(asset, quote, pos, decision.reason)

    def _unwind(self, asset: str, quote, pos, reason: str) -> None:
        """Resumable, leg-aware unwind. Each leg is closed and persisted
        independently, so a failure (or restart) mid-unwind resumes next poll
        without ever re-hitting an already-closed leg."""
        spot_sym, perp_sym = self.data.resolve(asset)
        pos = self.risk.open_position(asset)
        if pos is None:
            return
        # 1) Cover the short perp (the scarier exposure) first.
        if not pos["perp_closed"]:
            fill = self.executor.cover_perp(asset, perp_sym, float(pos["perp_qty"]), quote.perp)
            if fill is None:
                self.notifier.error(f"🪢CARRY {asset} perp cover failed - retrying next poll.")
                return
            self.risk.mark_perp_closed(int(pos["id"]), fill)
            pos = self.risk.open_position(asset)
        # 2) Sell the long spot.
        if not pos["spot_closed"]:
            fill = self.executor.sell_spot(asset, spot_sym, float(pos["spot_qty"]), quote.spot)
            if fill is None:
                self.notifier.error(f"🪢CARRY {asset} spot sell failed - perp already covered, "
                                    f"holding spot long; retrying next poll.")
                return
            self.risk.mark_spot_closed(int(pos["id"]), fill)
            pos = self.risk.open_position(asset)
        # 3) Both legs closed -> settle.
        realized = self.risk.finalize_unwind(pos, reason)
        emoji = "✅" if realized >= 0 else "🔻"
        self.notifier.message(f"{emoji} 🪢CARRY UNWIND {asset}\n"
                              f"Realized: ${realized:,.2f}\nReason: {reason}")

    def _maybe_open(self, asset: str, quote) -> None:
        allowed, why = self.risk.can_open(asset)
        if not allowed:
            logger.debug("{} no open: {}", asset, why)
            return
        decision = evaluate(quote, held=False, low_reads=0, params=self.params)
        if decision.action != "OPEN":
            logger.info("{} flat | net {:.2%}/yr | {}", asset, decision.net_apr, decision.reason)
            return
        sizing = self.risk.size(quote.spot)
        if not sizing["viable"]:
            logger.info("{} open skipped: size ${:.2f} below min / sleeve exhausted.",
                        asset, sizing["notional"])
            return
        spot_sym, perp_sym = self.data.resolve(asset)
        try:
            fills = self.executor.open_pair(asset, spot_sym, perp_sym, sizing["notional"],
                                            quote.spot, quote.perp)
        except CarryExecutionError as exc:
            logger.critical("{}", exc)
            self.notifier.error(f"🪢CARRY CRITICAL {exc}")
            return
        if fills is None:
            logger.error("{} open failed (legs not taken).", asset)
            return
        self.risk.record_open(fills, sizing["capital"], decision.reason)
        self.notifier.message(f"🟢 🪢CARRY OPEN {asset}\n"
                              f"Notional: ${sizing['notional']:,.2f}\n"
                              f"Net carry: {decision.net_apr:.1%}/yr\n{decision.reason}")

    # ------------------------------------------------------------------ #
    def _maybe_heartbeat(self, any_quote) -> None:
        now = time.time()
        if any_quote is not None:
            self._maybe_summary(any_quote)
        if now - self._last_heartbeat >= self.heartbeat_every:
            self._last_heartbeat = now
            stats = self.risk.daily_stats()
            logger.info("HEARTBEAT {}", stats)

    def _maybe_summary(self, quote) -> None:
        from datetime import datetime
        day = datetime.now(timezone.utc).date().isoformat()
        if day == self._last_summary_day:
            return
        self._last_summary_day = day
        stats = self.risk.daily_stats()
        logger.info("CARRY DAILY SUMMARY {}", stats)
        self.notifier.message(
            f"📊 🪢CARRY summary ({stats['date_utc']})\nMode: {stats['mode']}\n"
            f"Open pairs: {stats['open_pairs']} | Capital used: ${stats['capital_used']:,.2f}\n"
            f"Funding today: ${stats['funding_today_usd']:.4f} | "
            f"Realized: ${stats['realized_today_usd']:,.2f}")


def main() -> None:
    CarryBot().run()


if __name__ == "__main__":
    main()
