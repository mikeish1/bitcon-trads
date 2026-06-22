"""Ops statistics: bootstrap CIs, MWU / Welch, z-score, and degradation flagging."""
from __future__ import annotations

import numpy as np
import pytest

from src import ops_stats as S


def test_daily_returns():
    r = S.daily_returns([100, 110, 121])
    assert len(r) == 2 and r[0] == pytest.approx(0.1)


def test_bootstrap_diff_ci_detects_adverse():
    rng = np.random.default_rng(1)
    live = rng.normal(-0.001, 0.008, 80)
    bt = rng.normal(0.003, 0.008, 80)
    d = S.bootstrap_diff_ci(live, bt, seed=2)
    assert d["point"] < 0 and d["adverse"] is True       # whole CI below 0


def test_mann_whitney_separates_distributions():
    rng = np.random.default_rng(3)
    a = rng.normal(0.0, 0.01, 60)
    b = rng.normal(0.02, 0.01, 60)
    assert S.mann_whitney_u(a, b)["p"] < 0.05
    # Same distribution -> not significant.
    c = rng.normal(0.0, 0.01, 60)
    d = rng.normal(0.0, 0.01, 60)
    assert S.mann_whitney_u(c, d)["p"] > 0.05


def test_welch_t_and_zscore():
    rng = np.random.default_rng(4)
    a = rng.normal(0.0, 0.01, 60)
    b = rng.normal(0.03, 0.01, 60)
    assert S.welch_t(a, b)["p"] < 0.05
    assert S.zscore_vs_distribution(1.0, [5.0, 5.1, 4.9, 5.0]) < -2   # far below


def test_flag_degradation_high_when_live_worse():
    rng = np.random.default_rng(5)
    live = rng.normal(-0.002, 0.008, 60)
    bt = rng.normal(0.003, 0.008, 60)
    res = S.flag_degradation(live, bt, {"calmar": 0.2}, {"calmar": [1.5, 1.4, 1.6, 1.5, 1.55]},
                             {"pvalue": 0.05, "dd_z": 2.0}, seed=6)
    assert res["severity"] in ("medium", "high")
    metrics = {f["metric"] for f in res["flags"]}
    assert "daily_return_distribution" in metrics or "calmar" in metrics


def test_flag_degradation_none_when_matched():
    rng = np.random.default_rng(7)
    live = rng.normal(0.002, 0.008, 60)
    bt = rng.normal(0.002, 0.008, 60)
    res = S.flag_degradation(live, bt, {"calmar": 1.5}, {"calmar": [1.5, 1.4, 1.6, 1.5]},
                             {"pvalue": 0.05, "dd_z": 2.0}, seed=8)
    assert res["severity"] == "none" and res["flags"] == []


def test_insufficient_data_is_safe():
    res = S.flag_degradation(np.array([0.01]), np.array([0.02]), {}, {},
                             {"pvalue": 0.05, "dd_z": 2.0})
    assert res["severity"] == "none" and res["flags"] == []
