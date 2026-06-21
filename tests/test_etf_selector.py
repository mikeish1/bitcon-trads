"""Tests for the reused ETF selector (Donchian filter + MomentumRotation)."""
from __future__ import annotations

from src.etf.selector import EtfMomentumSelector
from tests.conftest import etf_cfg, make_bars

_UP = [100.0 * (1.01 ** i) for i in range(150)]     # strong, persistent uptrend
_DOWN = [100.0 * (0.99 ** i) for i in range(150)]   # downtrend
_FLAT = [100.0] * 150                               # no trend


def _frames():
    return {
        "UP": {"1d": make_bars(_UP)},
        "DOWN": {"1d": make_bars(_DOWN)},
        "FLAT": {"1d": make_bars(_FLAT)},
    }


def test_only_uptrending_symbol_is_an_active_candidate():
    sel = EtfMomentumSelector(etf_cfg(top_k=1))
    cands = sel.candidates(_frames())
    assert "UP" in cands
    assert "DOWN" not in cands and "FLAT" not in cands


def test_plan_selects_the_single_strongest_when_top_k_1():
    sel = EtfMomentumSelector(etf_cfg(top_k=1))
    plan = sel.plan(_frames(), held=[])
    assert plan["target"] == {"UP"}
    assert plan["enter"] == ["UP"]


def test_rebalance_clock_passthrough():
    sel = EtfMomentumSelector(etf_cfg(rebalance_days=5))
    assert sel.is_due(None, "2024-03-01") is True          # never rotated -> due
    assert sel.is_due("2024-03-01", "2024-03-02") is False  # only 1 day later
    assert sel.is_due("2024-03-01", "2024-03-08") is True   # >= 5 days later


def test_held_leader_is_kept_not_churned():
    sel = EtfMomentumSelector(etf_cfg(top_k=1))
    plan = sel.plan(_frames(), held=["UP"])
    assert plan["exit"] == [] and plan["enter"] == []      # already holding the leader
