"""
Tests for the ETF venue-adapter layer: the broker abstraction wiring (offline,
via a FakeBroker) and the factory/AlpacaBroker construction (offline, no network).
"""
from __future__ import annotations

from typing import Any, Optional

import pandas as pd
import pytest

from src.etf.brokers import build_broker
from src.etf.brokers.base import EtfBroker
from src.etf.data import EtfData
from src.etf.executor import EtfExecutor
from tests.conftest import etf_cfg


class FakeBroker(EtfBroker):
    venue = "fake"

    def __init__(self):
        self.calls: list[tuple] = []

    def daily_bars(self, symbol: str, lookback: int) -> pd.DataFrame:
        closes = [100.0 + i for i in range(80)]
        ts = pd.date_range("2024-01-01", periods=80, freq="D", tz="UTC")
        return pd.DataFrame({
            "timestamp": ts, "open": closes,
            "high": [c * 1.001 for c in closes], "low": [c * 0.999 for c in closes],
            "close": closes, "volume": [1_000_000.0] * 80,
        })

    def available_symbols(self, symbols: list[str]) -> list[str]:
        return list(symbols)

    def cash(self) -> float:
        return 1000.0

    def positions(self) -> dict[str, float]:
        return {"SPY": 5.0}

    def market_buy(self, symbol: str, notional_usd: float, price_hint: float) -> Optional[dict[str, Any]]:
        self.calls.append(("buy", symbol, notional_usd))
        qty = notional_usd / price_hint
        return {"id": "b1", "qty": qty, "price": price_hint, "cost": qty * price_hint, "fee": 0.0}

    def market_sell(self, symbol: str, qty: float, price_hint: float) -> Optional[dict[str, Any]]:
        self.calls.append(("sell", symbol, qty))
        return {"id": "s1", "qty": qty, "price": price_hint, "fee": 0.0}


def test_data_adds_indicators_from_broker_bars():
    data = EtfData(etf_cfg(), FakeBroker())
    frames = data.frames("SPY")
    df = frames["1d"]
    assert "atr" in df.columns and len(df) == 80
    assert data.last_price(frames) == df.iloc[-1]["close"]


def test_sim_executor_never_touches_broker():
    fb = FakeBroker()
    ex = EtfExecutor(etf_cfg(place=False), fb)
    fill = ex.market_buy("SPY", 100.0, 100.0)
    assert fill is not None and fill["id"].startswith("sim-")
    assert fb.calls == []                       # sim is fully internal


def test_live_executor_delegates_to_broker():
    fb = FakeBroker()
    ex = EtfExecutor(etf_cfg(place=True), fb)
    ex.market_buy("SPY", 100.0, 100.0)
    ex.market_sell("SPY", 2.0, 100.0, "rotation")
    assert ("buy", "SPY", 100.0) in fb.calls
    assert ("sell", "SPY", 2.0) in fb.calls


def test_buy_below_min_notional_is_skipped():
    ex = EtfExecutor(etf_cfg(place=False), FakeBroker())
    assert ex.market_buy("SPY", 5.0, 100.0) is None    # min_notional is $10


def test_factory_builds_alpaca_broker_offline():
    cfg = etf_cfg()
    cfg["etf_runtime"].update({"api_key": "dummy", "api_secret": "dummy"})
    broker = build_broker(cfg)                  # constructs alpaca-py clients, no network
    assert broker.venue == "alpaca"
    assert broker.is_market_open.__self__ is broker   # method bound (no crash on import)


def test_alpaca_broker_requires_keys():
    from src.etf.brokers.alpaca_broker import AlpacaBroker
    with pytest.raises(SystemExit):
        AlpacaBroker(etf_cfg())                 # api_key/secret are empty
