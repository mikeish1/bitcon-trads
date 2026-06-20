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
        # Some venues (Alpaca) cap how many bars a single request returns and
        # ignore `limit`, which starves higher timeframes of the ~200 bars
        # EMA-200 needs. Page forward from an explicit `since` until we have
        # enough history (or hit "now").
        tf_ms = self.exchange.parse_timeframe(timeframe) * 1000
        now_ms = self.exchange.milliseconds()
        since = now_ms - tf_ms * (limit + 10)
        rows: list[list] = []
        for _ in range(20):  # hard cap on pages, just in case
            batch = self.exchange.fetch_ohlcv(self.symbol, timeframe=timeframe,
                                              since=since, limit=1000)
            if not batch:
                break
            rows.extend(batch)
            nxt = batch[-1][0] + tf_ms
            if nxt <= since or batch[-1][0] >= now_ms or len(rows) >= limit + 10:
                break
            since = nxt

        df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df = df.drop_duplicates(subset="timestamp").sort_values("timestamp").reset_index(drop=True)
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        for col in ("open", "high", "low", "close", "volume"):
            df[col] = df[col].astype(float)
        return df.tail(limit).reset_index(drop=True)

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
        if not self.cfg["runtime"]["api_key"]:
            return {"USDT": 0.0, "BTC": 0.0}
        try:
            bal = self.exchange.fetch_balance()
            base, quote = self.symbol.split("/")  # e.g. BTC / USDT (or USD on Alpaca)
            # Keys are generic labels: "USDT" = quote cash, "BTC" = base coin.
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
    Build a ccxt client for the configured venue (Binance.US or Alpaca).
    Public market data works without API keys; keys are needed for balances and
    for placing orders.
    """
    runtime = cfg["runtime"]
    exchange_id = runtime.get("exchange_id", "binanceus")
    params: dict[str, Any] = {"enableRateLimit": True}
    if exchange_id != "alpaca":
        params["options"] = {"defaultType": "spot"}  # Binance.US spot
    if runtime["api_key"]:
        params["apiKey"] = runtime["api_key"]
        params["secret"] = runtime["api_secret"]

    try:
        exchange = getattr(ccxt, exchange_id)(params)
    except AttributeError:
        logger.warning("Unknown EXCHANGE_ID '{}'; falling back to binanceus.", exchange_id)
        exchange = ccxt.binanceus(params)
        exchange_id = "binanceus"

    if runtime["use_sandbox"]:
        try:
            exchange.set_sandbox_mode(True)  # Alpaca PAPER endpoint
            logger.info("Sandbox/PAPER endpoint enabled for '{}'.", exchange_id)
        except Exception as exc:
            logger.warning("Sandbox mode unavailable for {}: {}", exchange_id, exc)

    if runtime["real_money"]:
        logger.warning("REAL-MONEY trading on '{}' - live orders will be placed!", exchange_id)
    elif runtime["place_orders"]:
        logger.info("PAPER-BROKER mode on '{}' - orders go to the PAPER endpoint "
                    "(realistic fills, NO real money).", exchange_id)
    else:
        logger.info("SIMULATION mode on '{}' - orders are simulated internally.", exchange_id)

    try:
        exchange.load_markets()
    except Exception as exc:  # pragma: no cover - network
        logger.warning("Could not preload markets ({}); will retry on first call.", exc)
    return exchange
