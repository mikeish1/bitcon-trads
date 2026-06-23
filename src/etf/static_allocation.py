"""
Static fixed-weight allocator (PURE) — the Stage-4-validated ETF sleeve.

Holds a FIXED weight map (default 40% SPY / 40% AGG / 20% GLD — the diversified
blend that beat both Dual Momentum and SPY on risk-adjusted, after-tax, OOS terms;
see docs/equities_replatform/validation_report.md). Rebalances on a slow clock
(default ~quarterly) and only trades a symbol whose weight has drifted beyond a
band — so turnover, and therefore taxable realization, stays low.

This is deliberately NOT a momentum selector: there is no signal, no regime, no
absolute filter. The edge is diversification + discipline, not prediction. Same
interface as the other selectors (`is_due`, `plan`, `top_k`) plus `target_weights`.
"""
from __future__ import annotations

from datetime import date
from typing import Any

import pandas as pd

DEFAULT_WEIGHTS = {"SPY": 0.40, "AGG": 0.40, "GLD": 0.20}


def rebalance_deltas(current_value: dict[str, float], weights: dict[str, float],
                     equity: float, *, band: float, min_notional: float) -> dict[str, float]:
    """PURE rebalance decision shared by the live loop and the backtest simulator (so
    they cannot drift - the golden-master for the static path).

    Given each symbol's current market value, the target weights and total equity,
    return {symbol: signed notional delta to trade} (positive = buy, negative = sell).
    A symbol already held within the drift band is left alone; a delta below
    min_notional is skipped (dust).
    """
    deltas: dict[str, float] = {}
    for s in set(weights) | set(current_value):
        cur = current_value.get(s, 0.0)
        target = weights.get(s, 0.0) * equity
        if cur > 0 and equity > 0 and abs(cur - target) / equity < band:
            continue
        d = target - cur
        if abs(d) >= min_notional:
            deltas[s] = d
    return deltas


class StaticAllocator:
    def __init__(self, cfg: dict[str, Any]):
        e = cfg["etf"]
        sa = e.get("static_allocation", {}) or {}
        raw = sa.get("weights", DEFAULT_WEIGHTS)
        w = {str(s).upper(): float(x) for s, x in raw.items() if float(x) > 0}
        total = sum(w.values()) or 1.0
        self.weights = {s: x / total for s, x in w.items()}      # normalized to 1.0
        self.rebalance_days = int(sa.get("rebalance_days", 63))  # ~quarterly
        self.drift_band = float(sa.get("drift_band", 0.05))      # skip trades within +/-5%
        self.primary_tf = e["primary_timeframe"]
        self.top_k = len(self.weights)

    def is_due(self, last_day_iso: str | None, today_iso: str) -> bool:
        if not last_day_iso:
            return True
        try:
            gap = (date.fromisoformat(today_iso) - date.fromisoformat(last_day_iso)).days
        except ValueError:
            return True
        return gap >= self.rebalance_days

    def target_weights(self) -> dict[str, float]:
        return dict(self.weights)

    def plan(self, frames_by_symbol: dict[str, dict[str, pd.DataFrame]],
             held: list[str]) -> dict[str, Any]:
        """Target = the weighted symbols that have data; plus the weight map so the
        loop/simulator can rebalance to targets (not just enter/exit)."""
        target = {s for s in self.weights if s in frames_by_symbol}
        enter = [s for s in target if s not in held]
        exits = [s for s in held if s not in self.weights]
        return {"target": target, "enter": enter, "exit": exits,
                "weights": {s: self.weights[s] for s in target}}
