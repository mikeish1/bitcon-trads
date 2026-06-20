"""
Data pipeline for Binance.US spot.

- Builds a ccxt.binanceus client (spot).
- Fetches candles for multiple timeframes (5m primary + 15m/1h confirmation).
- Computes technical indicators on each timeframe.
- Reads real account balances (USDT / BTC) for sizing + reconciliation.

Read-only with respect to your account except for `fetch_balance`. Order
placement lives in executor.py.

A basic user does not need to change anything here.
"""
from __future__ import annotations

from typing import Any

import ccxt
import pandas as pd
import ta
from loguru import logger
from tenacity import retry, stop_after_attempt, wait_exponential


class DataPipeline:
    def __init__(self, cfg: dict[str, Any], exchange: ccxt.Exchange):
        self.cfg = cfg
        self.exchange = exchange
        self.symbol = cfg["market"]["symbol"]
        self.primary_tf = cfg["market"]["primary_timeframe"]
        self.confirm_tfs = cfg["market"]["confirm_timeframes"]
        self.backfill = cfg["market"]["backfill_candles"]

    @property
    def all_timeframes(self) -> list[str]:
        return [self.primary_tf, *self.confirm_tfs]

    # ------------------------------------------------------------------ #
    # Candles                                                            #
    # ------------------------------------------------------------------ #
    @retry(stop=stop_after_attempt(4), wait=wait_exponential(multiplier=1, max=20))
    def _fetch(self, timeframe: str, limit: int) -> pd.DataFrame:
        raw = self.exchange.fetch_ohlcv(self.symbol, timeframe=timeframe, limit=limit)
        df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        for col in ("open", "high", "low", "close", "volume"):
            df[col] = df[col].astype(float)
        return df

    def get_frames(self) -> dict[str, pd.DataFrame]:
        """Return {timeframe: dataframe-with-indicators} for every timeframe."""
        frames: dict[str, pd.DataFrame] = {}
        for tf in self.all_timeframes:
            df = self._fetch(tf, self.backfill)
            frames[tf] = self.add_indicators(df)
        return frames

    @staticmethod
    def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
        """Compute indicators used by the gates, triggers, vetoes and exits."""
        if len(df) < 60:
            return df
        close, high, low, vol = df["close"], df["high"], df["low"], df["volume"]

        for span in (21, 50, 200):
            df[f"ema_{span}"] = ta.trend.ema_indicator(close, window=span)
        df["rsi"] = ta.momentum.rsi(close, window=14)
        macd = ta.trend.MACD(close)
        df["macd_diff"] = macd.macd_diff()
        stoch = ta.momentum.StochasticOscillator(high, low, close)
        df["stoch_k"] = stoch.stoch()
        df["stoch_d"] = stoch.stoch_signal()
        df["roc_5"] = ta.momentum.roc(close, window=5)
        adx = ta.trend.ADXIndicator(high, low, close, window=14)
        df["adx"] = adx.adx()
        df["adx_pos"] = adx.adx_pos()
        df["adx_neg"] = adx.adx_neg()
        df["atr"] = ta.volatility.average_true_range(high, low, close, window=14)
        df["vol_ema"] = ta.trend.ema_indicator(vol, window=20)
        return df

    # ------------------------------------------------------------------ #
    # Account balances                                                  #
    # ------------------------------------------------------------------ #
    def fetch_balances(self) -> dict[str, float]:
        """
        Return {'USDT': free_usdt, 'BTC': free_btc}. In paper mode (or if keys
        are missing) returns zeros - sizing then falls back to default capital.
        """
        if not self.cfg["runtime"]["binance_api_key"]:
            return {"USDT": 0.0, "BTC": 0.0}
        try:
            bal = self.exchange.fetch_balance()
            base, quote = self.symbol.split("/")  # BTC, USDT
            return {
                "USDT": float(bal.get(quote, {}).get("free", 0.0) or 0.0),
                "BTC": float(bal.get(base, {}).get("free", 0.0) or 0.0),
            }
        except Exception as exc:
            logger.warning("Could not fetch balances ({}); using defaults.", exc)
            return {"USDT": 0.0, "BTC": 0.0}

    def last_price(self, frames: dict[str, pd.DataFrame]) -> float:
        return float(frames[self.primary_tf].iloc[-1]["close"])


# ---------------------------------------------------------------------- #
# Exchange factory                                                        #
# ---------------------------------------------------------------------- #
def build_exchange(cfg: dict[str, Any]) -> ccxt.Exchange:
    """
    Build a ccxt spot client (Binance.US by default).
    Public market data works without API keys; keys are needed for balances
    and (when really_live) order placement.
    """
    runtime = cfg["runtime"]
    exchange_id = runtime.get("exchange_id", "binanceus")
    params: dict[str, Any] = {"enableRateLimit": True, "options": {"defaultType": "spot"}}
    if runtime["binance_api_key"]:
        params["apiKey"] = runtime["binance_api_key"]
        params["secret"] = runtime["binance_api_secret"]

    try:
        exchange = getattr(ccxt, exchange_id)(params)
    except AttributeError:
        logger.warning("Unknown EXCHANGE_ID '{}'; falling back to binanceus.", exchange_id)
        exchange = ccxt.binanceus(params)

    if runtime["really_live"]:
        logger.warning("LIVE TRADING ENABLED on '{}' - REAL orders will be placed!", exchange_id)
    else:
        logger.info("PAPER mode on '{}' - orders are simulated, no real money at risk.", exchange_id)

    try:
        exchange.load_markets()
    except Exception as exc:  # pragma: no cover - network
        logger.warning("Could not preload markets ({}); will retry on first call.", exc)
    return exchange
