"""
Order executor (multi-asset spot: Binance.US or Alpaca).

Symbol-agnostic: every method takes the symbol to act on, so one executor serves
the whole universe. Three execution modes (decided in config):

  * SIMULATION   - internal paper fills, no API orders        (Binance.US paper)
  * PAPER-BROKER - real orders to a PAPER endpoint, no money  (Alpaca paper)
  * LIVE         - real orders with real money                (two-key tripwire)

Long-only spot:
    market_buy(symbol, quote)       -> buy base coin with quote cash
    market_sell(symbol, qty)        -> sell base coin for quote cash
    place_stop_limit_sell(symbol..) -> exchange-side protective stop (best-effort)
    cancel(symbol, order_id)        -> cancel a resting order
"""
from __future__ import annotations

import time
from typing import Any, Optional

import ccxt
from loguru import logger

from src.slippage import SlippageRecorder


class SpotExecutor:
    def __init__(self, cfg: dict[str, Any], exchange: ccxt.Exchange):
        self.cfg = cfg
        self.exchange = exchange
        self.place = cfg["runtime"]["place_orders"]
        self.real_money = cfg["runtime"]["real_money"]
        ex = cfg["execution"]
        self.fee_pct = ex["taker_fee_pct"]
        self.maker_fee_pct = float(ex.get("maker_fee_pct", self.fee_pct))
        self.slip = ex["paper_slippage_pct"]
        self.min_notional = cfg["risk"]["min_notional_usd"]
        self._seq = 0
        self.tag = "[LIVE]" if self.real_money else ("[PAPER-BROKER]" if self.place else "[SIM]")
        self.mode_tag = "LIVE" if self.real_money else ("PAPER-BROKER" if self.place else "SIM")
        self._log = logger.warning if self.real_money else logger.info

        # --- Limit-order entry config (market fallback preserves robustness) ---
        self.use_limit_entry = bool(ex.get("use_limit_orders_on_entry", False))
        self.entry_limit_offset_bps = float(ex.get("entry_limit_offset_bps", 0.0))
        self.limit_timeout = float(ex.get("limit_order_timeout_sec", 60))
        self.limit_poll = max(0.1, float(ex.get("limit_poll_interval_sec", 3)))
        self.post_only = bool(ex.get("post_only", False))
        self.paper_fill_model = str(ex.get("paper_limit_fill_model", "optimistic")).lower()
        self.paper_fill_ratio = min(1.0, max(0.0, float(ex.get("paper_limit_fill_ratio", 1.0))))
        self.max_entry_chase_bps = float(ex.get("max_entry_chase_bps", 0.0) or 0.0)

        # --- Slippage instrumentation (intended vs actual on every fill) ---
        self.slippage = SlippageRecorder(
            cfg["runtime"]["db_path"],
            enabled=bool(ex.get("slippage_logging_enabled", True)),
            tolerance_bps=float(ex.get("max_slippage_tolerance_bps", 50.0)))

    # ------------------------------------------------------------------ #
    def _attach_slippage(self, fill: Optional[dict[str, Any]], symbol: str, side: str,
                         order_type: str, intended_price: float, reason: str = "") -> Optional[dict[str, Any]]:
        """Record this fill's slippage and annotate the fill dict (additive keys, so
        existing callers are unaffected). No-op-safe when fill is None."""
        if fill is None:
            return None
        s = self.slippage.record(symbol, side, order_type, intended_price or fill["price"],
                                 fill["price"], fill["qty"], fill.get("fee", 0.0),
                                 self.mode_tag, reason)
        fill.update(s)
        fill["order_type"] = order_type
        return fill

    def _chase_ok(self, intended: float, fallback_price: float) -> bool:
        """True if a market FALLBACK at `fallback_price` is within the configured
        chase tolerance of the signal price. When it isn't, the caller abandons the
        entry rather than chasing a breakout that already ran away (0 = no limit)."""
        if self.max_entry_chase_bps <= 0 or not intended or intended <= 0:
            return True
        chase_bps = (fallback_price - intended) / intended * 1e4
        if chase_bps > self.max_entry_chase_bps:
            logger.info("{} chase guard: fallback {:+.1f} bps > {:.0f} bps from signal - "
                        "abandoning unfilled remainder.", self.tag, chase_bps, self.max_entry_chase_bps)
            return False
        return True

    def _fresh_ask(self, symbol: str, default: float) -> float:
        """Best-effort current ask for the live chase decision; `default` on failure."""
        try:
            t = self.exchange.fetch_ticker(symbol)
            return float(t.get("ask") or t.get("last") or default)
        except Exception:
            return default

    # ------------------------------------------------------------------ #
    def amount_prec(self, symbol: str, qty: float) -> float:
        try:
            return float(self.exchange.amount_to_precision(symbol, qty))
        except Exception:
            return float(f"{qty:.8f}")

    def price_prec(self, symbol: str, price: float) -> float:
        try:
            return float(self.exchange.price_to_precision(symbol, price))
        except Exception:
            return float(f"{price:.2f}")

    def _min_notional(self, symbol: str) -> float:
        try:
            lim = self.exchange.market(symbol)["limits"]["cost"]["min"]
            if lim:
                return max(float(lim), self.min_notional)
        except Exception:
            pass
        return self.min_notional

    # ------------------------------------------------------------------ #
    def market_buy(self, symbol: str, quote_to_spend: float, price_hint: float,
                   intended_price: Optional[float] = None,
                   order_type: str = "market") -> Optional[dict[str, Any]]:
        """Market buy (also the fallback path for limit entries). `intended_price`
        is the signal/sizing price used for slippage measurement (defaults to the
        hint); `order_type` labels the fill in the slippage log."""
        intended = intended_price if intended_price is not None else price_hint
        if quote_to_spend < self._min_notional(symbol):
            logger.info("{} {} buy skipped: {:.2f} below min notional {:.2f}.",
                        self.tag, symbol, quote_to_spend, self._min_notional(symbol))
            return None
        qty = self.amount_prec(symbol, quote_to_spend / price_hint)
        if qty <= 0:
            return None

        if not self.place:
            entry = price_hint * (1 + self.slip)
            cost = qty * entry
            fee = cost * self.fee_pct
            self._seq += 1
            logger.info("{} BUY {} {:.6f} @ ~{:.4f} (cost {:.2f}, fee {:.2f})",
                        self.tag, symbol, qty, entry, cost, fee)
            fill = {"id": f"sim-buy-{self._seq}", "qty": qty, "price": entry, "cost": cost, "fee": fee}
            return self._attach_slippage(fill, symbol, "buy", order_type, intended)

        try:
            order = self.exchange.create_market_buy_order(symbol, qty)
            filled = float(order.get("filled") or qty)
            cost = float(order.get("cost") or (filled * price_hint))
            avg = float(order.get("average") or (cost / filled if filled else price_hint))
            fee = self._extract_fee(order)
            self._log("{} BUY {} filled {:.6f} @ {:.4f} (cost {:.2f}, fee {:.2f})",
                      self.tag, symbol, filled, avg, cost, fee)
            fill = {"id": order.get("id"), "qty": filled, "price": avg, "cost": cost, "fee": fee}
            return self._attach_slippage(fill, symbol, "buy", order_type, intended)
        except Exception as exc:
            logger.error("{} {} BUY failed: {}", self.tag, symbol, exc)
            return None

    def market_sell(self, symbol: str, qty: float, price_hint: float, reason: str,
                    intended_price: Optional[float] = None,
                    order_type: str = "market") -> Optional[dict[str, Any]]:
        intended = intended_price if intended_price is not None else price_hint
        qty = self.amount_prec(symbol, qty)
        if qty <= 0:
            return None

        if not self.place:
            exitp = price_hint * (1 - self.slip)
            proceeds = qty * exitp
            fee = proceeds * self.fee_pct
            self._seq += 1
            logger.info("{} SELL {} {:.6f} @ ~{:.4f} ({}) proceeds {:.2f} fee {:.2f}",
                        self.tag, symbol, qty, exitp, reason, proceeds, fee)
            fill = {"id": f"sim-sell-{self._seq}", "qty": qty, "price": exitp,
                    "proceeds": proceeds, "fee": fee}
            return self._attach_slippage(fill, symbol, "sell", order_type, intended, reason)

        try:
            order = self.exchange.create_market_sell_order(symbol, qty)
            filled = float(order.get("filled") or qty)
            proceeds = float(order.get("cost") or (filled * price_hint))
            avg = float(order.get("average") or (proceeds / filled if filled else price_hint))
            fee = self._extract_fee(order)
            self._log("{} SELL {} filled {:.6f} @ {:.4f} ({}) proceeds {:.2f} fee {:.2f}",
                      self.tag, symbol, filled, avg, reason, proceeds, fee)
            fill = {"id": order.get("id"), "qty": filled, "price": avg, "proceeds": proceeds, "fee": fee}
            return self._attach_slippage(fill, symbol, "sell", order_type, intended, reason)
        except Exception as exc:
            logger.error("{} {} SELL failed: {}", self.tag, symbol, exc)
            return None

    # ------------------------------------------------------------------ #
    # Entry order: LIMIT (near the signal price) with timeout + MARKET fallback
    # ------------------------------------------------------------------ #
    def open_buy(self, symbol: str, quote_to_spend: float, price_hint: float,
                 intended_price: Optional[float] = None) -> Optional[dict[str, Any]]:
        """Place a breakout ENTRY. When execution.use_limit_orders_on_entry is set,
        rest a limit at price_hint*(1+offset) and, if it is not (fully) filled within
        limit_order_timeout_sec, cancel and market-buy the remainder so a real
        breakout is never missed. Otherwise a plain market buy. Returns the same
        dict shape as market_buy (plus slippage_bps / order_type). Paper mode models
        the limit filling at the limit price (price improvement vs the slippage path).
        """
        intended = intended_price if intended_price is not None else price_hint
        if not self.use_limit_entry:
            return self.market_buy(symbol, quote_to_spend, price_hint, intended_price=intended)
        if quote_to_spend < self._min_notional(symbol):
            logger.info("{} {} entry skipped: {:.2f} below min notional {:.2f}.",
                        self.tag, symbol, quote_to_spend, self._min_notional(symbol))
            return None

        limit_price = self.price_prec(symbol, price_hint * (1 + self.entry_limit_offset_bps / 1e4))
        qty = self.amount_prec(symbol, quote_to_spend / max(limit_price, 1e-12))
        if qty <= 0:
            return None

        if not self.place:
            return self._sim_open_buy(symbol, qty, limit_price, price_hint, intended)
        return self._live_open_buy(symbol, qty, quote_to_spend, limit_price, price_hint, intended)

    def _sim_open_buy(self, symbol: str, qty: float, limit_price: float,
                      price_hint: float, intended: float) -> Optional[dict[str, Any]]:
        """Paper limit entry. `optimistic` fills the whole order at the limit price;
        `realistic` fills only `paper_limit_fill_ratio` at the limit and routes the
        remainder to a market fallback (or abandons it via the chase guard), so paper
        reflects that passive limits don't always fully fill."""
        ratio = self.paper_fill_ratio if self.paper_fill_model == "realistic" else 1.0
        lim_qty = qty * ratio
        result: Optional[dict[str, Any]] = None
        if lim_qty > 0:
            cost = lim_qty * limit_price
            fee = cost * (self.maker_fee_pct if self.post_only else self.fee_pct)
            self._seq += 1
            logger.info("{} LIMIT BUY {} {:.6f} @ {:.4f} (cost {:.2f}, fee {:.2f}) [sim fill]",
                        self.tag, symbol, lim_qty, limit_price, cost, fee)
            result = self._attach_slippage(
                {"id": f"sim-limit-buy-{self._seq}", "qty": lim_qty, "price": limit_price,
                 "cost": cost, "fee": fee}, symbol, "buy", "limit", intended)
        rem_qty = qty - lim_qty
        if rem_qty > 1e-12:
            fallback_price = price_hint * (1 + self.slip)
            if self._chase_ok(intended, fallback_price):
                mkt = self.market_buy(symbol, rem_qty * price_hint, price_hint,
                                      intended_price=intended, order_type="market_fallback")
                result = self._combine_fills(result, mkt)
        return result

    def _live_open_buy(self, symbol: str, qty: float, quote_to_spend: float, limit_price: float,
                       price_hint: float, intended: float) -> Optional[dict[str, Any]]:
        """Live limit entry: rest the order, poll to the timeout, then cancel and
        market-fill any unfilled remainder. Partial fills are combined."""
        params: dict[str, Any] = {"timeInForce": "GTC"}
        if self.post_only:
            params["postOnly"] = True
        try:
            order = self.exchange.create_order(symbol, "limit", "buy", qty, limit_price, params)
        except Exception as exc:
            logger.warning("{} {} limit-buy rejected ({}); MARKET fallback.",
                           self.tag, symbol, str(exc).splitlines()[0][:80])
            return self.market_buy(symbol, quote_to_spend, price_hint,
                                   intended_price=intended, order_type="market_fallback")

        oid = order.get("id")
        deadline = time.time() + self.limit_timeout
        filled = float(order.get("filled") or 0.0)
        while filled < qty - 1e-12 and time.time() < deadline:
            time.sleep(self.limit_poll)
            try:
                order = self.exchange.fetch_order(oid, symbol)
            except Exception:
                break
            filled = float(order.get("filled") or filled)
            if (order.get("status") or "").lower() in ("closed", "filled"):
                break

        if filled < qty - 1e-12:                       # cancel whatever is still resting
            self.cancel(symbol, oid)

        result: Optional[dict[str, Any]] = None
        if filled > 0:
            cost = float(order.get("cost") or filled * limit_price)
            avg = float(order.get("average") or (cost / filled if filled else limit_price))
            fee = self._extract_fee(order)
            self._log("{} LIMIT BUY {} filled {:.6f}/{:.6f} @ {:.4f} (cost {:.2f}, fee {:.2f})",
                      self.tag, symbol, filled, qty, avg, cost, fee)
            result = self._attach_slippage(
                {"id": oid, "qty": filled, "price": avg, "cost": cost, "fee": fee},
                symbol, "buy", "limit", intended)

        remaining_qty = qty - filled
        if remaining_qty > 1e-9:
            remaining_quote = remaining_qty * price_hint
            if remaining_quote < self._min_notional(symbol):
                if result is None:
                    logger.info("{} {} entry abandoned (limit unfilled; remainder below min notional).",
                                self.tag, symbol)
            elif not self._chase_ok(intended, self._fresh_ask(symbol, price_hint)):
                pass  # breakout ran away past the chase tolerance - take the partial (or skip)
            else:
                logger.info("{} {} limit unfilled {:.6f}; MARKET fallback for remainder.",
                            self.tag, symbol, remaining_qty)
                mkt = self.market_buy(symbol, remaining_quote, price_hint,
                                      intended_price=intended, order_type="market_fallback")
                result = self._combine_fills(result, mkt)
        return result

    @staticmethod
    def _combine_fills(a: Optional[dict[str, Any]], b: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
        """Merge a limit leg + a market-fallback leg into one position fill
        (qty-weighted avg price; summed cost/fee). Per-leg slippage is already
        recorded, so the combined dict is not re-recorded."""
        if a is None:
            return b
        if b is None:
            return a
        qty = a["qty"] + b["qty"]
        cost = a.get("cost", a["qty"] * a["price"]) + b.get("cost", b["qty"] * b["price"])
        fee = a.get("fee", 0.0) + b.get("fee", 0.0)
        price = cost / qty if qty else a["price"]
        return {"id": a.get("id"), "qty": qty, "price": price, "cost": cost, "fee": fee,
                "order_type": "limit+market"}

    def place_stop_limit_sell(self, symbol: str, qty: float, stop_price: float,
                              limit_price: float) -> Optional[str]:
        qty = self.amount_prec(symbol, qty)
        stop_price = self.price_prec(symbol, stop_price)
        limit_price = self.price_prec(symbol, limit_price)
        if qty <= 0:
            return None

        if not self.place:
            self._seq += 1
            return f"sim-stop-{self._seq}"

        try:
            order = self.exchange.create_order(
                symbol, "limit", "sell", qty, limit_price,
                {"stopPrice": stop_price, "timeInForce": "GTC"})
            self._log("{} {} protective STOP-LIMIT id={} stop={:.4f}",
                      self.tag, symbol, order.get("id"), stop_price)
            return order.get("id")
        except Exception as exc:
            logger.warning("{} {} stop-limit not placed ({}); software stop protects.",
                           self.tag, symbol, str(exc).splitlines()[0][:100])
            return None

    def cancel(self, symbol: str, order_id: Optional[str]) -> None:
        if not order_id or not self.place:
            return
        try:
            self.exchange.cancel_order(order_id, symbol)
        except Exception as exc:
            logger.warning("Cancel of {} ({}) failed (may be gone): {}", order_id, symbol, exc)

    # ------------------------------------------------------------------ #
    @staticmethod
    def _extract_fee(order: dict[str, Any]) -> float:
        fee = order.get("fee") or {}
        if isinstance(fee, dict) and fee.get("cost") is not None:
            try:
                return float(fee["cost"])
            except (TypeError, ValueError):
                pass
        return 0.0
