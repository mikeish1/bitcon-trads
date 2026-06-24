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
from datetime import datetime, timezone
from typing import Any, Optional

from loguru import logger

from src.claude_orchestrator import ClaudeOrchestrator
from src.config import load_config
from src.data_pipeline import DataPipeline, build_exchange
from src.executor import SpotExecutor
from src.cost_model import universe_costs, live_costs, cost_preference_mode, cost_penalty
from src.momentum_allocator import MomentumRotation
from src.notifications import Notifier
from src.portfolio_sleeve_allocator import SleeveAllocator, build_sleeve_performance
from src.regime import RegimeState, regime_from_config
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
        # Regime gating: the new `strategy.regime` block supersedes the legacy
        # `strategy.btc_regime` when enabled; either being on activates gating.
        rg = sd.get("regime", {}) or {}
        self.regime_new = bool(rg.get("enabled", False))
        self.regime_enabled = self.regime_new or sd.get("btc_regime", {}).get("enabled", False)
        self.regime_ref = str(rg.get("reference", "BTC")).upper() if self.regime_new else "BTC"
        # In risk-off: flatten open positions (legacy behaviour, default), and/or use
        # a tighter chandelier on whatever is still held.
        self.regime_risk_off_exit = bool(rg.get("risk_off_exit", True)) if self.regime_new else True
        self.regime_tighten_trail = rg.get("tighten_trail_mult", None) if self.regime_new else None
        self._regime = RegimeState(True, 1.0, 1.0, "init", "startup (assume risk-on)")
        self._pvol: float | None = None    # expected portfolio daily-vol proxy (for vol targeting)

        # Optional thin sleeve overlay: logs daily target weights across the three
        # sibling sleeves. INFORMATIONAL only - it never changes this bot's sizing,
        # and the carry/ETF bots are untouched. Default off.
        self.sleeves_enabled = bool((sd_pf := self.cfg.get("portfolio", {}) or {})
                                    .get("sleeves", {}).get("enabled", False))
        self._sleeve_alloc = SleeveAllocator(self.cfg) if self.sleeves_enabled else None
        self._sleeve_prev: Optional[dict[str, float]] = None

        # Cost-aware pair preference (off | soft | strict). Effective round-trip cost
        # per coin is recomputed each cycle from the live frames.
        self.cost_mode = cost_preference_mode(self.cfg)
        self.max_cost_bps = float(self.cfg["execution"].get("max_effective_cost_bps", 60.0))
        self.cost_use_live_quotes = bool(self.cfg["execution"].get("cost_use_live_quotes", False))
        self._costs: dict[str, float] = {}

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

        # Startup reconcile (broker venues): close DB rows whose coins are gone AND
        # adopt untracked holdings (orphaned fills from a crash) with a protective
        # stop. Gather confirmed-close prices + ATRs so an adopted stop uses the
        # chandelier rather than a tight default.
        try:
            balances = self.data.fetch_balances()
            prices, atrs = {}, {}
            for sym in self.symbols:
                try:
                    frames = self.data.signal_frames(self.data.get_frames(sym))
                    last = frames[self.primary_tf].iloc[-1]
                    prices[_base_of(sym)] = float(last["close"])
                    if "atr" in last and last["atr"] == last["atr"]:
                        atrs[_base_of(sym)] = float(last["atr"])
                except Exception:
                    pass
            self.risk.reconcile(balances, prices, atrs,
                                exit_resolver=self.executor.resolve_fill_price)
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
            # Closed-candle view for DECISIONS (breakout signal + trail ratchet). The
            # live price/atr above stay for marking, sizing and the exit-breach check.
            sig_frames = self.data.signal_frames(frames)
            sig_last = sig_frames[self.primary_tf].iloc[-1]
            sig_price = float(sig_last["close"])
            sig_atr = (float(sig_last["atr"])
                       if "atr" in sig_last and sig_last["atr"] == sig_last["atr"] else 0.0)
            snap[sym] = {"frames": frames, "price": price, "atr": atr, "ts": last["timestamp"],
                         "sig_frames": sig_frames, "sig_price": sig_price, "sig_atr": sig_atr}
            prices[_base_of(sym)] = price

        # Market regime: is BTC in an uptrend? (gates entries + forces risk-off exits)
        self._update_regime(snap)

        # Pass 2: manage open positions (now that regime is known).
        for sym, s in snap.items():
            if self.risk.open_position(sym) is not None:
                self._manage(sym, s["price"], s["atr"], balances,
                             signal_price=s["sig_price"], signal_atr=s["sig_atr"])

        equity = self.risk.current_equity(balances, prices)
        avail_quote = self.risk.available_quote(balances)
        open_value = self.risk.open_value(prices)
        n_open = len(self.risk.open_positions())

        # Record the daily equity mark, then resolve the portfolio-vol estimate the
        # optional global vol-target scalar uses: realized equity-curve vol when
        # configured (and warmed up), else the instant ATR proxy.
        self.risk.record_equity(equity)
        self._pvol = self.risk.effective_portfolio_vol(self._portfolio_vol(snap))

        # Daily summary once per UTC day (use the primary symbol's candle clock).
        any_ts = next((s["ts"] for s in snap.values()), None)
        if any_ts is not None:
            self._maybe_summary(any_ts, equity)

        # Cost-aware preference: effective round-trip cost per coin (off -> empty).
        # Real venue fees + live spreads when enabled, else the daily-range proxy.
        if self.cost_mode != "off":
            frames_by_sym = {sym: s["frames"] for sym, s in snap.items()}
            if self.cost_use_live_quotes:
                self._costs = live_costs(frames_by_sym, self.exchange, self.cfg, self.primary_tf)
            else:
                self._costs = universe_costs(frames_by_sym, self.cfg, self.primary_tf)

        # Entry phase. Two allocation modes:
        if self.alloc_mode == "momentum_rotation":
            self._rotate(snap, equity, avail_quote, open_value, n_open)
        else:
            # first_come: each flat symbol with a freshly closed candle is sized alone.
            # SOFT cost mode trades the cheapest-to-execute breakouts first (so the
            # limited exposure budget favours low-cost coins); STRICT skips coins
            # whose effective cost exceeds the ceiling.
            items = list(snap.items())
            if self.cost_mode == "soft" and self._costs:
                items.sort(key=lambda kv: self._costs.get(kv[0], 1e9))
            for sym, s in items:
                if self.risk.open_position(sym) is not None:
                    continue
                if self.cost_mode == "strict" and self._costs.get(sym, 0.0) > self.max_cost_bps:
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
            self._regime = RegimeState(True, 1.0, 1.0, "disabled", "regime gating off")
            return
        ref_sym = f"{self.regime_ref}/{self._quote}"
        bf = None
        try:
            bf = (snap[ref_sym]["frames"][self.primary_tf] if ref_sym in snap
                  else self.data.get_frames(ref_sym)[self.primary_tf])
        except Exception as exc:
            logger.warning("{} regime check failed ({}); assuming risk-on.", ref_sym, exc)
        new = regime_from_config(bf, self.cfg)   # fail-open to risk-on inside
        if new.risk_on != self._regime.risk_on:
            state = (f"RISK-ON ({self.regime_ref} {new.reason})" if new.risk_on
                     else f"RISK-OFF ({self.regime_ref} {new.reason}); size x{new.size_factor:g}")
            logger.warning("Market regime -> {}", state)
            self.notifier.error(f"Market regime: {state}")
        self._regime = new

    def _portfolio_vol(self, snap: dict[str, dict]) -> float | None:
        """Expected daily portfolio-vol proxy = mean ATR% across held coins (or the
        whole snapshot when flat). For a ~0.8-correlated crypto book this is a
        reasonable stand-in for realized portfolio vol and needs no equity history.
        Feeds the optional global vol-target scalar in RiskManager."""
        held = {_base_of(p["symbol"]) for p in self.risk.open_positions()}
        pcts = []
        for sym, s in snap.items():
            if s["price"] and s["atr"] and s["atr"] > 0:
                if not held or _base_of(sym) in held:
                    pcts.append(s["atr"] / s["price"])
        return (sum(pcts) / len(pcts)) if pcts else None

    def _try_enter(self, sym, s, equity, avail_quote, open_value, n_open) -> float:
        regime_factor = self._regime.size_factor if self.regime_enabled else 1.0
        if regime_factor <= 0:
            return 0.0  # market risk-off: no new entries
        ep = self.overrides.get(_base_of(sym), {}).get("entry_period")
        sig_frames = s.get("sig_frames", s["frames"])      # confirmed-close view
        if self.strategy_mode == "donchian":
            decision = self.strategy.decide(sig_frames, entry_period=ep)
        else:
            decision = self.strategy.decide(sig_frames)
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
        sizing = self.risk.size_for_asset(equity, avail_quote, open_value, atr_pct=atr_pct,
                                          portfolio_vol=self._pvol, regime_factor=regime_factor)
        if not sizing["viable"]:
            logger.info("{} BUY skipped: size ${:.2f} below min / exposure cap.", sym, sizing["spend_usd"])
            return 0.0
        if self.risk.risk_budget_enabled or regime_factor < 1.0:
            logger.info("{} sizing: spend ${:.2f} | per-asset ${:.2f} | risk-notional {} | "
                        "vol-scalar {:.2f} | regime x{:.2f}", sym, sizing["spend_usd"],
                        sizing["per_asset_cap"],
                        f"${sizing['risk_notional']:.2f}" if sizing["risk_notional"] else "n/a",
                        sizing["vol_scalar"], sizing["regime_factor"])

        price = s["price"]
        fill = self.executor.open_buy(sym, sizing["spend_usd"], price, intended_price=price)
        if fill is None:
            return 0.0
        # The order is LIVE from here. Persist (+ protective stop) inside a guard so a
        # crash/exception after the fill is loud and recoverable: the position is left
        # for reconcile() to ADOPT next startup rather than running untracked.
        try:
            stop0 = self.risk.chandelier_stop(fill["price"], s["atr"])
            stop_order_id = None
            if self.use_exchange_stop:
                stop_order_id = self.executor.place_protective_stop(sym, fill["qty"], stop0)
            self.risk.record_open(sym, fill, stop0, 0.0, stop_order_id, decision.reasoning,
                                  peak_price=fill["price"], entry_atr=s["atr"])
        except Exception as exc:
            logger.exception("{} FILLED but failed to record position: {}", sym, exc)
            self.notifier.error(f"{sym} FILLED but NOT booked ({exc}); reconcile will adopt it.")
            return fill.get("cost", 0.0)
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
        regime_factor = self._regime.size_factor if self.regime_enabled else 1.0
        if regime_factor <= 0:
            self.risk.state_set("last_rotation_day", today)
            return

        # Candidates = coins in an active Donchian trend, scored for top-K ranking
        # (simple ROC or composite blend per config). active_state filters first.
        active_frames: dict[str, dict] = {}
        for sym, s in snap.items():
            ep = self.overrides.get(_base_of(sym), {}).get("entry_period")
            sig_frames = s.get("sig_frames", s["frames"])    # confirmed-close view
            if self.strategy.active_state(sig_frames, entry_period=ep):
                active_frames[sym] = sig_frames
        btc_frames = snap.get(f"BTC/{self._quote}", {}).get("sig_frames")
        cands = self.rotation.score_candidates(active_frames, btc_frames)
        # Cost-aware preference on the candidate scores: STRICT drops dear coins;
        # SOFT applies a small cost penalty as a tie-breaker (never overrides regime
        # or active-trend gates, which already filtered `active_frames`).
        if self.cost_mode == "strict" and self._costs:
            dropped = [s for s in cands if self._costs.get(s, 0.0) > self.max_cost_bps]
            if dropped:
                logger.info("Cost filter (strict): dropping {} (> {} bps).", dropped, self.max_cost_bps)
            cands = {s: v for s, v in cands.items() if self._costs.get(s, 0.0) <= self.max_cost_bps}
        elif self.cost_mode == "soft" and self._costs:
            cands = {s: v - cost_penalty(s, self._costs, self.cfg) for s, v in cands.items()}
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
        regime_factor = self._regime.size_factor if self.regime_enabled else 1.0
        sizing = self.risk.size_rotation(equity, avail_quote, open_value, self.rotation.top_k,
                                         portfolio_vol=self._pvol, regime_factor=regime_factor)
        if not sizing["viable"]:
            logger.info("{} rotation entry skipped: size ${:.2f} below min / exposure cap.",
                        sym, sizing["spend_usd"])
            return 0.0
        price = s["price"]
        fill = self.executor.open_buy(sym, sizing["spend_usd"], price, intended_price=price)
        if fill is None:
            return 0.0
        reason = f"momentum rotation: top-{self.rotation.top_k} entry"
        # Guard the post-fill persistence (see _try_enter): an orphaned fill is left
        # for reconcile() to adopt rather than running with no stop/trail.
        try:
            stop0 = self.risk.chandelier_stop(fill["price"], s["atr"])
            stop_order_id = None
            if self.use_exchange_stop:
                stop_order_id = self.executor.place_protective_stop(sym, fill["qty"], stop0)
            self.risk.record_open(sym, fill, stop0, 0.0, stop_order_id, reason,
                                  peak_price=fill["price"], entry_atr=s["atr"])
            self.risk.log_decision(sym, "BUY", 1, False, reason)
        except Exception as exc:
            logger.exception("{} FILLED but failed to record rotation position: {}", sym, exc)
            self.notifier.error(f"{sym} FILLED but NOT booked ({exc}); reconcile will adopt it.")
            return fill.get("cost", 0.0)
        self._cb_notified = False
        self.notifier.entry(fill["price"], fill["qty"], fill["cost"], stop0, 0.0, f"{sym}: {reason}")
        return fill["cost"]

    # ------------------------------------------------------------------ #
    def _manage(self, sym: str, price: float, atr: float, balances: dict[str, float],
                signal_price: float | None = None, signal_atr: float | None = None) -> None:
        pos = self.risk.open_position(sym)
        if pos is None:
            return
        base = _base_of(sym)
        if self.cfg["runtime"]["uses_broker"]:
            dust = self.cfg["risk"]["min_notional_usd"] / price if price else 0
            if balances.get(base, 0.0) < dust and pos["qty"] > dust:
                # Stop filled offline: book the venue's ACTUAL fill when resolvable,
                # else a conservative estimate so the loss isn't under-counted.
                est = self.risk.offline_exit_estimate(pos, price)
                exit_px = self.executor.resolve_fill_price(sym, pos["stop_order_id"], est)
                self.risk.record_close(pos, exit_px, 0.0, "exchange stop filled")
                self.notifier.exit(exit_px, 0.0, f"{sym}: exchange stop filled")
                return

        # Market risk-off -> flatten (legacy default) unless configured to hold+tighten.
        if (self.regime_enabled and not self._regime.risk_on and self.regime_risk_off_exit):
            self._exit(sym, pos, price, f"{self.regime_ref} risk-off")
            return

        # --- Staged profit-taking: scale out tranches + ratchet/breakeven (opt-in) -
        trail_mult: float | None = None
        breakeven_floor: float | None = None
        scaled = False
        if self.risk.profit_taking_enabled and atr > 0:
            plan = self.risk.profit_taking_plan(pos, price, atr)
            trail_mult, breakeven_floor = plan["trail_mult"], plan["breakeven_floor"]
            if plan["scale_fraction"] > 0:
                scaled = self._scale_out(sym, pos, price, plan)
                pos = self.risk.open_position(sym)
                if pos is None:
                    return  # position fully exited (final tranche) - nothing left to manage

        # Time-based exit for very long winners that have already scaled (optional).
        if (self.risk.pt_time_stop_days > 0 and (pos["tranches_done"] or 0) > 0
                and self._days_held(pos) >= self.risk.pt_time_stop_days):
            self._exit(sym, pos, price, f"time stop ({self.risk.pt_time_stop_days}d runner)")
            return

        # Risk-off but configured to hold and tighten rather than flatten.
        if (self.regime_enabled and not self._regime.risk_on
                and self.regime_tighten_trail is not None):
            tm = float(self.regime_tighten_trail)
            trail_mult = tm if trail_mult is None else min(trail_mult, tm)

        # Ratchet peak + chandelier off the CONFIRMED close/ATR (no intrabar-wick
        # ratchet that the close-based backtest never sees); the live `price` is still
        # used for the exit-breach check below, so genuine intraday breaks still exit.
        trail_price = signal_price if signal_price is not None else price
        trail_atr = signal_atr if signal_atr is not None else atr
        prev_stop = pos["current_stop"] or pos["stop_price"]
        peak = max(pos["peak_price"] or pos["entry_price"], trail_price)
        new_stop = (self.risk.chandelier_stop(peak, trail_atr, mult=trail_mult)
                    if trail_atr > 0 else prev_stop)
        current_stop = max(prev_stop, new_stop)
        if breakeven_floor is not None:                 # lock in BE+buffer once armed
            current_stop = max(current_stop, breakeven_floor)
        sid = pos["stop_order_id"]
        # Re-place the resting stop if it ratcheted up OR a scale-out changed the qty.
        if (self.cfg["runtime"]["place_orders"] and self.use_exchange_stop
                and (scaled or current_stop > prev_stop * 1.001)):
            self.executor.cancel(sym, pos["stop_order_id"])
            sid = self.executor.place_protective_stop(sym, pos["qty"], current_stop)
            if current_stop > prev_stop * 1.001:
                logger.info("{} chandelier raised {:.4f} -> {:.4f}", sym, prev_stop, current_stop)
        self.risk.update_trail(pos["id"], peak, current_stop, sid)

        if price <= current_stop:
            self._exit(sym, pos, price, "chandelier trail")

    def _scale_out(self, sym: str, pos, price: float, plan: dict) -> bool:
        """Execute one staged profit-taking sell of a fraction of the ORIGINAL
        position. Cancels the resting exchange stop first (re-placed for the reduced
        qty by the caller). If the remainder would be unsellable dust, exits fully
        instead. Returns True iff a partial sell was booked."""
        orig = pos["orig_qty"] or pos["qty"]
        sell_qty = min(plan["scale_fraction"] * orig, pos["qty"])
        remaining = pos["qty"] - sell_qty
        if sell_qty <= 0:
            return False
        if remaining * price < self.cfg["risk"]["min_notional_usd"]:
            self._exit(sym, pos, price, "profit-taking (final tranche)")
            return False
        if self.cfg["runtime"]["place_orders"] and self.use_exchange_stop:
            self.executor.cancel(sym, pos["stop_order_id"])
        fill = self.executor.market_sell(sym, sell_qty, price, plan["reason"])
        if fill is None:
            logger.error("{} scale-out sell failed - will retry next cycle.", sym)
            return False
        realized = self.risk.reduce_position(pos, fill, plan["new_tranches"], plan["reason"])
        self.notifier.exit(fill["price"], realized, f"{sym}: {plan['reason']}")
        return True

    @staticmethod
    def _days_held(pos) -> float:
        try:
            opened = datetime.fromisoformat(pos["opened_at"])
            if opened.tzinfo is None:
                opened = opened.replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - opened).total_seconds() / 86400.0
        except (TypeError, ValueError):
            return 0.0

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
        self._maybe_log_sleeve_targets()
        self._maybe_run_ops_agent()

    def _maybe_run_ops_agent(self) -> None:
        """Optional daily ops check (read-only analysis + WRITES pending proposals to
        the approval gate; never applies anything). Off unless ops_agent.enabled.
        Wrapped so a failure can never disturb the trading loop."""
        if not (self.cfg.get("ops_agent", {}) or {}).get("enabled", False):
            return
        try:
            from src.claude_orchestrator import OpsAgent
            OpsAgent(self.cfg, orchestrator=self.claude).run_daily_ops()
        except Exception as exc:
            logger.warning("Ops agent daily run skipped: {}", exc)

    def _maybe_log_sleeve_targets(self) -> None:
        """Informational meta-view: compute target weights across the Donchian,
        Carry and ETF sleeves from each sleeve's recent equity (read read-only from
        the shared DB) and log them. Never moves capital; wrapped so a failure can
        never disturb the trading loop. Off unless portfolio.sleeves.enabled."""
        if not self.sleeves_enabled or self._sleeve_alloc is None:
            return
        try:
            perf = build_sleeve_performance(self.cfg["runtime"]["db_path"], self.cfg)
            if not perf:
                logger.info("Sleeve overlay: insufficient sleeve history yet - skipping.")
                return
            regime = {"risk_on": self._regime.risk_on} if self.regime_enabled else None
            weights = self._sleeve_alloc.compute_weights(
                perf, regime_state=regime, prev_weights=self._sleeve_prev)
            self._sleeve_prev = weights
            logger.info("Sleeve target weights (informational, no capital moved): {}", weights)
        except Exception as exc:
            logger.warning("Sleeve overlay skipped: {}", exc)


def main() -> None:
    TradingBot().run()


if __name__ == "__main__":
    main()
