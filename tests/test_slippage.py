"""Slippage instrumentation: sign convention, persistence, and aggregation."""
from __future__ import annotations

import pytest

from src.slippage import SlippageRecorder, slippage_summary


def test_compute_sign_convention():
    # Buy filled above intended -> adverse (positive).
    bps, usd = SlippageRecorder.compute("buy", 100.0, 100.5, 10.0)
    assert bps == pytest.approx(50.0)
    assert usd == pytest.approx(5.0)
    # Sell filled below intended -> adverse (positive).
    bps, usd = SlippageRecorder.compute("sell", 100.0, 99.5, 10.0)
    assert bps == pytest.approx(50.0)
    assert usd == pytest.approx(5.0)
    # Buy filled below intended -> favorable (negative).
    bps, _ = SlippageRecorder.compute("buy", 100.0, 99.9, 10.0)
    assert bps == pytest.approx(-10.0)
    # Degenerate input is safe.
    assert SlippageRecorder.compute("buy", 0.0, 100.0, 1.0) == (0.0, 0.0)


def test_record_persists_and_summarizes(tmp_path):
    db = str(tmp_path / "fills.db")
    rec = SlippageRecorder(db, enabled=True, tolerance_bps=50)
    rec.record("BTC/USD", "buy", "limit", 100.0, 100.0, 10.0, fee_usd=1.0)     # 0 bps
    rec.record("BTC/USD", "buy", "market", 100.0, 100.7, 10.0, fee_usd=1.0)    # +70 bps
    rec.record("ETH/USD", "sell", "market", 50.0, 49.95, 20.0, fee_usd=0.5)    # +10 bps
    s = slippage_summary(db)
    assert s["fills"] == 3
    assert s["max_adverse_bps"] == pytest.approx(70.0, abs=0.1)
    # by_order_type breakdown present and limit cheaper than market.
    by_type = {r["order_type"]: r["avg_bps"] for r in s["by_order_type"]}
    assert by_type["limit"] < by_type["market"]
    # by_symbol present.
    syms = {r["symbol"] for r in s["by_symbol"]}
    assert syms == {"BTC/USD", "ETH/USD"}


def test_disabled_recorder_computes_but_does_not_store(tmp_path):
    db = str(tmp_path / "off.db")
    rec = SlippageRecorder(db, enabled=False)
    out = rec.record("BTC/USD", "buy", "market", 100.0, 100.5, 10.0)
    assert out["slippage_bps"] == pytest.approx(50.0)     # still computed
    assert rec.conn is None                               # nothing opened/stored
    assert slippage_summary(db) == {}                     # no table created
