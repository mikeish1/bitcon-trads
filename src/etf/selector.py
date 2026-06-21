"""
ETF momentum selector — the reusable core (PURE, offline-testable).

This is the "reuse momentum_allocator almost verbatim" piece: it wraps the exact
same `DonchianStrategy.active_state` trend filter and `MomentumRotation` top-K
selector the crypto bot uses, and applies them to an ETF universe. No network.

A symbol is eligible if it is currently in an active Donchian trend; eligible
symbols are then ranked by N-day momentum and the strongest K are held, with the
same rotation clock + rank hysteresis as the crypto allocator.
"""
from __future__ import annotations

from typing import Any

import pandas as pd

from src.momentum_allocator import MomentumRotation
from src.strategy import DonchianStrategy


def _shim_cfg(cfg: dict[str, Any]) -> dict[str, Any]:
    """Build the minimal cfg shape the reused crypto components expect from the
    ETF config block, so we instantiate them unchanged."""
    s = cfg["etf"]["selection"]
    return {
        "market": {"primary_timeframe": cfg["etf"]["primary_timeframe"]},
        "strategy": {
            "donchian": {
                "entry_period": s["entry_period"],
                "atr_trail_mult": s["atr_trail_mult"],
                "min_history": s["min_history"],
            },
            "allocation": {"momentum_rotation": {
                "top_k": s["top_k"],
                "rebalance_days": s["rebalance_days"],
                "lookback_days": s["lookback_days"],
                "keep_band": s["keep_band"],
            }},
        },
    }


class EtfMomentumSelector:
    def __init__(self, cfg: dict[str, Any]):
        shim = _shim_cfg(cfg)
        self.primary_tf = shim["market"]["primary_timeframe"]
        self.donchian = DonchianStrategy(shim)       # reused verbatim
        self.rotation = MomentumRotation(shim)       # reused verbatim
        self.top_k = self.rotation.top_k

    def is_due(self, last_day_iso: str | None, today_iso: str) -> bool:
        return self.rotation.is_due(last_day_iso, today_iso)

    def candidates(self, frames_by_symbol: dict[str, dict[str, pd.DataFrame]]) -> dict[str, float]:
        """{symbol: momentum} for symbols in an active Donchian trend with momentum
        defined. Mirrors the crypto loop's rotation candidate gathering."""
        cands: dict[str, float] = {}
        for sym, frames in frames_by_symbol.items():
            if self.donchian.active_state(frames):
                mom = self.rotation.momentum(frames)
                if mom is not None:
                    cands[sym] = mom
        return cands

    def plan(self, frames_by_symbol: dict[str, dict[str, pd.DataFrame]],
             held: list[str]) -> dict[str, Any]:
        """Target set + enter/exit lists to reach the strongest-K eligible ETFs."""
        return self.rotation.plan(self.candidates(frames_by_symbol), held)
