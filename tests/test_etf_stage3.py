"""
Stage-3 tests: selector factory, the sim=backtest parity guard (R3), the
split-aware reconcile drift flag (R8), and the PDT same-day guard (R10).
"""
from __future__ import annotations

from datetime import datetime, timezone

import numpy as np

from src.etf.data import EtfData
from src.etf.dual_momentum import DualMomentumSelector
from src.etf.risk import EtfRiskManager
from src.etf.selector import EtfMomentumSelector, build_selector
from tests.conftest import etf_cfg, make_bars


# --------------------------------------------------------------------------- #
# selector factory                                                            #
# --------------------------------------------------------------------------- #
def _dm_cfg(**kw):
    cfg = etf_cfg(**kw)
    cfg["etf"]["selection"]["mode"] = "dual_momentum"
    cfg["etf"]["dual_momentum"] = {
        "offensive": ["SPY", "EFA", "EEM"], "defensive": ["TLT", "IEF", "GLD", "BIL"],
        "abs_benchmark": "BIL", "lookback_days": 20, "top_k": 1,
        "rebalance_days": 20, "keep_band": 0, "min_history": 25,
    }
    return cfg


def test_build_selector_picks_mode():
    assert isinstance(build_selector(etf_cfg()), EtfMomentumSelector)          # default rotation
    assert isinstance(build_selector(_dm_cfg()), DualMomentumSelector)         # dual_momentum


# --------------------------------------------------------------------------- #
# R3: closed-candle live view == backtester point-in-time slice               #
# --------------------------------------------------------------------------- #
class _PanelBroker:
    """Returns pre-built per-symbol frames (no network) for EtfData wiring."""
    venue = "panel"

    def __init__(self, panel):
        self._panel = panel

    def daily_bars(self, symbol, lookback):
        return self._panel[symbol]


def _panel():
    n = 60
    # SPY rallies then rolls over (forces a risk-on -> risk-off flip mid-window);
    # defensive TLT trends up so risk-off has a clear hold.
    spy = list(np.linspace(100, 160, 35)) + list(np.linspace(160, 110, 25))
    return {
        "SPY": make_bars(spy),
        "EFA": make_bars(list(np.linspace(100, 120, n))),
        "EEM": make_bars(list(np.linspace(100, 95, n))),
        "TLT": make_bars(list(np.linspace(100, 130, n))),
        "IEF": make_bars(list(np.linspace(100, 108, n))),
        "GLD": make_bars(list(np.linspace(100, 112, n))),
        "BIL": make_bars(list(np.linspace(100, 100.6, n))),
    }


def test_closed_view_matches_backtest_slice_no_drift():
    """The live loop (full frames -> closed_view drops the forming bar) must feed the
    selector EXACTLY the closed-bar panel the backtester slices with df<=t. Same
    inputs -> identical plans, at every decision date. This is the golden-master that
    pins live==backtest and prevents dual-codepath drift."""
    cfg = _dm_cfg()
    panel = _panel()
    selector = DualMomentumSelector(cfg)
    data = EtfData(cfg, _PanelBroker(panel))
    tf = "1d"
    dates = list(panel["SPY"]["timestamp"])

    targets_seen = set()
    # Decide at several indices; need a forming bar at i+1 for the live path.
    for i in range(30, len(dates) - 1):
        t = dates[i]
        # Backtester path: point-in-time slice ending at the closed bar t.
        bt_frames = {s: {tf: df[df["timestamp"] <= t]} for s, df in panel.items()}
        # Live path: mirror the loop EXACTLY - per symbol, fetch frames that include
        # the still-forming bar at i+1, then closed_view drops it.
        live_view = {s: data.closed_view({tf: df[df["timestamp"] <= dates[i + 1]]},
                                          market_open=True)
                     for s, df in panel.items()}

        bt = selector.plan(bt_frames, held=[])
        live = selector.plan(live_view, held=[])
        assert bt["target"] == live["target"]
        assert bt["enter"] == live["enter"] and bt["exit"] == live["exit"]
        assert bt["regime"] == live["regime"]
        targets_seen.add(frozenset(bt["target"]))

    # The window genuinely rotates (>=2 distinct holdings), so parity is checked
    # across real enter/exit transitions, not a single constant plan.
    assert len(targets_seen) >= 2


# --------------------------------------------------------------------------- #
# R8: split-aware reconcile (flag drift, never auto-rewrite basis)            #
# --------------------------------------------------------------------------- #
def _open_spy(rm, qty=10.0):
    rm.record_open("SPY", {"qty": qty, "price": 100.0, "cost": qty * 100.0, "fee": 0.0}, "entry")


def test_reconcile_flags_qty_drift_without_closing():
    rm = EtfRiskManager(etf_cfg(place=True))
    _open_spy(rm, qty=10.0)
    notes = rm.reconcile(broker_positions={"SPY": 40.0}, prices={"SPY": 25.0})  # 4:1 split
    assert rm.open_position("SPY") is not None                  # NOT closed, basis untouched
    assert any("drift" in n and "SPY" in n for n in notes)


def test_reconcile_returns_close_note_when_position_gone():
    rm = EtfRiskManager(etf_cfg(place=True))
    _open_spy(rm)
    notes = rm.reconcile(broker_positions={}, prices={"SPY": 100.0})
    assert rm.open_position("SPY") is None
    assert any("closed" in n and "SPY" in n for n in notes)


def test_reconcile_no_note_when_in_line():
    rm = EtfRiskManager(etf_cfg(place=True))
    _open_spy(rm, qty=10.0)
    assert rm.reconcile(broker_positions={"SPY": 10.0}, prices={"SPY": 100.0}) == []


# --------------------------------------------------------------------------- #
# R10: PDT same-day guard helper                                              #
# --------------------------------------------------------------------------- #
def test_opened_today_detects_same_calendar_day():
    rm = EtfRiskManager(etf_cfg(place=False))
    _open_spy(rm)
    pos = rm.open_position("SPY")
    today = datetime.now(timezone.utc).date().isoformat()
    assert rm.opened_today(pos, today) is True
    assert rm.opened_today(pos, "2020-01-01") is False
