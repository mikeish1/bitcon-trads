"""Unit tests for the pure funding-series backtest simulator."""
from __future__ import annotations

from src.carry.backtester import run_series

_PPY = 8760.0 / 8.0   # 8h interval


def _stats(rate: float, n: int):
    return run_series([rate] * n, min_entry_apr=0.08, min_hold_apr=0.02,
                      flip_confirm_reads=3, expected_hold_days=30.0,
                      taker=0.0005, slip=0.0005, periods_per_year=_PPY)


def test_strong_positive_funding_deploys_and_profits():
    s = _stats(0.0002, 200)   # 2bp/8h -> ~21.9%/yr gross, well above entry
    assert s["n_trades"] >= 1
    assert s["pct_deployed"] > 0.5
    assert s["pnl_per_notional"] > 0.0


def test_negative_funding_never_opens():
    s = _stats(-0.0002, 200)
    assert s["n_trades"] == 0
    assert s["pnl_per_notional"] == 0.0


def test_thin_funding_below_net_entry_skips():
    # 0.6bp/8h -> ~6.6%/yr gross; net after ~4.9%/yr drag < 8% entry.
    s = _stats(0.00006, 200)
    assert s["n_trades"] == 0


def test_reports_fee_drag_and_horizon():
    s = _stats(0.0002, 100)
    assert s["fee_drag_apr"] > 0.0
    assert s["years"] > 0.0
