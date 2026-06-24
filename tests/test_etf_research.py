"""
Unit tests for the Stage-4 validation harness (pure, offline; no network).

Covers the cost model, metrics, the gap-aware simulator (fills at the next OPEN,
whole-position rotation, realized-trade ledger), benchmarks, the tax-drag estimate,
and the bootstrap.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from src.etf.research.harness import (CostModel, block_bootstrap, buy_hold_curve,
                                       metrics, simulate, tax_drag_estimate, trade_stats)


def _bars(opens, closes, start="2020-01-01"):
    n = len(closes)
    ts = pd.date_range(start, periods=n, freq="D", tz="UTC")
    return pd.DataFrame({"timestamp": ts, "open": opens,
                         "high": [max(o, c) for o, c in zip(opens, closes)],
                         "low": [min(o, c) for o, c in zip(opens, closes)],
                         "close": closes, "volume": [1e6] * n})


class _SwitchSelector:
    """Targets `early` before `switch_iso`, then `late` (forces a rotation/sell)."""
    top_k = 1

    def __init__(self, early, late, switch_iso):
        self.early, self.late, self.switch = early, late, switch_iso

    def is_due(self, last, today):
        return True

    def plan(self, frames, held):
        t = next(iter(frames.values()))["1d"]["timestamp"].iloc[-1]
        sym = self.early if str(pd.Timestamp(t).date()) < self.switch else self.late
        return {"target": {sym}}


# --- cost model ------------------------------------------------------------- #
def test_cost_model_sides():
    c = CostModel(slippage_bps=10.0)
    assert c.buy_px(100.0) == 100.1 and c.sell_px(100.0) == 99.9
    assert c.commission(100.0) == 0.0


# --- metrics ---------------------------------------------------------------- #
def test_metrics_monotonic_up():
    curve = pd.Series(np.linspace(100, 200, 252), index=pd.date_range("2020-01-01", periods=252))
    m = metrics(curve)
    assert m["total_return"] > 0.9 and m["max_drawdown"] == 0.0 and m["sharpe"] > 0


def test_metrics_drawdown_detected():
    curve = pd.Series([100, 120, 60, 90], index=pd.date_range("2020-01-01", periods=4))
    m = metrics(curve)
    assert m["max_drawdown"] == 0.5         # 120 -> 60


# --- simulator -------------------------------------------------------------- #
def test_simulate_deploys_and_fills_at_next_open():
    # AAA: open 100, close 110 every day (a persistent gap up overnight).
    n = 20
    panel = {
        "AAA": _bars([100.0] * n, [110.0] * n),
        "BBB": _bars([50.0] * n, [50.0] * n),
    }
    sel = _SwitchSelector("AAA", "BBB", switch_iso="2020-01-15")
    res = simulate(panel, sel, warmup=2, cost=CostModel(slippage_bps=0.0),
                   initial=10_000.0, top_k=1)
    assert res.days == n and res.days_in_risk > 0 and res.turnover_notional > 0
    # It rotated AAA -> BBB, so AAA is a realized trade; entry filled at the OPEN
    # (100), not the close (110) -> proves next-open execution + gap modelling.
    aaa = [r for r in res.realized if r.symbol == "AAA"]
    fill_px = aaa[0].cost / aaa[0].qty           # per-share basis = the OPEN fill
    assert aaa and abs(fill_px - 100.0) < 1.0


def test_simulate_records_gain_on_rotation():
    # AAA rises while held, then we rotate out -> a positive realized trade.
    closes = list(np.linspace(100, 140, 30))
    panel = {"AAA": _bars(closes, closes), "BBB": _bars([50.0] * 30, [50.0] * 30)}
    sel = _SwitchSelector("AAA", "BBB", switch_iso="2020-01-25")
    res = simulate(panel, sel, warmup=2, cost=CostModel(slippage_bps=0.0), top_k=1)
    aaa = [r for r in res.realized if r.symbol == "AAA"]
    assert aaa and aaa[0].gain > 0
    ts = trade_stats(res)
    assert ts["trades"] >= 1 and 0.0 <= ts["time_in_risk"] <= 1.0


# --- benchmarks ------------------------------------------------------------- #
def test_buy_hold_curve_rebases():
    df = _bars(list(np.linspace(100, 200, 50)), list(np.linspace(100, 200, 50)))
    c = buy_hold_curve(df, initial=1000.0)
    assert abs(c.iloc[0] - 1000.0) < 1e-6 and c.iloc[-1] > c.iloc[0]


# --- tax drag --------------------------------------------------------------- #
def test_tax_drag_taxes_short_term_gain():
    closes = list(np.linspace(100, 140, 30))     # < 1y hold -> short-term
    panel = {"AAA": _bars(closes, closes), "BBB": _bars([50.0] * 30, [50.0] * 30)}
    sel = _SwitchSelector("AAA", "BBB", switch_iso="2020-01-25")
    res = simulate(panel, sel, warmup=2, cost=CostModel(slippage_bps=0.0), top_k=1)
    tax = tax_drag_estimate(res, st_rate=0.24, lt_rate=0.15)
    assert tax["st_gain"] > 0 and tax["est_tax"] > 0


# --- bootstrap -------------------------------------------------------------- #
def test_block_bootstrap_returns_percentiles():
    rng = np.random.default_rng(0)
    rets = 1 + rng.normal(0.0005, 0.01, 400).cumsum() * 0  # placeholder
    curve = pd.Series((1 + pd.Series(rng.normal(0.0005, 0.01, 400))).cumprod().values,
                      index=pd.date_range("2020-01-01", periods=400))
    out = block_bootstrap(curve, n=200, block=20, seed=1)
    assert {"cagr_p5", "cagr_p50", "cagr_p95", "maxdd_p95"} <= set(out)
    assert out["cagr_p5"] <= out["cagr_p95"]
