"""
AlpacaBroker — US equities/ETFs via the official alpaca-py SDK.

Data (daily bars, asset list, clock) works with any Alpaca keys. Orders route to
the PAPER brokerage when `alpaca_paper` is true (real paper fills, no money) or to
the LIVE brokerage when false (gated by the two-key tripwire in config_etf). Alpaca
equities are commission-free and support fractional/notional market orders, which
is exactly what equal-weight 1/K sizing needs.

Requires `pip install -r requirements-etf.txt` (alpaca-py). Imported lazily so the
rest of the system has no hard dependency on it.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import pandas as pd
from loguru import logger

from .base import EtfBroker

_FILL_POLL_SECONDS = 1.0
_FILL_POLL_MAX = 12        # ~12s for a market order to fill before we give up


class AlpacaBroker(EtfBroker):
    venue = "alpaca"

    def __init__(self, cfg: dict[str, Any]):
        rt = cfg["etf_runtime"]
        if not rt["api_key"] or not rt["api_secret"]:
            raise SystemExit(
                "Alpaca keys required for venue=alpaca (data needs them too). "
                "Set ALPACA_API_KEY / ALPACA_API_SECRET (free paper keys work).")
        try:
            from alpaca.data.enums import DataFeed
            from alpaca.data.historical import StockHistoricalDataClient
            from alpaca.data.requests import StockBarsRequest
            from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
            from alpaca.trading.client import TradingClient
            from alpaca.trading.enums import AssetStatus, OrderSide, TimeInForce
            from alpaca.trading.requests import MarketOrderRequest
        except ImportError as exc:  # pragma: no cover - import guard
            raise SystemExit("alpaca-py not installed. Run: pip install -r requirements-etf.txt") from exc

        # stash the SDK symbols we need
        self._StockBarsRequest = StockBarsRequest
        self._MarketOrderRequest = MarketOrderRequest
        self._OrderSide, self._TimeInForce = OrderSide, TimeInForce
        self._AssetStatus = AssetStatus
        self._day = TimeFrame(1, TimeFrameUnit.Day)
        self._feed = DataFeed(str(cfg["etf"].get("alpaca_feed", "iex")))

        self._data = StockHistoricalDataClient(rt["api_key"], rt["api_secret"])
        # Trading client is used for assets/clock/account reads even in sim; orders
        # are only submitted when place_orders is true.
        self._trading = TradingClient(rt["api_key"], rt["api_secret"], paper=rt["alpaca_paper"])
        self._place = rt["place_orders"]
        logger.info("AlpacaBroker ready (paper={}, feed={}, orders={}).",
                    rt["alpaca_paper"], self._feed.value, self._place)

    # --- market data --------------------------------------------------- #
    def daily_bars(self, symbol: str, lookback: int) -> pd.DataFrame:
        # Request more calendar days than trading days to cover weekends/holidays.
        start = datetime.now(timezone.utc) - timedelta(days=int(lookback * 1.6) + 15)
        req = self._StockBarsRequest(symbol_or_symbols=symbol, timeframe=self._day,
                                     start=start, feed=self._feed)
        df = self._data.get_stock_bars(req).df
        if df is None or df.empty:
            return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
        df = df.reset_index()              # -> columns incl. symbol, timestamp, OHLCV
        out = df[["timestamp", "open", "high", "low", "close", "volume"]].copy()
        out["timestamp"] = pd.to_datetime(out["timestamp"], utc=True)
        for col in ("open", "high", "low", "close", "volume"):
            out[col] = out[col].astype(float)
        return out.tail(lookback).reset_index(drop=True)

    def available_symbols(self, symbols: list[str]) -> list[str]:
        ok, skipped = [], []
        for s in symbols:
            try:
                asset = self._trading.get_asset(s)
                if asset.tradable and asset.status == self._AssetStatus.ACTIVE:
                    ok.append(s)
                else:
                    skipped.append(s)
            except Exception:
                skipped.append(s)
        if skipped:
            logger.warning("Alpaca: skipping non-tradable/unknown symbols: {}", ", ".join(skipped))
        return ok

    def is_market_open(self) -> bool:
        try:
            return bool(self._trading.get_clock().is_open)
        except Exception as exc:
            logger.warning("Alpaca clock check failed ({}); treating market as CLOSED (safe).", exc)
            return False        # fail closed: don't trade when we can't confirm hours

    # --- account ------------------------------------------------------- #
    def cash(self) -> float:
        try:
            return float(self._trading.get_account().cash)
        except Exception as exc:
            logger.warning("Alpaca cash read failed: {}", exc)
            return 0.0

    def positions(self) -> dict[str, float]:
        try:
            return {p.symbol: float(p.qty) for p in self._trading.get_all_positions()}
        except Exception as exc:
            logger.warning("Alpaca positions read failed: {}", exc)
            return {}

    # --- execution ----------------------------------------------------- #
    def _await_fill(self, order_id: Any):
        """Poll briefly for a market order to fill (rebalance cadence tolerates it)."""
        order = None
        for _ in range(_FILL_POLL_MAX):
            order = self._trading.get_order_by_id(order_id)
            if str(order.status) in ("OrderStatus.FILLED", "filled") or order.filled_qty:
                if order.filled_avg_price:           # fully priced -> done
                    return order
            time.sleep(_FILL_POLL_SECONDS)
        return order

    def market_buy(self, symbol: str, notional_usd: float,
                   price_hint: float) -> Optional[dict[str, Any]]:
        if not self._place:
            logger.error("AlpacaBroker.market_buy called without order permission.")
            return None
        try:
            req = self._MarketOrderRequest(symbol=symbol, notional=round(notional_usd, 2),
                                           side=self._OrderSide.BUY,
                                           time_in_force=self._TimeInForce.DAY)
            order = self._await_fill(self._trading.submit_order(req).id)
            qty = float(order.filled_qty or 0.0) or (notional_usd / price_hint)
            price = float(order.filled_avg_price) if order.filled_avg_price else price_hint
            return {"id": str(order.id), "qty": qty, "price": price, "cost": qty * price, "fee": 0.0}
        except Exception as exc:
            logger.error("Alpaca BUY {} failed: {}", symbol, exc)
            return None

    def market_sell(self, symbol: str, qty: float,
                    price_hint: float) -> Optional[dict[str, Any]]:
        if not self._place:
            logger.error("AlpacaBroker.market_sell called without order permission.")
            return None
        try:
            req = self._MarketOrderRequest(symbol=symbol, qty=round(qty, 6),
                                           side=self._OrderSide.SELL,
                                           time_in_force=self._TimeInForce.DAY)
            order = self._await_fill(self._trading.submit_order(req).id)
            filled = float(order.filled_qty or 0.0) or qty
            price = float(order.filled_avg_price) if order.filled_avg_price else price_hint
            return {"id": str(order.id), "qty": filled, "price": price, "fee": 0.0}
        except Exception as exc:
            logger.error("Alpaca SELL {} failed: {}", symbol, exc)
            return None
