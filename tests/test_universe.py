"""Universe-expansion gates: liquidity, correlation, portfolio stats, and the
end-to-end validate_universe_addition verdict (structure + each gate's veto)."""
from __future__ import annotations

import numpy as np
import pytest

from tests.conftest import make_bars

from src import universe as U


def _members():
    return {
        "BTC": make_bars([100.0 * (1.002 ** i) for i in range(250)]),
        "ETH": make_bars([100.0 * (1.0015 ** i) for i in range(250)]),
        "SOL": make_bars([100.0 * (1.0025 ** i) for i in range(250)]),
    }


def _random_walk(n=250, seed=11):
    rng = np.random.default_rng(seed)
    return make_bars(list(100.0 * np.exp(np.cumsum(rng.normal(0, 0.02, n)))))


def _cfg():
    return {
        "liquidity_filters": {"min_avg_daily_volume_usdt": 30e6, "volume_window": 30,
                              "max_pairwise_correlation": 0.90, "correlation_lookback": 180,
                              "min_vol_reduction_pct": 0.0, "min_calmar_improvement": 0.0,
                              "max_turnover_increase_pct": 0.50},
        "strategy": {"donchian": {"entry_period": 40, "atr_trail_mult": 3.0}},
        "execution": {"taker_fee_pct": 0.001, "paper_slippage_pct": 0.0007},
    }


def test_rolling_avg_dollar_volume():
    df = make_bars([100.0] * 60)               # volume 1e6, close 100 -> $100M/day
    assert U.rolling_avg_dollar_volume(df, 30) == pytest.approx(1e8, rel=1e-6)


def test_daily_returns_length():
    df = make_bars([100.0 + i for i in range(50)])
    assert len(U.daily_returns(df)) == 49


def test_correlation_detects_duplicate():
    members = _members()
    dup = make_bars([100.0 * (1.002 ** i) for i in range(250)])   # == BTC
    corr, who = U.max_correlation_with(dup, members, lookback=180)
    assert corr == pytest.approx(1.0, abs=1e-6)
    ok, c, _ = U.correlation_ok(dup, members, max_corr=0.90, lookback=180)
    assert ok is False


def test_portfolio_stats_keys_and_sign():
    eq = np.array([100.0 * (1.01 ** i) for i in range(60)])
    s = U.portfolio_stats(eq)
    assert set(s) == {"vol", "max_dd", "cagr", "calmar"}
    assert s["cagr"] > 0


def test_validate_rejects_illiquid_candidate():
    cand = make_bars([100.0 * (1.001 ** i) for i in range(250)])
    cand["volume"] = 1.0                       # ~$100/day -> far below the floor
    verdict = U.validate_universe_addition("LOWLIQ", cand, _members(), _cfg())
    assert verdict["gates"]["liquidity"] is False
    assert verdict["approved"] is False
    assert verdict["gates"]["diversification"] is False   # skipped after a failed gate


def test_validate_rejects_too_correlated_candidate():
    dup = make_bars([100.0 * (1.002 ** i) for i in range(250)])   # == BTC
    verdict = U.validate_universe_addition("DUP", dup, _members(), _cfg())
    assert verdict["gates"]["correlation"] is False
    assert verdict["approved"] is False


def test_validate_runs_full_pipeline_for_clean_candidate():
    verdict = U.validate_universe_addition("RW", _random_walk(), _members(), _cfg())
    # Liquidity + correlation should pass for an uncorrelated, liquid series, so the
    # diversification backtest runs and yields a concrete boolean verdict.
    assert verdict["gates"]["liquidity"] is True
    assert verdict["gates"]["correlation"] is True
    assert isinstance(verdict["gates"]["diversification"], bool)
    assert "diversification" in verdict["metrics"]
    assert isinstance(verdict["approved"], bool)
