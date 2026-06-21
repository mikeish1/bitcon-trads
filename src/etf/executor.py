"""
ETF executor (long-only). Two tiers gated by the tripwire in config_etf:

  * sim  - internal paper fills with slippage/fee, no broker order (needs no broker)
  * live - delegates to the EtfBroker (Alpaca paper or live, or ccxt fallback)

Keeping the sim fills here (not in the broker) means paper runs need no venue
credentials and stay deterministic for tests.
"""
from __future__ import annotations

from typing import Any, Optional

from loguru import logger

from .brokers.base import EtfBroker


class EtfExecutor:
    def __init__(self, cfg: dict[str, Any], broker: Optional[EtfBroker] = None):
        self.cfg = cfg
        self.broker = broker
        rt = cfg["etf_runtime"]
        self.place = rt["place_orders"]
        self.real_money = rt["real_money"]
        ex = cfg["etf"]["execution"]
        self.fee_pct = float(ex["taker_fee_pct"])
        self.slip = float(ex["paper_slippage_pct"])
        self.min_notional = float(cfg["etf"]["capital"]["min_notional_usd"])
        self.tag = "[ETF-LIVE]" if self.real_money else ("[ETF-BROKER]" if self.place else "[ETF-SIM]")
        self._seq = 0

    def market_buy(self, symbol: str, quote_to_spend: float,
                   price_hint: float) -> Optional[dict[str, Any]]:
        if quote_to_spend < self.min_notional:
            return None
        if not self.place:
            price = price_hint * (1 + self.slip)
            qty = quote_to_spend / price
            self._seq += 1
            logger.info("{} BUY {} {:.4f} @ ~{:.2f} (${:.2f})", self.tag, symbol, qty, price, quote_to_spend)
            return {"id": f"sim-buy-{self._seq}", "qty": qty, "price": price,
                    "cost": qty * price, "fee": qty * price * self.fee_pct}
        fill = self.broker.market_buy(symbol, quote_to_spend, price_hint)
        if fill is not None:
            logger.info("{} BUY {} filled {:.4f} @ {:.2f} (${:.2f})",
                        self.tag, symbol, fill["qty"], fill["price"], fill["cost"])
        return fill

    def market_sell(self, symbol: str, qty: float, price_hint: float,
                    reason: str) -> Optional[dict[str, Any]]:
        if qty <= 0:
            return None
        if not self.place:
            price = price_hint * (1 - self.slip)
            self._seq += 1
            logger.info("{} SELL {} {:.4f} @ ~{:.2f} ({})", self.tag, symbol, qty, price, reason)
            return {"id": f"sim-sell-{self._seq}", "qty": qty, "price": price,
                    "fee": qty * price * self.fee_pct}
        fill = self.broker.market_sell(symbol, qty, price_hint)
        if fill is not None:
            logger.info("{} SELL {} filled {:.4f} @ {:.2f} ({})",
                        self.tag, symbol, fill["qty"], fill["price"], reason)
        return fill
