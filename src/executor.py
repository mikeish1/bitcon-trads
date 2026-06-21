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

from typing import Any, Optional

import ccxt
from loguru import logger


class SpotExecutor:
    def __init__(self, cfg: dict[str, Any], exchange: ccxt.Exchange):
        self.cfg = cfg
        self.exchange = exchange
        self.place = cfg["runtime"]["place_orders"]
        self.real_money = cfg["runtime"]["real_money"]
        self.fee_pct = cfg["execution"]["taker_fee_pct"]
        self.slip = cfg["execution"]["paper_slippage_pct"]
        self.min_notional = cfg["risk"]["min_notional_usd"]
        self._seq = 0
        self.tag = "[LIVE]" if self.real_money else ("[PAPER-BROKER]" if self.place else "[SIM]")
        self._log = logger.warning if self.real_money else logger.info

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
    def market_buy(self, symbol: str, quote_to_spend: float, price_hint: float) -> Optional[dict[str, Any]]:
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
            return {"id": f"sim-buy-{self._seq}", "qty": qty, "price": entry, "cost": cost, "fee": fee}

        try:
            order = self.exchange.create_market_buy_order(symbol, qty)
            filled = float(order.get("filled") or qty)
            cost = float(order.get("cost") or (filled * price_hint))
            avg = float(order.get("average") or (cost / filled if filled else price_hint))
            fee = self._extract_fee(order)
            self._log("{} BUY {} filled {:.6f} @ {:.4f} (cost {:.2f}, fee {:.2f})",
                      self.tag, symbol, filled, avg, cost, fee)
            return {"id": order.get("id"), "qty": filled, "price": avg, "cost": cost, "fee": fee}
        except Exception as exc:
            logger.error("{} {} BUY failed: {}", self.tag, symbol, exc)
            return None

    def market_sell(self, symbol: str, qty: float, price_hint: float, reason: str) -> Optional[dict[str, Any]]:
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
            return {"id": f"sim-sell-{self._seq}", "qty": qty, "price": exitp,
                    "proceeds": proceeds, "fee": fee}

        try:
            order = self.exchange.create_market_sell_order(symbol, qty)
            filled = float(order.get("filled") or qty)
            proceeds = float(order.get("cost") or (filled * price_hint))
            avg = float(order.get("average") or (proceeds / filled if filled else price_hint))
            fee = self._extract_fee(order)
            self._log("{} SELL {} filled {:.6f} @ {:.4f} ({}) proceeds {:.2f} fee {:.2f}",
                      self.tag, symbol, filled, avg, reason, proceeds, fee)
            return {"id": order.get("id"), "qty": filled, "price": avg, "proceeds": proceeds, "fee": fee}
        except Exception as exc:
            logger.error("{} {} SELL failed: {}", self.tag, symbol, exc)
            return None

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
