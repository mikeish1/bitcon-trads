"""Feature D: composite scoring + min-momentum gate in MomentumRotation.

Verifies "simple" mode is unchanged (score == N-day ROC), the absolute-momentum
threshold filters weak names out of the candidate pool, raw components are
well-formed, and "composite" mode ranks the strongest trending name on top while
preserving the existing plan()/keep_band hysteresis contract."""
from __future__ import annotations

import pytest

from tests.conftest import make_bars

from src.momentum_allocator import MomentumRotation


def _cfg(scoring="simple", threshold=None):
    return {
        "market": {"primary_timeframe": "1d"},
        "strategy": {
            "donchian": {"entry_period": 40},
            "allocation": {"momentum_rotation": {
                "top_k": 4, "rebalance_days": 2, "lookback_days": 90, "keep_band": 1,
                "scoring": scoring, "min_momentum_threshold": threshold,
                "composite": {"weights": {"breakout": 0.30, "roc_long": 0.30,
                                          "roc_short": 0.15, "rel_btc": 0.15, "inv_vol": 0.10},
                              "roc_short_days": 20, "entry_period": 40, "normalize": "zscore"}}}},
    }


def _frames(rate, n=150, base=100.0):
    return {"1d": make_bars([base * (rate ** i) for i in range(n)])}


# Strong / mild uptrends and a downtrend, plus a flat BTC reference.
STRONG = _frames(1.010)
MILD = _frames(1.003)
WEAK = _frames(0.997)
BTC = _frames(1.001)


def test_simple_score_equals_n_day_roc():
    mr = MomentumRotation(_cfg("simple"))
    scores = mr.score_candidates({"STRONG": STRONG, "MILD": MILD})
    assert scores["STRONG"] == pytest.approx(mr.momentum(STRONG))
    assert scores["MILD"] == pytest.approx(mr.momentum(MILD))
    assert scores["STRONG"] > scores["MILD"]


def test_min_momentum_threshold_excludes_weak_names():
    mr = MomentumRotation(_cfg("simple", threshold=0.0))
    scores = mr.score_candidates({"STRONG": STRONG, "WEAK": WEAK})
    assert "STRONG" in scores
    assert "WEAK" not in scores            # negative long ROC -> filtered out


def test_raw_components_are_well_formed():
    mr = MomentumRotation(_cfg("composite"))
    comps = mr.raw_components(STRONG, BTC)
    assert set(comps) == {"breakout", "roc_long", "roc_short", "rel_btc", "inv_vol"}
    assert comps["roc_long"] == pytest.approx(mr.momentum(STRONG))
    # Strong asset clearly outpaces the slow BTC reference.
    assert comps["rel_btc"] > 0


def test_composite_ranks_strongest_on_top():
    mr = MomentumRotation(_cfg("composite"))
    scores = mr.score_candidates({"STRONG": STRONG, "MILD": MILD, "WEAK": WEAK}, BTC)
    assert set(scores) == {"STRONG", "MILD", "WEAK"}
    assert scores["STRONG"] == max(scores.values())


def test_composite_feeds_plan_topk_selection():
    mr = MomentumRotation(_cfg("composite"))
    scores = mr.score_candidates({"STRONG": STRONG, "MILD": MILD, "WEAK": WEAK}, BTC)
    plan = mr.plan(scores, held=[])
    assert "STRONG" in plan["target"]      # strongest is always selected first
    assert plan["rank"]["STRONG"] == 0


def test_insufficient_history_returns_no_score():
    mr = MomentumRotation(_cfg("simple"))
    short = {"1d": make_bars([100.0 + i for i in range(30)])}   # < lookback_days
    assert mr.score_candidates({"X": short}) == {}
