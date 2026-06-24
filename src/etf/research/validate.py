"""
Stage-4 validation CLI (research only). Fetches long adjusted history, runs the
selected Dual Momentum design and the incumbent rotation (control-A) through the
realistic gap-aware simulator, and reports full-period + walk-forward IS/OOS +
regime + Monte-Carlo + parameter-sensitivity metrics against SPY B&H and 60/40.

    python -m src.etf.research.validate

Prints a structured block; the narrative verdict lives in
docs/equities_replatform/validation_report.md. Applies the binding decision rule
(plan.md G1-G7): if the design does not clearly beat buy-and-hold on risk-adjusted,
after-cost, after-tax OOS terms, say so plainly.
"""
from __future__ import annotations

from typing import Any, Optional

import pandas as pd

from src.data_pipeline import DataPipeline
from src.etf.dual_momentum import DualMomentumSelector
from src.etf.selector import EtfMomentumSelector
from .feed import fetch
from .harness import (CostModel, block_bootstrap, buy_hold_curve, metrics, simulate,
                      sixty_forty_curve, tax_drag_estimate, trade_stats)

OFFENSIVE = ["SPY", "EFA", "EEM"]
DEFENSIVE = ["TLT", "IEF", "GLD", "BIL"]
BENCH_BOND = "AGG"
UNIVERSE = OFFENSIVE + DEFENSIVE + [BENCH_BOND]

ANALYSIS_START = "2008-07-01"     # after 252d warmup (BIL inception 2007-05); GFC H2 in
SPLIT = "2017-01-01"              # IS before / OOS after
REGIMES = {
    "GFC 2008-09":   ("2008-07-01", "2009-06-30"),
    "COVID 2020":    ("2020-02-01", "2020-06-30"),
    "Bear 2022":     ("2022-01-01", "2022-12-31"),
    "Chop 2015-16":  ("2015-01-01", "2016-06-30"),
}


def _dm_cfg(lookback=252, top_k=1, benchmark="BIL", rebalance_days=20) -> dict[str, Any]:
    return {"etf": {"primary_timeframe": "1d", "dual_momentum": {
        "offensive": OFFENSIVE, "defensive": DEFENSIVE, "abs_benchmark": benchmark,
        "lookback_days": lookback, "top_k": top_k, "rebalance_days": rebalance_days,
        "keep_band": 0, "min_history": lookback + 8}}}


def _rotation_cfg(top_k=3, rebalance_days=20, lookback=90, entry=40) -> dict[str, Any]:
    return {"etf": {"primary_timeframe": "1d", "selection": {
        "entry_period": entry, "atr_trail_mult": 3.0, "min_history": 60, "top_k": top_k,
        "rebalance_days": rebalance_days, "lookback_days": lookback, "keep_band": 1}}}


def _indicator_panel(raw: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    return {s: DataPipeline.add_indicators(df) for s, df in raw.items()}


def _window(curve: pd.Series, start: str, end: Optional[str] = None) -> pd.Series:
    s = curve[curve.index >= pd.Timestamp(start, tz="UTC")]
    if end:
        s = s[s.index <= pd.Timestamp(end, tz="UTC")]
    return s


def _fmt(d: dict[str, Any]) -> str:
    return "  ".join(f"{k}={v}" for k, v in d.items())


def run() -> None:  # pragma: no cover - network + heavy
    raw = fetch(UNIVERSE)
    have = [s for s in UNIVERSE if s in raw]
    print(f"\n=== DATA ===\nfetched {have}")
    for s in have:
        print(f"  {s}: {len(raw[s])} bars {raw[s]['timestamp'].iloc[0].date()} -> {raw[s]['timestamp'].iloc[-1].date()}")
    panel = _indicator_panel({s: raw[s] for s in have})
    cost = CostModel(slippage_bps=5.0, commission_bps=0.0)

    # --- primary: configured Dual Momentum -------------------------------------
    dm = DualMomentumSelector(_dm_cfg())
    dm_res = simulate(panel, dm, warmup=260, cost=cost, initial=10_000.0, top_k=1)
    # control-A: incumbent rotation on the same universe
    rot = EtfMomentumSelector(_rotation_cfg())
    rot_res = simulate(panel, rot, warmup=100, cost=cost, initial=10_000.0, top_k=3)

    # benchmarks
    spy = buy_hold_curve(raw["SPY"], initial=10_000.0)
    six = sixty_forty_curve(raw["SPY"], raw[BENCH_BOND], initial=10_000.0)

    def report(label: str, curve: pd.Series, res=None):
        full = _window(curve, ANALYSIS_START)
        print(f"\n[{label}] FULL {ANALYSIS_START}+   {_fmt(metrics(full))}")
        print(f"   IS  {_fmt(metrics(_window(curve, ANALYSIS_START, SPLIT)))}")
        print(f"   OOS {_fmt(metrics(_window(curve, SPLIT)))}")
        if res is not None:
            print(f"   {_fmt(trade_stats(res))}")
            print(f"   tax {_fmt(tax_drag_estimate(res))}")

    print("\n=== FULL / WALK-FORWARD (net of 5bps/side slippage, fills at next open) ===")
    report("DUAL MOMENTUM", dm_res.curve, dm_res)
    report("CONTROL-A rotation", rot_res.curve, rot_res)
    report("SPY buy&hold", spy)
    report("60/40 SPY/AGG", six)

    print("\n=== REGIMES (return / maxDD) DM vs SPY ===")
    for name, (a, b) in REGIMES.items():
        dmw, spw = _window(dm_res.curve, a, b), _window(spy, a, b)
        m_dm, m_sp = metrics(dmw), metrics(spw)
        print(f"  {name:14s} DM ret={m_dm.get('total_return')} dd={m_dm.get('max_drawdown')}"
              f" | SPY ret={m_sp.get('total_return')} dd={m_sp.get('max_drawdown')}")

    print("\n=== MONTE-CARLO (block bootstrap, DM daily returns) ===")
    print(" ", _fmt(block_bootstrap(_window(dm_res.curve, ANALYSIS_START))))

    print("\n=== PARAMETER SENSITIVITY (DM, full period) ===")
    for lb in (126, 189, 252, 315):
        for tk in (1, 2):
            res = simulate(panel, DualMomentumSelector(_dm_cfg(lookback=lb, top_k=tk)),
                           warmup=lb + 8, cost=cost, initial=10_000.0, top_k=tk)
            m = metrics(_window(res.curve, ANALYSIS_START))
            print(f"  lookback={lb:3d} top_k={tk}  cagr={m.get('cagr')} maxdd={m.get('max_drawdown')}"
                  f" sharpe={m.get('sharpe')} calmar={m.get('calmar')}")
    for bench in ("BIL", ""):
        res = simulate(panel, DualMomentumSelector(_dm_cfg(benchmark=bench)),
                       warmup=260, cost=cost, initial=10_000.0, top_k=1)
        m = metrics(_window(res.curve, ANALYSIS_START))
        print(f"  abs_benchmark={bench or '0.0':3s}  cagr={m.get('cagr')} maxdd={m.get('max_drawdown')}"
              f" sharpe={m.get('sharpe')} calmar={m.get('calmar')}")


if __name__ == "__main__":
    run()
