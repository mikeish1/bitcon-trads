"""
Order executor (Binance.US or Alpaca).

A thin, safe layer over ccxt. Three execution modes (decided in config):

  * SIMULATION  - internal paper fills, no API orders        (Binance.US paper)
  * PAPER-BROKER - real orders to a PAPER endpoint, no money  (Alpaca paper)
  * LIVE        - real orders with real money                 (two-key tripwire)

Spot, long-only:
    market_buy(usdt)         -> buy base coin with quote cash
    market_sell(qty)         -> sell base coin for quote cash
    place_stop_limit_sell()  -> exchange-side protective stop (best-effort)
    cancel(order_id)         -> cancel a resting order
"""
from __future__ import annotations

from typing import Any, Optional

import ccxt
from loguru import logger


class SpotExecutor:
    def __init__(self, cfg: dict[str, Any], exchange: ccxt.Exchange):
        self.cfg = cfg
        self.exchange = exchange
        self.symbol = cfg["market"]["symbol"]
        self.place = cfg["runtime"]["place_orders"]     # send real API orders?
        self.real_money = cfg["runtime"]["real_money"]  # real funds?
        self.fee_pct = cfg["execution"]["taker_fee_pct"]
        self.slip = cfg["execution"]["paper_slippage_pct"]
        self.min_notional = cfg["risk"]["min_notional_usd"]
        self._seq = 0
        self.tag = "[LIVE]" if self.real_money else ("[PAPER-BROKER]" if self.place else "[SIM]")
        self._log = logger.warning if self.real_money else logger.info

    # ------------------------------------------------------------------ #
    def amount_prec(self, qty: float) -> float:
        try:
            return float(self.exchange.amount_to_precision(self.symbol, qty))
        except Exception:
            return float(f"{qty:.6f}")

    def price_prec(self, price: float) -> float:
        try:
            return float(self.exchange.price_to_precision(self.symbol, price))
        except Exception:
            return float(f"{price:.2f}")

    def _min_notional(self) -> float:
        try:
            lim = self.exchange.market(self.symbol)["limits"]["cost"]["min"]
            if lim:
                return max(float(lim), self.min_notional)
        except Exception:
            pass
        return self.min_notional

    # ------------------------------------------------------------------ #
    def market_buy(self, quote_to_spend: float, price_hint: float) -> Optional[dict[str, Any]]:
        if quote_to_spend < self._min_notional():
            logger.info("Buy skipped: {:.2f} below min notional {:.2f}.",
                        quote_to_spend, self._min_notional())
            return None
        qty = self.amount_prec(quote_to_spend / price_hint)
        if qty <= 0:
            logger.info("Buy skipped: quantity rounds to zero.")
            return None

        if not self.place:
            entry = price_hint * (1 + self.slip)
            cost = qty * entry
            fee = cost * self.fee_pct
            self._seq += 1
            logger.info("{} BUY {:.6f} @ ~{:.2f} (cost {:.2f}, fee {:.2f})",
                        self.tag, qty, entry, cost, fee)
            return {"id": f"sim-buy-{self._seq}", "qty": qty, "price": entry,
                    "cost": cost, "fee": fee}

        try:
            order = self.exchange.create_market_buy_order(self.symbol, qty)
            filled = float(order.get("filled") or qty)
            cost = float(order.get("cost") or (filled * price_hint))
            avg = float(order.get("average") or (cost / filled if filled else price_hint))
            fee = self._extract_fee(order)
            self._log("{} BUY filled {:.6f} @ {:.2f} (cost {:.2f}, fee {:.2f})",
                      self.tag, filled, avg, cost, fee)
            return {"id": order.get("id"), "qty": filled, "price": avg, "cost": cost, "fee": fee}
        except Exception as exc:
            logger.error("{} BUY failed: {}", self.tag, exc)
            return None

    def market_sell(self, qty: float, price_hint: float, reason: str) -> Optional[dict[str, Any]]:
        qty = self.amount_prec(qty)
        if qty <= 0:
            return None

        if not self.place:
            exitp = price_hint * (1 - self.slip)
            proceeds = qty * exitp
            fee = proceeds * self.fee_pct
            self._seq += 1
            logger.info("{} SELL {:.6f} @ ~{:.2f} ({}) proceeds {:.2f} fee {:.2f}",
                        self.tag, qty, exitp, reason, proceeds, fee)
            return {"id": f"sim-sell-{self._seq}", "qty": qty, "price": exitp,
                    "proceeds": proceeds, "fee": fee}

        try:
            order = self.exchange.create_market_sell_order(self.symbol, qty)
            filled = float(order.get("filled") or qty)
            proceeds = float(order.get("cost") or (filled * price_hint))
            avg = float(order.get("average") or (proceeds / filled if filled else price_hint))
            fee = self._extract_fee(order)
            self._log("{} SELL filled {:.6f} @ {:.2f} ({}) proceeds {:.2f} fee {:.2f}",
                      self.tag, filled, avg, reason, proceeds, fee)
            return {"id": order.get("id"), "qty": filled, "price": avg,
                    "proceeds": proceeds, "fee": fee}
        except Exception as exc:
            logger.error("{} SELL failed: {}", self.tag, exc)
            return None

    def place_stop_limit_sell(self, qty: float, stop_price: float,
                              limit_price: float) -> Optional[str]:
        qty = self.amount_prec(qty)
        stop_price = self.price_prec(stop_price)
        limit_price = self.price_prec(limit_price)
        if qty <= 0:
            return None

        if not self.place:
            self._seq += 1
            logger.info("{} protective STOP-LIMIT: sell {:.6f} if<= {:.2f} (limit {:.2f})",
                        self.tag, qty, stop_price, limit_price)
            return f"sim-stop-{self._seq}"

        try:
            order = self.exchange.create_order(
                self.symbol, "limit", "sell", qty, limit_price,
                {"stopPrice": stop_price, "timeInForce": "GTC"})
            self._log("{} protective STOP-LIMIT placed id={} stop={:.2f}",
                      self.tag, order.get("id"), stop_price)
            return order.get("id")
        except Exception as exc:
            # Some venues (e.g. Alpaca crypto) reject stop orders - the in-loop
            # software stop still protects while the bot is running.
            logger.warning("{} stop-limit not placed ({}); relying on software stop.",
                           self.tag, str(exc).splitlines()[0][:120])
            return None

    def cancel(self, order_id: Optional[str]) -> None:
        if not order_id or not self.place:
            return
        try:
            self.exchange.cancel_order(order_id, self.symbol)
            logger.info("{} cancelled order {}", self.tag, order_id)
        except Exception as exc:
            logger.warning("Cancel of {} failed (may already be gone): {}", order_id, exc)

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
