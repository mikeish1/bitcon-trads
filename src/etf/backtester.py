"""
ETF momentum backtester (research only).

Pure walk-forward over a panel of daily bars: every rebalance the selector picks
the strongest-K eligible ETFs (active Donchian trend), we equal-weight them and
mark to market close-to-close until the next rebalance. Reports total return,
max drawdown, an annualised Sharpe, and deployment.

`run_backtest` is pure (operates on injected frames) and unit-tested offline. The
CLI pulls real bars via ccxt. Assumes a shared daily date grid across symbols
(true for liquid US ETFs).
"""
from __future__ import annotations

import argparse
import math
import statistics
from typing import Any, Optional

import pandas as pd

from .selector import EtfMomentumSelector


def _close_on_or_before(df: pd.DataFrame, t: Any) -> Optional[float]:
    sub = df[df["timestamp"] <= t]
    if sub.empty:
        return None
    return float(sub.iloc[-1]["close"])


def run_backtest(panel: dict[str, pd.DataFrame], selector: EtfMomentumSelector, *,
                 primary_tf: str, start_after: int = 60, ann_factor: int = 252) -> dict[str, Any]:
    dates = sorted(set().union(*[set(df["timestamp"]) for df in panel.values()]))
    held: list[str] = []
    weights: dict[str, float] = {}
    equity = 1.0
    curve: list[float] = []
    daily_rets: list[float] = []
    last_rebal: Optional[str] = None
    deployed = 0
    rebalances = 0

    for i, t in enumerate(dates):
        if i > 0 and held:
            ret = 0.0
            for sym in held:
                pp = _close_on_or_before(panel[sym], dates[i - 1])
                pn = _close_on_or_before(panel[sym], t)
                if pp and pn and pp > 0:
                    ret += weights.get(sym, 0.0) * (pn / pp - 1.0)
            equity *= (1.0 + ret)
            daily_rets.append(ret)
            deployed += 1
        else:
            daily_rets.append(0.0)
        curve.append(equity)

        if i < start_after:
            continue
        today = pd.Timestamp(t).date().isoformat()
        if selector.is_due(last_rebal, today):
            frames = {sym: {primary_tf: df[df["timestamp"] <= t]} for sym, df in panel.items()}
            plan = selector.plan(frames, held)
            held = sorted(plan["target"])
            weights = {s: 1.0 / len(held) for s in held} if held else {}
            last_rebal = today
            rebalances += 1

    peak, maxdd = -1.0, 0.0
    for v in curve:
        peak = max(peak, v)
        if peak > 0:
            maxdd = max(maxdd, (peak - v) / peak)
    mean = sum(daily_rets) / len(daily_rets) if daily_rets else 0.0
    sd = statistics.pstdev(daily_rets) if len(daily_rets) > 1 else 0.0
    sharpe = (mean / sd * math.sqrt(ann_factor)) if sd > 0 else 0.0
    n = len(dates)
    return {
        "days": n,
        "total_return": round(equity - 1.0, 4),
        "final_equity": round(equity, 4),
        "max_drawdown": round(maxdd, 4),
        "sharpe": round(sharpe, 2),
        "rebalances": rebalances,
        "pct_deployed": round(deployed / max(n - 1, 1), 3),
        "ending_holdings": held,
    }


def main() -> None:  # pragma: no cover - network CLI
    from .brokers import build_broker
    from .config_etf import load_etf_config
    from .data import EtfData

    p = argparse.ArgumentParser(description="ETF momentum backtest on real bars.")
    p.add_argument("--universe", default="")
    args = p.parse_args()

    cfg = load_etf_config()
    if args.universe:
        cfg["etf"]["universe"] = [s.strip().upper() for s in args.universe.split(",") if s.strip()]
    selector = EtfMomentumSelector(cfg)
    data = EtfData(cfg, build_broker(cfg))
    tf = cfg["etf"]["primary_timeframe"]
    panel: dict[str, pd.DataFrame] = {}
    for sym in cfg["etf"]["universe"]:
        try:
            panel[sym] = data.frames(sym)[tf]
        except Exception as exc:
            print(f"{sym}: fetch failed ({exc})")
    if not panel:
        print("No data fetched.")
        return
    stats = run_backtest(panel, selector, primary_tf=tf,
                         start_after=cfg["etf"]["selection"]["min_history"])
    print(f"\nETF momentum backtest ({len(panel)} symbols):")
    for k, v in stats.items():
        print(f"  {k:18s} {v}")


if __name__ == "__main__":
    main()
