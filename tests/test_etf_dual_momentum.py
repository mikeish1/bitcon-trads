"""
Unit tests for the dual-momentum selector (pure, offline).

Covers: relative ranking in risk-on, the absolute-momentum hurdle routing to the
defensive sleeve in risk-off, the configurable benchmark (incl. the 0.0 fallback),
top_k, the rebalance clock, and the insufficient-history path.
"""
from __future__ import annotations

import numpy as np

from src.etf.dual_momentum import DualMomentumSelector
from tests.conftest import make_bars


def _frames(closes):
    return {"1d": make_bars(list(closes))}


def _series(start: float, end: float, n: int = 40):
    return list(np.linspace(start, end, n))


def dm_cfg(*, top_k=1, lookback=20, rebalance_days=20, keep_band=0, benchmark="BIL",
           offensive=("SPY", "EFA", "EEM"), defensive=("TLT", "IEF", "GLD", "BIL")):
    return {"etf": {"primary_timeframe": "1d", "dual_momentum": {
        "offensive": list(offensive), "defensive": list(defensive),
        "abs_benchmark": benchmark, "lookback_days": lookback, "top_k": top_k,
        "rebalance_days": rebalance_days, "keep_band": keep_band}}}


def test_risk_on_holds_strongest_offensive():
    sel = DualMomentumSelector(dm_cfg(top_k=1))
    fbs = {
        "SPY": _frames(_series(100, 150)),   # strong up
        "EFA": _frames(_series(100, 110)),   # mild up
        "EEM": _frames(_series(150, 100)),   # down (excluded by abs filter)
        "TLT": _frames(_series(100, 100)),   # flat
        "IEF": _frames(_series(100, 100)),
        "GLD": _frames(_series(100, 100)),
        "BIL": _frames(_series(100, 100.5)),  # ~cash hurdle
    }
    plan = sel.plan(fbs, held=[])
    assert plan["regime"] == "risk_on"
    assert plan["target"] == {"SPY"}
    assert plan["enter"] == ["SPY"] and plan["exit"] == []


def test_risk_off_rotates_to_strongest_defensive():
    sel = DualMomentumSelector(dm_cfg(top_k=1))
    fbs = {
        "SPY": _frames(_series(150, 100)),   # all offense falling
        "EFA": _frames(_series(150, 120)),
        "EEM": _frames(_series(150, 100)),
        "TLT": _frames(_series(100, 120)),   # strongest defense
        "IEF": _frames(_series(100, 105)),
        "GLD": _frames(_series(100, 110)),
        "BIL": _frames(_series(100, 100.5)),
    }
    plan = sel.plan(fbs, held=["SPY"])       # was holding offense -> must rotate out
    assert plan["regime"] == "risk_off"
    assert plan["target"] == {"TLT"}
    assert "SPY" in plan["exit"] and plan["enter"] == ["TLT"]


def test_absolute_hurdle_blocks_weak_offense():
    # Offense is positive but below a high T-bill hurdle -> risk-off.
    sel = DualMomentumSelector(dm_cfg(top_k=1, benchmark="BIL"))
    fbs = {
        "SPY": _frames(_series(100, 108)),   # mildly positive
        "EFA": _frames(_series(100, 104)),
        "EEM": _frames(_series(100, 103)),
        "TLT": _frames(_series(100, 106)),
        "IEF": _frames(_series(100, 102)),
        "GLD": _frames(_series(100, 101)),
        "BIL": _frames(_series(100, 130)),   # engineered high hurdle
    }
    plan = sel.plan(fbs, held=[])
    assert plan["regime"] == "risk_off"
    assert plan["target"] and plan["target"].issubset({"TLT", "IEF", "GLD", "BIL"})


def test_zero_hurdle_when_no_benchmark():
    # benchmark "" -> hurdle 0.0; any positive-momentum offense qualifies.
    sel = DualMomentumSelector(dm_cfg(top_k=1, benchmark="", defensive=("TLT", "IEF", "GLD")))
    fbs = {
        "SPY": _frames(_series(100, 112)),
        "EFA": _frames(_series(100, 104)),
        "EEM": _frames(_series(100, 90)),    # negative -> excluded
        "TLT": _frames(_series(100, 100)),
        "IEF": _frames(_series(100, 100)),
        "GLD": _frames(_series(100, 100)),
    }
    plan = sel.plan(fbs, held=[])
    assert plan["regime"] == "risk_on" and plan["target"] == {"SPY"}


def test_top_k_two_holds_two_strongest_offensive():
    sel = DualMomentumSelector(dm_cfg(top_k=2))
    fbs = {
        "SPY": _frames(_series(100, 150)),
        "EFA": _frames(_series(100, 130)),
        "EEM": _frames(_series(100, 110)),
        "TLT": _frames(_series(100, 100)),
        "IEF": _frames(_series(100, 100)),
        "GLD": _frames(_series(100, 100)),
        "BIL": _frames(_series(100, 100.2)),
    }
    plan = sel.plan(fbs, held=[])
    assert plan["regime"] == "risk_on" and plan["target"] == {"SPY", "EFA"}


def test_is_due_delegates_to_clock():
    sel = DualMomentumSelector(dm_cfg(rebalance_days=20))
    assert sel.is_due(None, "2024-01-01") is True
    assert sel.is_due("2024-01-01", "2024-01-10") is False
    assert sel.is_due("2024-01-01", "2024-01-25") is True


def test_insufficient_history_yields_no_candidates():
    sel = DualMomentumSelector(dm_cfg(top_k=1, lookback=20))
    short = {s: _frames(_series(100, 101, n=10)) for s in ("SPY", "EFA", "EEM", "TLT", "IEF", "GLD", "BIL")}
    plan = sel.plan(short, held=[])
    assert plan["target"] == set() and plan["enter"] == []
