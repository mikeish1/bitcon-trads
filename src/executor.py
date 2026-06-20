"""
Order executor for Binance.US spot.

A thin, safe layer over ccxt for placing orders, with a faithful PAPER
simulation so the exact same code path runs whether or not real money is live.

Real orders are placed ONLY when runtime.really_live is True (i.e. both
PAPER_TRADING=false AND LIVE_TRADING_ENABLED=true). Otherwise every method
simulates and logs what it *would* have done.

Spot, long-only:
    market_buy(usdt)         -> buy BTC with USDT
    market_sell(qty)         -> sell BTC for USDT
    place_stop_limit_sell()  -> exchange-side protective stop (offline safety net)
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
        self.live = cfg["runtime"]["really_live"]
        self.fee_pct = cfg["execution"]["taker_fee_pct"]
        self.slip = cfg["execution"]["paper_slippage_pct"]
        self.min_notional = cfg["risk"]["min_notional_usd"]
        self._paper_order_seq = 0

    # ------------------------------------------------------------------ #
    # Precision helpers                                                  #
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
    # Market buy (spend USDT)                                            #
    # ------------------------------------------------------------------ #
    def market_buy(self, usdt_to_spend: float, price_hint: float) -> Optional[dict[str, Any]]:
        if usdt_to_spend < self._min_notional():
            logger.info("Buy skipped: ${:.2f} below min notional ${:.2f}.",
                        usdt_to_spend, self._min_notional())
            return None

        qty = self.amount_prec(usdt_to_spend / price_hint)
        if qty <= 0:
            logger.info("Buy skipped: computed quantity rounds to zero.")
            return None

        if not self.live:
            entry = price_hint * (1 + self.slip)
            cost = qty * entry
            fee = cost * self.fee_pct
            self._paper_order_seq += 1
            logger.info("[PAPER] BUY {:.6f} BTC @ ~{:.2f} (cost ${:.2f}, fee ${:.2f})",
                        qty, entry, cost, fee)
            return {"id": f"paper-buy-{self._paper_order_seq}", "qty": qty,
                    "price": entry, "cost": cost, "fee": fee}

        try:
            order = self.exchange.create_market_buy_order(self.symbol, qty)
            filled = float(order.get("filled") or qty)
            cost = float(order.get("cost") or (filled * price_hint))
            avg = float(order.get("average") or (cost / filled if filled else price_hint))
            fee = self._extract_fee(order, cost)
            logger.warning("[LIVE] BUY filled {:.6f} BTC @ {:.2f} (cost ${:.2f}, fee ${:.2f})",
                           filled, avg, cost, fee)
            return {"id": order.get("id"), "qty": filled, "price": avg, "cost": cost, "fee": fee}
        except Exception as exc:
            logger.error("[LIVE] BUY failed: {}", exc)
            return None

    # ------------------------------------------------------------------ #
    # Market sell (dump BTC)                                             #
    # ------------------------------------------------------------------ #
    def market_sell(self, qty: float, price_hint: float, reason: str) -> Optional[dict[str, Any]]:
        qty = self.amount_prec(qty)
        if qty <= 0:
            return None

        if not self.live:
            exitp = price_hint * (1 - self.slip)
            proceeds = qty * exitp
            fee = proceeds * self.fee_pct
            self._paper_order_seq += 1
            logger.info("[PAPER] SELL {:.6f} BTC @ ~{:.2f} ({}) proceeds ${:.2f} fee ${:.2f}",
                        qty, exitp, reason, proceeds, fee)
            return {"id": f"paper-sell-{self._paper_order_seq}", "qty": qty,
                    "price": exitp, "proceeds": proceeds, "fee": fee}

        try:
            order = self.exchange.create_market_sell_order(self.symbol, qty)
            filled = float(order.get("filled") or qty)
            proceeds = float(order.get("cost") or (filled * price_hint))
            avg = float(order.get("average") or (proceeds / filled if filled else price_hint))
            fee = self._extract_fee(order, proceeds)
            logger.warning("[LIVE] SELL filled {:.6f} BTC @ {:.2f} ({}) proceeds ${:.2f} fee ${:.2f}",
                           filled, avg, reason, proceeds, fee)
            return {"id": order.get("id"), "qty": filled, "price": avg,
                    "proceeds": proceeds, "fee": fee}
        except Exception as exc:
            logger.error("[LIVE] SELL failed: {}", exc)
            return None

    # ------------------------------------------------------------------ #
    # Exchange-side protective stop (offline safety net)                 #
    # ------------------------------------------------------------------ #
    def place_stop_limit_sell(self, qty: float, stop_price: float,
                              limit_price: float) -> Optional[str]:
        qty = self.amount_prec(qty)
        stop_price = self.price_prec(stop_price)
        limit_price = self.price_prec(limit_price)
        if qty <= 0:
            return None

        if not self.live:
            self._paper_order_seq += 1
            logger.info("[PAPER] protective STOP-LIMIT: sell {:.6f} if price<={:.2f} (limit {:.2f})",
                        qty, stop_price, limit_price)
            return f"paper-stop-{self._paper_order_seq}"

        try:
            order = self.exchange.create_order(
                self.symbol, "limit", "sell", qty, limit_price,
                {"stopPrice": stop_price, "timeInForce": "GTC"},
            )
            logger.warning("[LIVE] protective STOP-LIMIT placed id={} stop={:.2f} limit={:.2f}",
                           order.get("id"), stop_price, limit_price)
            return order.get("id")
        except Exception as exc:
            logger.error("[LIVE] stop-limit placement failed: {}", exc)
            return None

    def cancel(self, order_id: Optional[str]) -> None:
        if not order_id or not self.live:
            return
        try:
            self.exchange.cancel_order(order_id, self.symbol)
            logger.info("[LIVE] cancelled order {}", order_id)
        except Exception as exc:
            logger.warning("Cancel of {} failed (may already be gone): {}", order_id, exc)

    def is_order_open(self, order_id: Optional[str]) -> bool:
        """True if a live order is still resting (not filled/cancelled)."""
        if not order_id or not self.live:
            return True
        try:
            o = self.exchange.fetch_order(order_id, self.symbol)
            return o.get("status") == "open"
        except Exception:
            return True

    # ------------------------------------------------------------------ #
    @staticmethod
    def _extract_fee(order: dict[str, Any], notional: float) -> float:
        fee = order.get("fee") or {}
        if isinstance(fee, dict) and fee.get("cost") is not None:
            try:
                return float(fee["cost"])
            except (TypeError, ValueError):
                pass
        return 0.0  # caller may approximate; many spot fees are taken in BTC
