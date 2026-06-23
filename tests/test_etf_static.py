"""
Tests for the static fixed-weight allocation sleeve (Stage-4-validated design):
the pure allocator, the selector factory, the risk ledger's partial add/trim
(avg-cost, tax-aware), and the trade-based static simulator.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from src.etf.research.harness import CostModel, simulate_static
from src.etf.risk import EtfRiskManager
from src.etf.selector import build_selector
from src.etf.static_allocation import StaticAllocator, rebalance_deltas
from tests.conftest import etf_cfg


def test_rebalance_deltas_shared_decision():
    """The pure decision shared by the live loop and the backtest simulator (R3)."""
    w = {"SPY": 0.4, "AGG": 0.4, "GLD": 0.2}
    # On target -> no trades.
    on_target = {"SPY": 4000.0, "AGG": 4000.0, "GLD": 2000.0}
    assert rebalance_deltas(on_target, w, 10_000.0, band=0.05, min_notional=10.0) == {}
    # Drifted: SPY rallied to 5000, GLD fell to 1000 -> trim SPY, add GLD (AGG within band).
    drifted = {"SPY": 5000.0, "AGG": 4050.0, "GLD": 1000.0}
    d = rebalance_deltas(drifted, w, 10_050.0, band=0.05, min_notional=10.0)
    assert d["SPY"] < 0 and d["GLD"] > 0 and "AGG" not in d
    # A held symbol with no target weight -> full sell.
    d2 = rebalance_deltas({"QQQ": 1000.0}, w, 10_000.0, band=0.05, min_notional=10.0)
    assert d2["QQQ"] == -1000.0
    # Dust below min_notional -> skipped.
    assert rebalance_deltas({"SPY": 4005.0}, {"SPY": 0.4}, 10_000.0,
                            band=0.0, min_notional=10.0) == {}


def _sa_cfg(weights, rebalance_days=63, drift_band=0.05):
    return {"etf": {"primary_timeframe": "1d", "static_allocation": {
        "weights": weights, "rebalance_days": rebalance_days, "drift_band": drift_band}}}


def _bars(closes, opens=None, start="2020-01-01"):
    n = len(closes)
    opens = opens or closes
    ts = pd.date_range(start, periods=n, freq="D", tz="UTC")
    return pd.DataFrame({"timestamp": ts, "open": opens,
                         "high": [max(o, c) for o, c in zip(opens, closes)],
                         "low": [min(o, c) for o, c in zip(opens, closes)],
                         "close": closes, "volume": [1e6] * n})


# --- allocator -------------------------------------------------------------- #
def test_weights_normalized_to_one():
    a = StaticAllocator(_sa_cfg({"SPY": 2.0, "AGG": 2.0, "GLD": 1.0}))
    assert abs(sum(a.weights.values()) - 1.0) < 1e-9
    assert a.weights["SPY"] == 0.4 and a.weights["GLD"] == 0.2
    assert a.top_k == 3 and a.rebalance_days == 63


def test_plan_targets_weighted_symbols_with_data():
    a = StaticAllocator(_sa_cfg({"SPY": 0.4, "AGG": 0.4, "GLD": 0.2}))
    plan = a.plan({"SPY": {}, "AGG": {}, "GLD": {}}, held=["SPY"])
    assert plan["target"] == {"SPY", "AGG", "GLD"}
    assert set(plan["enter"]) == {"AGG", "GLD"} and plan["exit"] == []
    assert plan["weights"]["SPY"] == 0.4


def test_is_due_clock():
    a = StaticAllocator(_sa_cfg({"SPY": 1.0}, rebalance_days=63))
    assert a.is_due(None, "2020-01-01") is True
    assert a.is_due("2020-01-01", "2020-02-01") is False     # 31d < 63
    assert a.is_due("2020-01-01", "2020-04-01") is True      # 91d >= 63


def test_build_selector_static():
    cfg = etf_cfg()
    cfg["etf"]["selection"]["mode"] = "static_allocation"
    cfg["etf"]["static_allocation"] = {"weights": {"SPY": 0.6, "AGG": 0.4}}
    assert isinstance(build_selector(cfg), StaticAllocator)


# --- risk: partial add / trim (avg cost) ------------------------------------ #
def test_add_then_trim_avg_cost_and_realized():
    rm = EtfRiskManager(etf_cfg(place=False))            # sim ledger
    rm.record_open("SPY", {"qty": 10.0, "price": 100.0, "cost": 1000.0, "fee": 0.0}, "open")
    rm.add_to_position("SPY", {"qty": 5.0, "price": 120.0, "cost": 600.0, "fee": 0.0}, "add")
    pos = rm.open_position("SPY")
    assert pos["qty"] == 15.0 and abs(pos["entry_price"] - 1600.0 / 15.0) < 1e-6

    realized = rm.trim_position(pos, 5.0, 130.0)
    assert realized > 0                                  # sold above avg cost
    pos2 = rm.open_position("SPY")
    assert abs(pos2["qty"] - 10.0) < 1e-9                # still open, trimmed
    assert abs(float(rm.state_get("etf_realized_pnl")) - realized) < 1e-6


def test_trim_to_zero_closes_position():
    rm = EtfRiskManager(etf_cfg(place=False))
    rm.record_open("GLD", {"qty": 4.0, "price": 50.0, "cost": 200.0, "fee": 0.0}, "open")
    pos = rm.open_position("GLD")
    rm.trim_position(pos, 4.0, 55.0)
    assert rm.open_position("GLD") is None               # fully trimmed -> closed


# --- trade-based static simulator ------------------------------------------- #
def test_simulate_static_deploys_to_weights_low_turnover():
    n = 60
    panel = {
        "AAA": _bars(list(np.linspace(100, 130, n))),
        "BBB": _bars(list(np.linspace(100, 110, n))),
    }
    alloc = StaticAllocator(_sa_cfg({"AAA": 0.5, "BBB": 0.5}, rebalance_days=20, drift_band=0.05))
    res = simulate_static(panel, alloc, warmup=2, cost=CostModel(slippage_bps=0.0), initial=10_000.0)
    assert res.days == n and res.curve.iloc[-1] > res.initial    # both rose -> gains
    # low turnover: a 2-asset 50/50 rebalanced ~3x over 60 days trades only a handful
    assert len(res.realized) <= 6
