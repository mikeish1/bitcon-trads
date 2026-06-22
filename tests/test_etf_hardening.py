"""ETF sleeve hardening: confirmed-closed-candle signals, no phantom fills, reconcile.

  1. EtfData.closed_view drops the still-forming session bar when the market is open
     (signals match the close-based backtest) and keeps it when closed.
  2. AlpacaBroker._fill_from_order returns None when nothing filled, so a timed-out
     market order never records a phantom position.
  3. EtfRiskManager.reconcile closes a DB position the broker no longer holds.
"""
from __future__ import annotations

import pandas as pd
import pytest

from src.etf.brokers.alpaca_broker import AlpacaBroker
from src.etf.brokers.base import EtfBroker
from src.etf.data import EtfData
from src.etf.risk import EtfRiskManager
from tests.conftest import etf_cfg


class _StubBroker(EtfBroker):
    venue = "stub"

    def __init__(self, market_open=True, positions=None):
        self._open = market_open
        self._positions = positions or {}

    def daily_bars(self, symbol, lookback):
        ts = pd.date_range("2024-01-01", periods=5, freq="D", tz="UTC")
        closes = [100.0, 101.0, 102.0, 103.0, 104.0]
        return pd.DataFrame({"timestamp": ts, "open": closes, "high": closes,
                             "low": closes, "close": closes, "volume": [1e6] * 5})

    def available_symbols(self, symbols):
        return list(symbols)

    def is_market_open(self):
        return self._open

    def cash(self):
        return 1000.0

    def positions(self):
        return dict(self._positions)

    def market_buy(self, *a):
        return None

    def market_sell(self, *a):
        return None


# --- 1. confirmed-closed-candle view ------------------------------------------ #
def test_closed_view_drops_forming_bar_when_market_open():
    data = EtfData(etf_cfg(), _StubBroker(market_open=True))
    frames = data.frames("SPY")
    sig = data.closed_view(frames, market_open=True)
    assert len(sig["1d"]) == len(frames["1d"]) - 1          # forming bar dropped
    assert sig["1d"].iloc[-1]["close"] == 103.0             # decide on the prior close
    assert data.last_price(frames) == 104.0                 # marking keeps the live bar


def test_closed_view_keeps_final_bar_when_market_closed():
    data = EtfData(etf_cfg(), _StubBroker(market_open=False))
    frames = data.frames("SPY")
    sig = data.closed_view(frames, market_open=False)
    assert len(sig["1d"]) == len(frames["1d"])              # nothing dropped (final bar)
    assert sig["1d"].iloc[-1]["close"] == 104.0


# --- 2. no phantom fills ------------------------------------------------------ #
class _Order:
    def __init__(self, filled_qty, filled_avg_price=None, id="o1"):
        self.filled_qty = filled_qty
        self.filled_avg_price = filled_avg_price
        self.id = id


def test_fill_from_order_is_none_when_nothing_filled():
    assert AlpacaBroker._fill_from_order(_Order(0), 100.0, "buy") is None
    assert AlpacaBroker._fill_from_order(_Order(None), 100.0, "sell") is None


def test_fill_from_order_uses_actual_fill_not_the_hint():
    buy = AlpacaBroker._fill_from_order(_Order(3.0, 101.0), 100.0, "buy")
    assert buy["qty"] == 3.0 and buy["price"] == 101.0 and buy["cost"] == pytest.approx(303.0)
    sell = AlpacaBroker._fill_from_order(_Order(2.0, None), 100.0, "sell")
    assert sell["qty"] == 2.0 and sell["price"] == 100.0 and "cost" not in sell


# --- 3. reconcile ------------------------------------------------------------- #
def _open_spy(rm):
    rm.record_open("SPY", {"qty": 10.0, "price": 100.0, "cost": 1000.0, "fee": 0.0}, "entry")


def test_reconcile_closes_a_position_absent_from_broker():
    rm = EtfRiskManager(etf_cfg(place=True))                # uses_broker = True
    _open_spy(rm)
    rm.reconcile(broker_positions={}, prices={"SPY": 100.0})   # broker holds none
    assert rm.open_position("SPY") is None
    row = rm.conn.execute("SELECT status, reason FROM etf_positions WHERE symbol='SPY'").fetchone()
    assert row["status"] == "CLOSED" and "reconcile" in row["reason"]


def test_reconcile_keeps_a_position_still_held():
    rm = EtfRiskManager(etf_cfg(place=True))
    _open_spy(rm)
    rm.reconcile(broker_positions={"SPY": 10.0}, prices={"SPY": 100.0})
    assert rm.open_position("SPY") is not None


def test_reconcile_is_a_noop_in_sim():
    rm = EtfRiskManager(etf_cfg(place=False))               # uses_broker = False
    _open_spy(rm)
    rm.reconcile(broker_positions={}, prices={"SPY": 100.0})
    assert rm.open_position("SPY") is not None              # sim ledger is the truth
