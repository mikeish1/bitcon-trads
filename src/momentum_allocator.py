"""
Momentum-rotation allocator (live).

Cross-sectional momentum selection: of the coins currently in an active Donchian
trend, hold the K STRONGEST by N-day momentum, rotating at most every
`rebalance_days`. This is the live, paper-testable form of the variant validated
out-of-sample in src/momentum_final.py (top-4 / 2-day / 90-day beat the per-coin
first-come baseline across regimes).

This module is PURE selection logic - it decides which symbols to hold, enter and
exit. The main loop owns sizing, order placement, stops and all safety rails;
RISK exits (chandelier trail, regime risk-off) still happen every cycle in the
loop, independent of the rotation clock.
"""
from __future__ import annotations

from datetime import date
from typing import Any

import pandas as pd


class MomentumRotation:
    def __init__(self, cfg: dict[str, Any]):
        a = cfg["strategy"].get("allocation", {}).get("momentum_rotation", {})
        self.top_k = int(a.get("top_k", 4))
        self.rebalance_days = int(a.get("rebalance_days", 2))
        self.lookback_days = int(a.get("lookback_days", 90))
        self.keep_band = int(a.get("keep_band", 0))
        self.primary_tf = cfg["market"]["primary_timeframe"]

    # ------------------------------------------------------------------ #
    def is_due(self, last_day_iso: str | None, today_iso: str) -> bool:
        """Has it been >= rebalance_days since the last rotation? (None -> yes)."""
        if not last_day_iso:
            return True
        try:
            gap = (date.fromisoformat(today_iso) - date.fromisoformat(last_day_iso)).days
        except ValueError:
            return True
        return gap >= self.rebalance_days

    def momentum(self, frames: dict[str, pd.DataFrame]) -> float | None:
        """N-day momentum from daily closes; None if not enough history."""
        df = frames.get(self.primary_tf)
        if df is None or len(df) <= self.lookback_days:
            return None
        c = df["close"]
        prev = float(c.iloc[-1 - self.lookback_days])
        cur = float(c.iloc[-1])
        if prev <= 0 or prev != prev or cur != cur:
            return None
        return cur / prev - 1.0

    def plan(self, candidates: dict[str, float], held: list[str]) -> dict[str, Any]:
        """
        candidates : {symbol: momentum} for coins in an active trend (regime-on).
        held       : symbols we currently hold.

        Returns target set, plus the entries/exits to reach it. Hysteresis keeps a
        held coin until its momentum rank slips below top_k + keep_band, so we
        don't churn on tiny rank flips.
        """
        order = [s for s, _ in sorted(candidates.items(), key=lambda kv: kv[1], reverse=True)]
        rank = {s: i for i, s in enumerate(order)}

        keep = [s for s in held if rank.get(s, 10**9) < self.top_k + self.keep_band]
        target = list(keep)
        for s in order:                       # fill remaining slots from strongest
            if len(target) >= self.top_k:
                break
            if s not in target:
                target.append(s)
        target_set = set(target[:self.top_k])

        to_exit = [s for s in held if s not in target_set]
        to_enter = [s for s in order if s in target_set and s not in held]
        return {"target": target_set, "enter": to_enter, "exit": to_exit, "rank": rank}
