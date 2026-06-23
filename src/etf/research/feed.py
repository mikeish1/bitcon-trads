"""
Research data feed — long split+dividend-adjusted daily bars via yfinance, cached
to CSV so the harness is reproducible and runs offline after the first fetch.

RESEARCH ONLY. The live bot fetches from Alpaca (src/etf/brokers/alpaca_broker.py);
this is used solely by the Stage-4 validation harness. `auto_adjust=True` gives the
same split+dividend-adjusted basis the live path uses (Adjustment.ALL).
"""
from __future__ import annotations

import os
from typing import Optional

import pandas as pd

CACHE_DIR = os.path.join("backtests", "etf")


def fetch(symbols: list[str], *, start: str = "2007-01-01", end: Optional[str] = None,
          cache_dir: str = CACHE_DIR, refresh: bool = False) -> dict[str, pd.DataFrame]:
    """{symbol: adjusted daily OHLCV} (columns: timestamp, open, high, low, close,
    volume). Cached per symbol; pass refresh=True to re-download."""
    os.makedirs(cache_dir, exist_ok=True)
    out: dict[str, pd.DataFrame] = {}
    for sym in symbols:
        path = os.path.join(cache_dir, f"{sym}_etf_1d.csv")
        if os.path.exists(path) and not refresh:
            df = pd.read_csv(path)
            df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
            out[sym] = df
            continue
        df = _download(sym, start, end)
        if df is None or df.empty:
            print(f"{sym}: no data fetched")
            continue
        df.to_csv(path, index=False)
        out[sym] = df
    return out


def _download(sym: str, start: str, end: Optional[str]) -> Optional[pd.DataFrame]:
    import yfinance as yf
    raw = yf.download(sym, start=start, end=end, progress=False, auto_adjust=True)
    if raw is None or len(raw) == 0:
        return None
    if isinstance(raw.columns, pd.MultiIndex):       # single-ticker download still MI
        raw.columns = raw.columns.get_level_values(0)
    raw = raw.reset_index()
    return pd.DataFrame({
        "timestamp": pd.to_datetime(raw["Date"], utc=True),
        "open": raw["Open"].astype(float), "high": raw["High"].astype(float),
        "low": raw["Low"].astype(float), "close": raw["Close"].astype(float),
        "volume": raw["Volume"].astype(float),
    })
