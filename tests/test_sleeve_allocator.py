"""Thin sleeve allocator: weighting modes, regime overlay, bounds, turnover band,
and defensive handling of missing / equity-curve inputs."""
from __future__ import annotations

import pytest

from src.portfolio_sleeve_allocator import SleeveAllocator, metrics_from_equity


def _cfg(**over):
    sleeves = {"enabled": True, "members": ["donchian", "carry", "etf"],
               "allocator_mode": "risk_parity", "lookback_days": 60,
               "min_weight": 0.15, "max_weight": 0.60, "rebalance_threshold": 0.10,
               "momentum_metric": "sharpe", "momentum_tilt": 0.5,
               "regime_boost_factor": 0.20, "vol_floor": 0.001}
    sleeves.update(over)
    return {"portfolio": {"sleeves": sleeves}}


def _vol(donchian, carry, etf, sharpe=None):
    s = sharpe or {}
    return {"donchian": {"vol": donchian, "sharpe": s.get("donchian", 0.0)},
            "carry": {"vol": carry, "sharpe": s.get("carry", 0.0)},
            "etf": {"vol": etf, "sharpe": s.get("etf", 0.0)}}


def test_metrics_from_equity_basic():
    m = metrics_from_equity([100, 110, 120, 132], lookback_days=60)
    assert m["ret"] == pytest.approx(0.32)
    assert m["vol"] > 0 and m["n"] == 3
    short = metrics_from_equity([100, 110], lookback_days=60)
    assert short["vol"] != short["vol"]            # NaN -> unusable


def test_risk_parity_inverse_vol_with_clamp():
    w = SleeveAllocator(_cfg()).compute_weights(_vol(0.04, 0.01, 0.02))
    assert sum(w.values()) == pytest.approx(1.0)
    # inv-vol raw would give donchian 0.143 (< 0.15 floor) -> clamped up to 0.15.
    assert w["donchian"] == pytest.approx(0.15, abs=1e-6)
    assert w["carry"] > w["etf"] > w["donchian"]


def test_max_weight_clamp_binds():
    w = SleeveAllocator(_cfg()).compute_weights(_vol(0.10, 0.001, 0.05))
    assert w["carry"] == pytest.approx(0.60)        # huge inv-vol clamped to ceiling
    assert min(w.values()) >= 0.15 - 1e-9
    assert max(w.values()) <= 0.60 + 1e-9
    assert sum(w.values()) == pytest.approx(1.0)


def test_momentum_tilts_toward_higher_sharpe():
    perf = _vol(0.02, 0.02, 0.02, sharpe={"donchian": 2.0, "carry": 0.5, "etf": 1.0})
    w = SleeveAllocator(_cfg()).compute_weights(perf, mode="momentum_of_strategies")
    assert w["donchian"] > w["etf"] > w["carry"]
    assert sum(w.values()) == pytest.approx(1.0)


def test_regime_overlay_boosts_donchian():
    a = SleeveAllocator(_cfg())
    base = a.compute_weights(_vol(0.02, 0.02, 0.02))
    boosted = a.compute_weights(_vol(0.02, 0.02, 0.02), regime_state={"risk_on": True})
    assert boosted["donchian"] > base["donchian"]
    # risk-off does nothing.
    off = a.compute_weights(_vol(0.02, 0.02, 0.02), regime_state={"risk_on": False})
    assert off["donchian"] == pytest.approx(base["donchian"])


def test_rebalance_threshold_blocks_small_drift():
    a = SleeveAllocator(_cfg())
    prev = {"donchian": 0.34, "carry": 0.33, "etf": 0.33}
    # True weights are ~equal; drift from `prev` is < 10% band -> hold prev unchanged.
    held = a.compute_weights(_vol(0.02, 0.02, 0.02), prev_weights=prev)
    assert held == prev


def test_defensive_missing_and_equity_inputs():
    a = SleeveAllocator(_cfg())
    # Missing 'etf' -> split across the two present sleeves.
    w = a.compute_weights({"donchian": {"vol": 0.02}, "carry": {"vol": 0.04}})
    assert set(w) == {"donchian", "carry"} and sum(w.values()) == pytest.approx(1.0)
    # Empty -> equal weights over all configured members.
    eq = a.compute_weights({})
    assert eq == pytest.approx({"donchian": 1 / 3, "carry": 1 / 3, "etf": 1 / 3})
    # Equity-curve input is converted via metrics_from_equity.
    curve = list(range(100, 200))
    we = a.compute_weights({"donchian": {"equity": curve}, "carry": {"equity": curve},
                            "etf": {"equity": curve}})
    assert sum(we.values()) == pytest.approx(1.0)


def test_single_usable_sleeve_gets_full_weight():
    a = SleeveAllocator(_cfg())
    w = a.compute_weights({"donchian": {"vol": 0.03}, "carry": {"vol": None}, "etf": {}})
    assert w == {"donchian": pytest.approx(1.0)}
