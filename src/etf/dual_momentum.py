"""
Dual-momentum selector (Antonacci GEM-style), PURE and offline-testable.

The Stage-1-selected design for the ETF sleeve. Combines:
  * RELATIVE momentum — rank an OFFENSIVE basket (e.g. US / dev-intl / EM equity)
    by N-day return and hold the strongest K, and
  * ABSOLUTE momentum — only hold an offensive name while its own momentum beats a
    cash/T-bill hurdle; otherwise rotate to the strongest of a DEFENSIVE sleeve
    (Treasuries / gold / T-bills), which is the drawdown control.

Why this shape: absolute momentum is the historical drawdown lever, the defensive
sleeve gives risk-off capital somewhere real to hide (gold/T-bills cover the 2022
"bonds and stocks both fell" case), and the design is few-parameter and low-turnover
— important for a small TAXABLE account.

Maximal reuse: momentum, the rebalance clock, and the top-K + hysteresis selection
are the crypto bot's `MomentumRotation`, unchanged. This class only adds the
regime split (which candidate set feeds the allocator). It exposes the same
interface as `EtfMomentumSelector` (`is_due`, `plan`, `top_k`), so the loop and
backtester consume it without changes.
"""
from __future__ import annotations

from typing import Any

import pandas as pd

from src.momentum_allocator import MomentumRotation


class DualMomentumSelector:
    def __init__(self, cfg: dict[str, Any]):
        e = cfg["etf"]
        dm = e.get("dual_momentum", {}) or {}
        tf = e["primary_timeframe"]
        self.primary_tf = tf
        self.offensive = [s.upper() for s in dm.get("offensive", ["SPY", "EFA", "EEM"])]
        self.defensive = [s.upper() for s in dm.get("defensive", ["TLT", "IEF", "GLD", "BIL"])]
        bench = dm.get("abs_benchmark", "BIL")
        # The symbol whose momentum is the absolute hurdle (a T-bill proxy). Empty /
        # null / a symbol with no history -> hurdle falls back to 0.0 (momentum > 0).
        self.benchmark_symbol = str(bench).upper() if bench else ""

        # Reuse MomentumRotation verbatim for momentum + clock + top-K + hysteresis.
        shim = {
            "market": {"primary_timeframe": tf},
            "strategy": {"allocation": {"momentum_rotation": {
                "top_k": int(dm.get("top_k", 1)),
                "rebalance_days": int(dm.get("rebalance_days", 20)),
                "lookback_days": int(dm.get("lookback_days", 252)),
                "keep_band": int(dm.get("keep_band", 0)),
            }}},
        }
        self.rotation = MomentumRotation(shim)
        self.top_k = self.rotation.top_k

    # ------------------------------------------------------------------ #
    def is_due(self, last_day_iso: str | None, today_iso: str) -> bool:
        return self.rotation.is_due(last_day_iso, today_iso)

    def _momenta(self, frames_by_symbol: dict[str, dict[str, pd.DataFrame]],
                 symbols: list[str]) -> dict[str, float]:
        """{symbol: N-day momentum} for the requested symbols that have history."""
        out: dict[str, float] = {}
        for s in symbols:
            frames = frames_by_symbol.get(s)
            if frames is None:
                continue
            mom = self.rotation.momentum(frames)
            if mom is not None:
                out[s] = mom
        return out

    def regime(self, frames_by_symbol: dict[str, dict[str, pd.DataFrame]]
               ) -> tuple[dict[str, float], str, float]:
        """Decide the candidate set for this rebalance.

        Returns (candidates, regime, hurdle):
          * risk_on  -> offensive names whose momentum BEATS the absolute hurdle,
          * risk_off -> the defensive sleeve (the strongest of which — typically
            bonds/gold, or the T-bill proxy when all else falls — becomes the hold).
        """
        offensive = self._momenta(frames_by_symbol, self.offensive)
        defensive = self._momenta(frames_by_symbol, self.defensive)
        hurdle = defensive.get(self.benchmark_symbol, 0.0) if self.benchmark_symbol else 0.0
        qualified = {s: m for s, m in offensive.items() if m > hurdle}
        if qualified:
            return qualified, "risk_on", hurdle
        return defensive, "risk_off", hurdle

    def plan(self, frames_by_symbol: dict[str, dict[str, pd.DataFrame]],
             held: list[str]) -> dict[str, Any]:
        """Target set + enter/exit to reach the dual-momentum hold, plus the regime."""
        candidates, regime, hurdle = self.regime(frames_by_symbol)
        out = self.rotation.plan(candidates, held)
        out["regime"] = regime
        out["hurdle"] = hurdle
        return out
