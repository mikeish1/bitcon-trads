"""
Data pipeline (multi-asset spot).

Symbol-agnostic: every method takes the symbol it should act on, so one pipeline
instance serves the whole universe (BTC, ETH, SOL, ...). Works for Binance.US and
Alpaca via ccxt.

- Per-symbol candle fetch for the configured timeframe(s), with pagination so even
  venues that cap bars-per-request return enough history for EMA-200/Donchian.
- Indicators computed dynamically per asset (same code for every coin).
- Account balances read once and shared across assets.
- available_symbols() filters the configured universe to what the venue lists.

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
        self.primary_tf = cfg["market"]["primary_timeframe"]
        self.confirm_tfs = cfg["market"]["confirm_timeframes"]
        self.backfill = cfg["market"]["backfill_candles"]
        self.quote = cfg.get("quote_ccy", "USDT")

    @property
    def all_timeframes(self) -> list[str]:
        return [self.primary_tf, *self.confirm_tfs]

    # ------------------------------------------------------------------ #
    # Candles                                                            #
    # ------------------------------------------------------------------ #
    @retry(stop=stop_after_attempt(4), wait=wait_exponential(multiplier=1, max=20))
    def _fetch(self, symbol: str, timeframe: str, limit: int) -> pd.DataFrame:
        tf_ms = self.exchange.parse_timeframe(timeframe) * 1000
        now_ms = self.exchange.milliseconds()
        since = now_ms - tf_ms * (limit + 10)
        rows: list[list] = []
        for _ in range(20):
            batch = self.exchange.fetch_ohlcv(symbol, timeframe=timeframe, since=since, limit=1000)
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

    def get_frames(self, symbol: str) -> dict[str, pd.DataFrame]:
        """Return {timeframe: dataframe-with-indicators} for one symbol."""
        frames: dict[str, pd.DataFrame] = {}
        for tf in self.all_timeframes:
            frames[tf] = self.add_indicators(self._fetch(symbol, tf, self.backfill))
        return frames

    @staticmethod
    def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
        """Indicators used by the strategies + exits. Same for every asset."""
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

    @staticmethod
    def last_price(frames: dict[str, pd.DataFrame], primary_tf: str) -> float:
        return float(frames[primary_tf].iloc[-1]["close"])

    # ------------------------------------------------------------------ #
    # Closed-candle gating (no lookahead on the still-forming bar)        #
    # ------------------------------------------------------------------ #
    def is_bar_forming(self, df: pd.DataFrame, timeframe: str) -> bool:
        """True if the last row is the still-forming bar for `timeframe` (its close
        time is in the future). Acting on this bar is lookahead vs a close-based
        backtest. Best-effort: returns False if the timeframe can't be parsed."""
        if df is None or len(df) == 0 or "timestamp" not in df.columns:
            return False
        try:
            tf_ms = self.exchange.parse_timeframe(timeframe) * 1000
            now_ms = self.exchange.milliseconds()
        except Exception:
            return False
        last_open_ms = int(pd.Timestamp(df.iloc[-1]["timestamp"]).value // 1_000_000)
        return (last_open_ms + tf_ms) > now_ms

    def signal_frames(self, frames: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
        """Frames for DECISIONS: drop the still-forming primary bar so breakout +
        trail logic see only CONFIRMED closes (matches the validated backtest).
        Marking, sizing and the live exit-breach check keep the original (live) last
        bar. No-op when `market.signal_on_closed_candle` is off or the bar is closed.
        """
        if not bool(self.cfg["market"].get("signal_on_closed_candle", True)):
            return frames
        df = frames.get(self.primary_tf)
        if df is not None and len(df) > 1 and self.is_bar_forming(df, self.primary_tf):
            out = dict(frames)
            out[self.primary_tf] = df.iloc[:-1].reset_index(drop=True)
            return out
        return frames

    # ------------------------------------------------------------------ #
    # Account / universe                                                 #
    # ------------------------------------------------------------------ #
    def fetch_balances(self) -> dict[str, float]:
        """Return {asset_code: free_amount} for the whole account (empty if no keys)."""
        if not self.cfg["runtime"]["api_key"]:
            return {}
        try:
            bal = self.exchange.fetch_balance()
            free = bal.get("free", {}) or {}
            return {k: float(v or 0.0) for k, v in free.items()}
        except Exception as exc:
            logger.warning("Could not fetch balances ({}); using empty.", exc)
            return {}

    def quote_free(self, balances: dict[str, float]) -> float:
        return float(balances.get(self.quote, 0.0))

    @staticmethod
    def base_free(balances: dict[str, float], base: str) -> float:
        return float(balances.get(base, 0.0))

    def available_symbols(self, symbols: list[str]) -> list[str]:
        """Filter the configured universe to symbols this venue actually lists."""
        try:
            markets = self.exchange.markets or self.exchange.load_markets()
        except Exception as exc:
            logger.warning("Could not load markets ({}); assuming all symbols valid.", exc)
            return symbols
        avail, skipped = [], []
        for s in symbols:
            (avail if s in markets else skipped).append(s)
        if skipped:
            logger.warning("Skipping {} not listed on this venue: {}", len(skipped), ", ".join(skipped))
        return avail


# ---------------------------------------------------------------------- #
# Exchange factory                                                        #
# ---------------------------------------------------------------------- #
def build_exchange(cfg: dict[str, Any]) -> ccxt.Exchange:
    """Build a ccxt client for the configured venue (Binance.US or Alpaca)."""
    runtime = cfg["runtime"]
    exchange_id = runtime.get("exchange_id", "binanceus")
    params: dict[str, Any] = {"enableRateLimit": True}
    if exchange_id != "alpaca":
        params["options"] = {"defaultType": "spot"}
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
            exchange.set_sandbox_mode(True)
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
