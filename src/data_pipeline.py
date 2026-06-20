"""
Data pipeline: market data + indicators.

Responsibilities
----------------
1. Connect to Binance via ccxt (testnet for paper, mainnet for live).
2. Backfill historical 5-minute candles on startup.
3. Stream new candles - WebSocket preferred, automatic polling fallback.
4. Compute a rich set of technical indicators on the candle dataframe.

Everything here is read-only with respect to your account: it only fetches
market data. Order placement lives in main_loop / risk_manager.

A basic user does not need to change anything in this file.
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any, Optional

import ccxt
import pandas as pd
import ta
from loguru import logger
from tenacity import retry, stop_after_attempt, wait_exponential

# Binance pushes klines for these intervals on this public stream symbol format.
_WS_URL_MAINNET = "wss://stream.binance.com:9443/ws"
_WS_URL_TESTNET = "wss://stream.binancefuture.com/ws"  # futures testnet stream


class DataPipeline:
    """Fetches candles and computes indicators for the ensemble."""

    def __init__(self, cfg: dict[str, Any], exchange: ccxt.Exchange):
        self.cfg = cfg
        self.exchange = exchange
        self.symbol = cfg["market"]["symbol"]
        self.timeframe = cfg["market"]["timeframe"]
        self.backfill = cfg["market"]["backfill_candles"]
        self.use_ws = cfg["market"]["use_websocket"]

        self._df: pd.DataFrame = pd.DataFrame()
        self._lock = threading.Lock()
        self._ws_thread: Optional[threading.Thread] = None
        self._ws_alive = False

    # ------------------------------------------------------------------ #
    # Backfill                                                            #
    # ------------------------------------------------------------------ #
    @retry(stop=stop_after_attempt(5), wait=wait_exponential(multiplier=1, max=30))
    def backfill_history(self) -> pd.DataFrame:
        """Load historical candles so indicators have enough warm-up data."""
        logger.info(
            "Backfilling {} {} candles for {}", self.backfill, self.timeframe, self.symbol
        )
        raw = self.exchange.fetch_ohlcv(
            self.symbol, timeframe=self.timeframe, limit=self.backfill
        )
        df = self._to_dataframe(raw)
        with self._lock:
            self._df = df
        logger.info("Backfill complete: {} candles loaded.", len(df))
        return df

    # ------------------------------------------------------------------ #
    # Polling (always available)                                          #
    # ------------------------------------------------------------------ #
    @retry(stop=stop_after_attempt(4), wait=wait_exponential(multiplier=1, max=20))
    def poll_latest(self) -> pd.DataFrame:
        """Fetch the most recent candles and merge into the working dataframe."""
        raw = self.exchange.fetch_ohlcv(self.symbol, timeframe=self.timeframe, limit=5)
        new = self._to_dataframe(raw)
        with self._lock:
            self._df = self._merge(self._df, new)
            return self._df.copy()

    # ------------------------------------------------------------------ #
    # WebSocket (optional, with fallback)                                 #
    # ------------------------------------------------------------------ #
    def start_websocket(self) -> bool:
        """
        Start a background WebSocket listener for closed klines.
        Returns True if it started, False if unavailable (caller should poll).
        """
        if not self.use_ws:
            return False
        try:
            import websocket  # noqa: F401  (websocket-client)
        except Exception:
            logger.warning("websocket-client not installed; using polling instead.")
            return False

        try:
            self._ws_alive = True
            self._ws_thread = threading.Thread(target=self._ws_run, daemon=True)
            self._ws_thread.start()
            logger.info("WebSocket listener started (polling remains as a safety net).")
            return True
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Could not start WebSocket ({}); falling back to polling.", exc)
            self._ws_alive = False
            return False

    def _ws_run(self) -> None:  # pragma: no cover - network thread
        import websocket

        testnet = self.cfg["runtime"]["binance_testnet"]
        base = _WS_URL_TESTNET if testnet else _WS_URL_MAINNET
        stream_symbol = self.symbol.replace("/", "").lower()
        url = f"{base}/{stream_symbol}@kline_{self.timeframe}"

        def on_message(_ws, message: str) -> None:
            try:
                data = json.loads(message)
                k = data.get("k", {})
                if not k.get("x"):  # only act on CLOSED candles
                    return
                candle = [[
                    int(k["t"]),
                    float(k["o"]),
                    float(k["h"]),
                    float(k["l"]),
                    float(k["c"]),
                    float(k["v"]),
                ]]
                new = self._to_dataframe(candle)
                with self._lock:
                    self._df = self._merge(self._df, new)
                logger.debug("WS closed candle merged @ {}", k["c"])
            except Exception as exc:
                logger.warning("WS message parse error: {}", exc)

        def on_error(_ws, error) -> None:
            logger.warning("WebSocket error: {}", error)

        def on_close(_ws, *_args) -> None:
            logger.warning("WebSocket closed; polling will keep data fresh.")
            self._ws_alive = False

        while self._ws_alive:
            try:
                ws = websocket.WebSocketApp(
                    url, on_message=on_message, on_error=on_error, on_close=on_close
                )
                ws.run_forever(ping_interval=20, ping_timeout=10)
            except Exception as exc:
                logger.warning("WebSocket loop crashed ({}); retrying in 5s.", exc)
            time.sleep(5)

    def websocket_alive(self) -> bool:
        return self._ws_alive

    # ------------------------------------------------------------------ #
    # Indicators                                                          #
    # ------------------------------------------------------------------ #
    def get_dataframe_with_indicators(self) -> pd.DataFrame:
        """Return the current candles enriched with technical indicators."""
        with self._lock:
            df = self._df.copy()
        return self.add_indicators(df)

    @staticmethod
    def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
        """Compute the indicators the ensemble voters rely on."""
        if len(df) < 60:
            return df  # not enough warm-up data yet

        close, high, low, vol = df["close"], df["high"], df["low"], df["volume"]

        # Trend - EMAs across several lengths (parameter variation for voters).
        for span in (5, 8, 13, 21, 34, 55, 89, 100, 200):
            df[f"ema_{span}"] = ta.trend.ema_indicator(close, window=span)

        # Momentum
        for window in (7, 14, 21):
            df[f"rsi_{window}"] = ta.momentum.rsi(close, window=window)
        macd = ta.trend.MACD(close)
        df["macd"] = macd.macd()
        df["macd_signal"] = macd.macd_signal()
        df["macd_diff"] = macd.macd_diff()
        stoch = ta.momentum.StochasticOscillator(high, low, close)
        df["stoch_k"] = stoch.stoch()
        df["stoch_d"] = stoch.stoch_signal()
        for window in (5, 10, 20):
            df[f"roc_{window}"] = ta.momentum.roc(close, window=window)
        df["willr"] = ta.momentum.williams_r(high, low, close)
        df["cci"] = ta.trend.cci(high, low, close, window=20)

        # Volatility / bands
        bb = ta.volatility.BollingerBands(close, window=20, window_dev=2)
        df["bb_high"] = bb.bollinger_hband()
        df["bb_low"] = bb.bollinger_lband()
        df["bb_mid"] = bb.bollinger_mavg()
        df["atr"] = ta.volatility.average_true_range(high, low, close, window=14)

        # Trend strength
        adx = ta.trend.ADXIndicator(high, low, close, window=14)
        df["adx"] = adx.adx()
        df["adx_pos"] = adx.adx_pos()
        df["adx_neg"] = adx.adx_neg()

        # Volume confirmation
        df["obv"] = ta.volume.on_balance_volume(close, vol)
        df["obv_ema"] = ta.trend.ema_indicator(df["obv"], window=21)
        df["vol_ema"] = ta.trend.ema_indicator(vol, window=20)

        return df

    # ------------------------------------------------------------------ #
    # Helpers                                                             #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _to_dataframe(raw: list[list]) -> pd.DataFrame:
        df = pd.DataFrame(
            raw, columns=["timestamp", "open", "high", "low", "close", "volume"]
        )
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        for col in ("open", "high", "low", "close", "volume"):
            df[col] = df[col].astype(float)
        return df

    @staticmethod
    def _merge(old: pd.DataFrame, new: pd.DataFrame) -> pd.DataFrame:
        if old.empty:
            combined = new
        else:
            combined = pd.concat([old, new], ignore_index=True)
        combined = combined.drop_duplicates(subset="timestamp", keep="last")
        combined = combined.sort_values("timestamp").reset_index(drop=True)
        # Keep memory bounded.
        return combined.tail(1000).reset_index(drop=True)


# ---------------------------------------------------------------------- #
# Exchange factory                                                        #
# ---------------------------------------------------------------------- #
def build_exchange(cfg: dict[str, Any]) -> ccxt.Exchange:
    """
    Build a ccxt Binance USDT-perpetual client.

    - Paper trading -> Binance Futures TESTNET (set_sandbox_mode(True)).
    - Live trading  -> Binance mainnet.
    Public market data works even without API keys.
    """
    runtime = cfg["runtime"]
    params: dict[str, Any] = {
        "enableRateLimit": True,
        "options": {"defaultType": "future"},
    }
    if runtime["binance_api_key"]:
        params["apiKey"] = runtime["binance_api_key"]
        params["secret"] = runtime["binance_api_secret"]

    exchange = ccxt.binance(params)

    use_testnet = runtime["paper_trading"] or runtime["binance_testnet"]
    if use_testnet:
        exchange.set_sandbox_mode(True)
        logger.info("Exchange in SANDBOX/TESTNET mode (safe).")
    else:
        logger.warning("Exchange in LIVE MAINNET mode - real orders will be placed!")

    try:
        exchange.load_markets()
    except Exception as exc:  # pragma: no cover - network
        logger.warning("Could not preload markets ({}); will retry on first fetch.", exc)
    return exchange
