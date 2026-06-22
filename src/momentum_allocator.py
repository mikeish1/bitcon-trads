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
        # Concentration cap: trim a held name above cap_mult x (equity/top_k) back to
        # the cap each rebalance (None = off; winners run unbounded). Bounds single-
        # name tail risk in the whole-position live book; validated in research.
        cap = a.get("concentration_cap_mult", None)
        self.cap_mult = float(cap) if cap is not None else None
        self.primary_tf = cfg["market"]["primary_timeframe"]

        # --- Composite scoring (opt-in; default preserves the simple ROC) --------
        self.scoring = str(a.get("scoring", "simple")).lower()
        thr = a.get("min_momentum_threshold", None)
        self.min_momentum_threshold = None if thr is None else float(thr)
        comp = a.get("composite", {}) or {}
        self.comp_weights = comp.get("weights", {}) or {
            "breakout": 0.30, "roc_long": 0.30, "roc_short": 0.15, "rel_btc": 0.15, "inv_vol": 0.10}
        self.comp_roc_short = int(comp.get("roc_short_days", 20))
        self.comp_entry_period = int(comp.get(
            "entry_period", cfg["strategy"].get("donchian", {}).get("entry_period", 40)))
        self.comp_normalize = str(comp.get("normalize", "zscore")).lower()

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

    # ------------------------------------------------------------------ #
    # Composite scoring                                                  #
    # ------------------------------------------------------------------ #
    def _roc(self, df: pd.DataFrame, days: int) -> float | None:
        c = df["close"]
        if df is None or len(c) <= days or days <= 0:
            return None
        prev = float(c.iloc[-1 - days])
        cur = float(c.iloc[-1])
        if prev <= 0 or prev != prev or cur != cur:
            return None
        return cur / prev - 1.0

    def raw_components(self, frames: dict[str, pd.DataFrame],
                       btc_frames: dict[str, pd.DataFrame] | None = None) -> dict[str, float] | None:
        """Per-asset RAW (un-normalized) composite components, or None if the long
        ROC can't be computed. Components:
          breakout : (close - prior `entry_period`-day high) / ATR  (breakout strength,
                     can be negative when price has pulled back below the band),
          roc_long : N-day momentum (the validated signal),
          roc_short: shorter-horizon ROC,
          rel_btc  : asset roc_long minus BTC roc_long (relative strength),
          inv_vol  : close/ATR (inverse normalized ATR - a calm/liquidity proxy).
        """
        df = frames.get(self.primary_tf)
        if df is None or "close" not in df:
            return None
        roc_long = self._roc(df, self.lookback_days)
        if roc_long is None:
            return None
        close = float(df["close"].iloc[-1])
        atr = float(df["atr"].iloc[-1]) if "atr" in df and df["atr"].iloc[-1] == df["atr"].iloc[-1] else 0.0

        prior_high = float("nan")
        if len(df) > self.comp_entry_period:
            ph = df["high"].rolling(self.comp_entry_period).max().shift(1).iloc[-1]
            prior_high = float(ph) if ph == ph else float("nan")
        breakout = ((close - prior_high) / atr) if (atr > 0 and prior_high == prior_high) else 0.0

        roc_short = self._roc(df, self.comp_roc_short)
        btc_long = self._roc(btc_frames.get(self.primary_tf), self.lookback_days) if btc_frames else None
        rel_btc = (roc_long - btc_long) if btc_long is not None else roc_long
        inv_vol = (close / atr) if atr > 0 else 0.0

        return {"breakout": breakout, "roc_long": roc_long,
                "roc_short": roc_short if roc_short is not None else roc_long,
                "rel_btc": rel_btc, "inv_vol": inv_vol}

    @staticmethod
    def _normalize(values: dict[str, float], mode: str) -> dict[str, float]:
        """Cross-sectional normalization of one component across candidates.
        zscore -> (x-mean)/std ; rank -> [0,1] rank. Degenerate input -> all 0.0."""
        keys = list(values)
        xs = [values[k] for k in keys]
        n = len(xs)
        if n == 0:
            return {}
        if n == 1:
            return {keys[0]: 0.0}
        if mode == "rank":
            order = sorted(range(n), key=lambda i: xs[i])
            out = {}
            for rank, i in enumerate(order):
                out[keys[i]] = rank / (n - 1)
            return out
        mean = sum(xs) / n
        var = sum((x - mean) ** 2 for x in xs) / n
        std = var ** 0.5
        if std <= 0:
            return {k: 0.0 for k in keys}
        return {k: (values[k] - mean) / std for k in keys}

    def score_candidates(self, frames_by_symbol: dict[str, dict[str, pd.DataFrame]],
                         btc_frames: dict[str, pd.DataFrame] | None = None) -> dict[str, float]:
        """Score a set of candidate symbols for top-K ranking.

        Returns {symbol: score} only for symbols that (a) have enough history and
        (b) pass `min_momentum_threshold` on their long ROC. In "simple" mode the
        score IS the long ROC (current validated behaviour). In "composite" mode the
        score is a weighted sum of cross-sectionally normalized components, so the
        allocator concentrates on the strongest, highest-quality breakouts.
        """
        # 1) gather raw components + apply the absolute-momentum gate.
        raw: dict[str, dict[str, float]] = {}
        for sym, frames in frames_by_symbol.items():
            comps = self.raw_components(frames, btc_frames)
            if comps is None:
                continue
            if self.min_momentum_threshold is not None and comps["roc_long"] < self.min_momentum_threshold:
                continue
            raw[sym] = comps
        if not raw:
            return {}

        if self.scoring != "composite":
            return {sym: comps["roc_long"] for sym, comps in raw.items()}

        # 2) normalize each component cross-sectionally, then weight-sum.
        components = list(self.comp_weights)
        normed: dict[str, dict[str, float]] = {c: {} for c in components}
        for c in components:
            normed[c] = self._normalize({sym: raw[sym].get(c, 0.0) for sym in raw}, self.comp_normalize)
        scores: dict[str, float] = {}
        for sym in raw:
            scores[sym] = sum(float(self.comp_weights.get(c, 0.0)) * normed[c].get(sym, 0.0)
                              for c in components)
        return scores

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
