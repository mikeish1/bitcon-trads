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
from loguru import logger

from src.data_pipeline import DataPipeline
from .brokers.base import EtfBroker
from .data_quality import validate_bars


class EtfData:
    def __init__(self, cfg: dict[str, Any], broker: EtfBroker):
        self.cfg = cfg
        self.broker = broker
        self.tf = cfg["etf"]["primary_timeframe"]
        self.backfill = int(cfg["etf"]["backfill_days"])
        self.signal_on_closed = bool(cfg["etf"].get("signal_on_closed_candle", True))

    def frames(self, symbol: str) -> dict[str, pd.DataFrame]:
        """{primary_tf: dataframe-with-indicators} for one symbol (full history,
        including any still-forming session bar - that bar carries the live price).
        Raw bars are quality-checked first (bad ticks / dupes dropped, gaps logged)
        so the trend/momentum signal is never computed on corrupt data."""
        report = validate_bars(self.broker.daily_bars(symbol, self.backfill), symbol=symbol)
        if report.issues:
            level = "warning" if report.ok else "error"
            logger.log(level.upper(), "ETF data quality [{}]: {}", symbol,
                       "; ".join(report.issues))
        return {self.tf: DataPipeline.add_indicators(report.clean)}

    def closed_view(self, frames: dict[str, pd.DataFrame],
                    market_open: bool) -> dict[str, pd.DataFrame]:
        """Frames for SIGNAL decisions: drop the still-forming session bar when the
        market is open, so the selector/momentum decide on the last CONFIRMED close
        (matching the close-based backtest). When the market is closed the last bar is
        final, so it is kept (never decide a day late). Indicators at earlier bars are
        unaffected by the drop. Marking/sizing keep the live price (frames' last bar)."""
        if not self.signal_on_closed or not market_open:
            return frames
        return {tf: (df.iloc[:-1] if len(df) > 1 else df) for tf, df in frames.items()}

    def last_price(self, frames: dict[str, pd.DataFrame]) -> float:
        """Last daily close (sufficient for a daily strategy; no extra API call)."""
        return float(frames[self.tf].iloc[-1]["close"])

    def available_symbols(self, symbols: list[str]) -> list[str]:
        return self.broker.available_symbols(symbols)
