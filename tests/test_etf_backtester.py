"""End-to-end test of the pure ETF momentum backtest on synthetic bars."""
from __future__ import annotations

from src.etf.backtester import run_backtest
from src.etf.selector import EtfMomentumSelector
from tests.conftest import etf_cfg, make_bars

_UP = [100.0 * (1.01 ** i) for i in range(160)]
_DOWN = [100.0 * (0.99 ** i) for i in range(160)]
_FLAT = [100.0] * 160


def _panel():
    return {"UP": make_bars(_UP), "DOWN": make_bars(_DOWN), "FLAT": make_bars(_FLAT)}


def test_backtest_rides_the_uptrend_and_profits():
    sel = EtfMomentumSelector(etf_cfg(top_k=1, rebalance_days=5))
    stats = run_backtest(_panel(), sel, primary_tf="1d", start_after=30)
    assert stats["rebalances"] > 0
    assert stats["pct_deployed"] > 0.0
    assert stats["total_return"] > 0.0          # captured the trend
    assert stats["ending_holdings"] == ["UP"]   # ended holding the only leader


def test_backtest_stays_flat_when_nothing_trends():
    sel = EtfMomentumSelector(etf_cfg(top_k=1, rebalance_days=5))
    # All flat -> nothing is ever an active candidate -> never deployed.
    panel = {"AAA": make_bars(_FLAT), "BBB": make_bars(_FLAT)}
    stats = run_backtest(panel, sel, primary_tf="1d", start_after=30)
    assert stats["pct_deployed"] == 0.0
    assert stats["total_return"] == 0.0
