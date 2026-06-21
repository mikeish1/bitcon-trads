"""
Venue adapters for the ETF bot.

The selector, risk manager, and backtester are venue-agnostic; the **broker** is
the only thing that knows how to reach a venue. `build_broker` picks the right one
from `etf.venue`:

  * "alpaca"  -> AlpacaBroker  (alpaca-py; real US equities/ETFs, paper or live)
  * anything else -> CcxtBroker (ccxt; data/crypto-style fallback)
"""
from __future__ import annotations

from typing import Any

from .base import EtfBroker


def build_broker(cfg: dict[str, Any]) -> EtfBroker:
    venue = cfg["etf_runtime"]["venue"]
    if venue == "alpaca":
        from .alpaca_broker import AlpacaBroker
        return AlpacaBroker(cfg)
    from src.etf.config_etf import build_etf_exchange
    from .ccxt_broker import CcxtBroker
    return CcxtBroker(cfg, build_etf_exchange(cfg))


__all__ = ["EtfBroker", "build_broker"]
