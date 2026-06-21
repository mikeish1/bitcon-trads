"""
CcxtBroker — ccxt-backed adapter (data + crypto-style fallback).

Used for any non-Alpaca venue. ccxt's equity support is venue-dependent, so this
is primarily a data/fallback path; the supported live US-equities venue is Alpaca
(AlpacaBroker). Symbols are used as the venue lists them.
"""
from __future__ import annotations

from typing import Any, Optional

import ccxt
import pandas as pd
from loguru import logger
from tenacity import retry, stop_after_attempt, wait_exponential

from .base import EtfBroker


class CcxtBroker(EtfBroker):
    def __init__(self, cfg: dict[str, Any], exchange: ccxt.Exchange):
        self.cfg = cfg
        self.exchange = exchange
        self.venue = cfg["etf_runtime"]["venue"]
        self.tf = cfg["etf"]["primary_timeframe"]
        self.quote = cfg["etf_runtime"]["quote"]
        self.place = cfg["etf_runtime"]["place_orders"]

    @retry(stop=stop_after_attempt(4), wait=wait_exponential(multiplier=1, max=20))
    def daily_bars(self, symbol: str, lookback: int) -> pd.DataFrame:
        tf_ms = self.exchange.parse_timeframe(self.tf) * 1000
        since = self.exchange.milliseconds() - tf_ms * (lookback + 10)
        rows: list[list] = []
        for _ in range(20):
            batch = self.exchange.fetch_ohlcv(symbol, timeframe=self.tf, since=since, limit=1000)
            if not batch:
                break
            rows.extend(batch)
            nxt = batch[-1][0] + tf_ms
            if nxt <= since or len(rows) >= lookback + 10:
                break
            since = nxt
        df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df = df.drop_duplicates(subset="timestamp").sort_values("timestamp").reset_index(drop=True)
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        for col in ("open", "high", "low", "close", "volume"):
            df[col] = df[col].astype(float)
        return df.tail(lookback).reset_index(drop=True)

    def available_symbols(self, symbols: list[str]) -> list[str]:
        try:
            markets = self.exchange.markets or self.exchange.load_markets()
        except Exception as exc:
            logger.warning("Could not load {} markets ({}); assuming all valid.", self.venue, exc)
            return symbols
        avail = [s for s in symbols if s in markets]
        skipped = [s for s in symbols if s not in markets]
        if skipped:
            logger.warning("Skipping {} not listed on {}: {}",
                           len(skipped), self.venue, ", ".join(skipped))
        return avail

    def cash(self) -> float:
        try:
            return float((self.exchange.fetch_balance().get("free", {}) or {}).get(self.quote, 0.0))
        except Exception as exc:
            logger.warning("ccxt cash read failed: {}", exc)
            return 0.0

    def positions(self) -> dict[str, float]:
        # Best-effort: ccxt balances are keyed by asset code, not the ETF symbol.
        # The supported live-equities path is Alpaca; this is a fallback only.
        try:
            free = self.exchange.fetch_balance().get("free", {}) or {}
            return {k: float(v or 0.0) for k, v in free.items() if k != self.quote and v}
        except Exception as exc:
            logger.warning("ccxt positions read failed: {}", exc)
            return {}

    def market_buy(self, symbol: str, notional_usd: float,
                   price_hint: float) -> Optional[dict[str, Any]]:
        qty = notional_usd / price_hint if price_hint else 0.0
        try:
            qty = float(self.exchange.amount_to_precision(symbol, qty))
        except Exception:
            pass
        if qty <= 0:
            return None
        try:
            order = self.exchange.create_market_buy_order(symbol, qty)
            filled = float(order.get("filled") or qty)
            cost = float(order.get("cost") or filled * price_hint)
            avg = float(order.get("average") or (cost / filled if filled else price_hint))
            return {"id": order.get("id"), "qty": filled, "price": avg, "cost": cost, "fee": 0.0}
        except Exception as exc:
            logger.error("ccxt BUY {} failed: {}", symbol, exc)
            return None

    def market_sell(self, symbol: str, qty: float,
                    price_hint: float) -> Optional[dict[str, Any]]:
        try:
            qty = float(self.exchange.amount_to_precision(symbol, qty))
        except Exception:
            pass
        if qty <= 0:
            return None
        try:
            order = self.exchange.create_market_sell_order(symbol, qty)
            filled = float(order.get("filled") or qty)
            proceeds = float(order.get("cost") or filled * price_hint)
            avg = float(order.get("average") or (proceeds / filled if filled else price_hint))
            return {"id": order.get("id"), "qty": filled, "price": avg, "fee": 0.0}
        except Exception as exc:
            logger.error("ccxt SELL {} failed: {}", symbol, exc)
            return None
