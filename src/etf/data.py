"""
ETF data layer — thin wrapper over an EtfBroker plus the crypto bot's indicator
builder (DataPipeline.add_indicators), so the trend filter sees the same columns
(notably `atr`) it expects. Venue specifics live in the broker; this stays
venue-agnostic. The pure selector/backtester operate on injected frames, so tests
never need this class or a live broker.
"""
from __future__ import annotations

from typing import Any

import pandas as pd

from src.data_pipeline import DataPipeline
from .brokers.base import EtfBroker


class EtfData:
    def __init__(self, cfg: dict[str, Any], broker: EtfBroker):
        self.cfg = cfg
        self.broker = broker
        self.tf = cfg["etf"]["primary_timeframe"]
        self.backfill = int(cfg["etf"]["backfill_days"])

    def frames(self, symbol: str) -> dict[str, pd.DataFrame]:
        """{primary_tf: dataframe-with-indicators} for one symbol."""
        return {self.tf: DataPipeline.add_indicators(self.broker.daily_bars(symbol, self.backfill))}

    def last_price(self, frames: dict[str, pd.DataFrame]) -> float:
        """Last daily close (sufficient for a daily strategy; no extra API call)."""
        return float(frames[self.tf].iloc[-1]["close"])

    def available_symbols(self, symbols: list[str]) -> list[str]:
        return self.broker.available_symbols(symbols)
