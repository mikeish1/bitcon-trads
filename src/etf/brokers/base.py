"""
EtfBroker — the single venue dependency for the ETF bot.

Everything else (selector, risk, backtester, loop) is venue-agnostic and talks to
this interface in terms of the **universe symbol** (e.g. "SPY"); each broker
translates to its venue's native symbol internally. Fills are returned as plain
dicts so the risk ledger stays decoupled from any SDK type:

  buy  -> {"id", "qty", "price", "cost", "fee"}
  sell -> {"id", "qty", "price", "fee"}
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Optional

import pandas as pd


class EtfBroker(ABC):
    venue: str = "?"

    # --- market data --------------------------------------------------- #
    @abstractmethod
    def daily_bars(self, symbol: str, lookback: int) -> pd.DataFrame:
        """Daily OHLCV with columns: timestamp, open, high, low, close, volume."""

    @abstractmethod
    def available_symbols(self, symbols: list[str]) -> list[str]:
        """Filter the universe to symbols this venue actually lists/trades."""

    def is_market_open(self) -> bool:
        """True if orders can fill now. 24/7 venues override to a constant True."""
        return True

    # --- account ------------------------------------------------------- #
    @abstractmethod
    def cash(self) -> float:
        """Free settlement-currency cash available to deploy."""

    @abstractmethod
    def positions(self) -> dict[str, float]:
        """Held quantity keyed by universe symbol (for live equity marking)."""

    # --- execution ----------------------------------------------------- #
    @abstractmethod
    def market_buy(self, symbol: str, notional_usd: float,
                   price_hint: float) -> Optional[dict[str, Any]]:
        """Buy ~notional_usd of the symbol. Returns a fill dict or None on failure."""

    @abstractmethod
    def market_sell(self, symbol: str, qty: float,
                    price_hint: float) -> Optional[dict[str, Any]]:
        """Sell qty of the symbol. Returns a fill dict or None on failure."""
