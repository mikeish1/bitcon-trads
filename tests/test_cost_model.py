"""Cost-aware preference: spread proxy, effective cost, filtering and penalty."""
from __future__ import annotations

import pytest

from tests.conftest import make_bars

from src import cost_model as C


def _cfg(mode="off", taker=0.001, factor=0.1, window=20, ceil=60.0, weight=1.0):
    return {"execution": {"taker_fee_pct": taker, "spread_proxy_factor": factor,
                          "spread_proxy_window": window, "cost_preference_mode": mode,
                          "max_effective_cost_bps": ceil, "cost_penalty_weight": weight}}


def test_spread_proxy_bps():
    # make_bars sets high=close*1.003, low=close*0.997 -> range/close = 0.6% = 60 bps.
    df = make_bars([100.0] * 40)
    assert C.spread_proxy_bps(df, 20) == pytest.approx(60.0, abs=0.5)


def test_effective_cost_is_fees_plus_scaled_spread():
    df = make_bars([100.0] * 40)
    cost = C.effective_cost_bps(df, _cfg(taker=0.001, factor=0.1))
    # 2*10bps taker + 0.1*60bps spread = 20 + 6 = 26 bps.
    assert cost == pytest.approx(26.0, abs=0.6)


def test_effective_cost_falls_back_to_fees_when_no_spread():
    import pandas as pd
    empty = pd.DataFrame({"high": [], "low": [], "close": []})
    assert C.effective_cost_bps(empty, _cfg(taker=0.001)) == pytest.approx(20.0)


def test_universe_costs_and_filter_and_penalty():
    tight = make_bars([100.0] * 40)                         # 60 bps range
    wide = make_bars([100.0] * 40)
    wide["high"] = wide["close"] * 1.05                     # 10% range -> ~1000 bps
    wide["low"] = wide["close"] * 0.95
    frames = {"BTC/USD": {"1d": tight}, "DOGE/USD": {"1d": wide}}
    costs = C.universe_costs(frames, _cfg(), "1d")
    assert costs["BTC/USD"] < costs["DOGE/USD"]
    # strict filter drops the dear one at a 50 bps ceiling.
    kept, dropped = C.filter_by_cost(costs, _cfg(ceil=50.0))
    assert "BTC/USD" in kept and "DOGE/USD" in dropped
    # soft penalty is larger for the dearer coin and non-negative.
    pen_btc = C.cost_penalty("BTC/USD", costs, _cfg(weight=1.0))
    pen_doge = C.cost_penalty("DOGE/USD", costs, _cfg(weight=1.0))
    assert 0.0 <= pen_btc < pen_doge


def test_mode_normalization():
    assert C.cost_preference_mode(_cfg("STRICT")) == "strict"
    assert C.cost_preference_mode(_cfg("bogus")) == "off"


# ----------------- Real fees + live spreads --------------------------------- #
class StubExchange:
    def __init__(self, taker=0.0008, spread_bps=4.0, fail_ticker=False):
        self.taker = taker
        self.spread_bps = spread_bps
        self.fail_ticker = fail_ticker

    def market(self, symbol):
        return {"taker": self.taker, "maker": self.taker / 2}

    def fetch_ticker(self, symbol):
        if self.fail_ticker:
            raise RuntimeError("no quote")
        mid = 100.0
        half = mid * (self.spread_bps / 1e4) / 2
        return {"bid": mid - half, "ask": mid + half, "last": mid}


def test_symbol_fee_bps_precedence():
    cfg = _cfg(taker=0.001)
    ex = StubExchange(taker=0.0008)
    assert C.symbol_fee_bps("BTC/USD", ex, cfg) == pytest.approx(8.0)        # venue tier
    assert C.symbol_fee_bps("BTC/USD", None, cfg) == pytest.approx(10.0)     # config fallback
    cfg["execution"]["fee_overrides"] = {"BTC": {"taker": 0.0005}}
    assert C.symbol_fee_bps("BTC/USD", ex, cfg) == pytest.approx(5.0)        # override wins


def test_symbol_spread_bps_from_ticker():
    assert C.symbol_spread_bps("BTC/USD", StubExchange(spread_bps=4.0)) == pytest.approx(4.0, abs=0.05)
    assert C.symbol_spread_bps("BTC/USD", None) != C.symbol_spread_bps("BTC/USD", None)  # NaN
    assert C.symbol_spread_bps("BTC/USD", StubExchange(fail_ticker=True)) != \
        C.symbol_spread_bps("BTC/USD", StubExchange(fail_ticker=True))                   # NaN on failure


def test_effective_cost_with_real_inputs():
    df = make_bars([100.0] * 40)
    # real: 2*8 bps fee + 4 bps spread = 20 bps (proxy ignored).
    assert C.effective_cost_bps(df, _cfg(), fee_bps=8.0, spread_bps=4.0) == pytest.approx(20.0)


def test_live_costs_real_then_proxy_fallback():
    df = make_bars([100.0] * 40)
    frames = {"BTC/USD": {"1d": df}}
    real = C.live_costs(frames, StubExchange(taker=0.0008, spread_bps=4.0), _cfg(), "1d")
    assert real["BTC/USD"] == pytest.approx(20.0)                 # 16 fee + 4 spread
    # Ticker fails -> spread proxy fallback (still uses real venue fee).
    fb = C.live_costs(frames, StubExchange(taker=0.0008, fail_ticker=True), _cfg(factor=0.1), "1d")
    assert fb["BTC/USD"] == pytest.approx(16.0 + 0.1 * 60.0, abs=0.6)
