"""
Thin portfolio overlay / sleeve allocator.

Computes target weights across the three sibling sleeves - Donchian (crypto
trend), Carry (funding), ETF (momentum) - from each sleeve's RECENT equity curve.
It is deliberately *thin*: it consumes a small, standardized performance contract
and returns weights. It does NOT fetch data, manage risk, or place orders - all of
that stays inside the three independent bots, which keep running unchanged.

Data contract (the only thing the allocator needs)
--------------------------------------------------
`performance` is a dict keyed by sleeve name ("donchian" | "carry" | "etf"). Each
value is EITHER a metrics dict or an equity series:

    {"donchian": {"ret": 0.12, "vol": 0.025, "sharpe": 1.4},      # summary metrics
     "carry":    {"equity": <pd.Series or list of daily equity>}, # raw curve -> metrics
     "etf":      {"ret": 0.03, "vol": 0.010}}                     # sharpe optional

`metrics_from_equity()` converts a curve to {ret, vol, sharpe, n}. Missing sleeves
or unusable metrics are handled defensively (dropped, with the remainder sharing
the book; an all-missing input returns equal weights).

Weighting modes
---------------
* risk_parity            : weight inversely proportional to recent daily vol.
* momentum_of_strategies : tilt toward sleeves with stronger recent Sharpe/return,
                           blended with equal weight by `momentum_tilt` (anti-whip).

An optional regime overlay modestly boosts the Donchian weight when the crypto
regime is risk-on. Final weights are clamped to [min_weight, max_weight], summed
to 1 via iterative bound projection, and only changed when drift from the previous
weights exceeds `rebalance_threshold` (turnover control).
"""
from __future__ import annotations

import sqlite3
from typing import Any, Mapping, Optional

import numpy as np
import pandas as pd
from loguru import logger

_TRADING_DAYS = 365.0   # crypto trades daily; annualization factor for Sharpe


def metrics_from_equity(equity: Any, lookback_days: int = 60) -> dict[str, Any]:
    """Summarize a daily equity curve over its last `lookback_days` points.

    Returns {ret, vol, sharpe, n}: total return over the window, daily-return
    stdev (vol), annualized Sharpe, and the number of return observations. Robust
    to short / noisy input: <3 valid points -> vol NaN and zero ret/sharpe so the
    allocator can drop the sleeve."""
    s = pd.Series(list(equity) if not isinstance(equity, pd.Series) else equity, dtype="float64")
    s = s.dropna()
    s = s[s > 0]
    if len(s) < 3:
        return {"ret": 0.0, "vol": float("nan"), "sharpe": 0.0, "n": int(len(s))}
    s = s.tail(int(lookback_days) + 1)
    rets = s.pct_change().dropna()
    if len(rets) < 2 or rets.std() == 0:
        return {"ret": float(s.iloc[-1] / s.iloc[0] - 1.0), "vol": float("nan"),
                "sharpe": 0.0, "n": int(len(rets))}
    vol = float(rets.std())
    sharpe = float(rets.mean() / vol * np.sqrt(_TRADING_DAYS))
    return {"ret": float(s.iloc[-1] / s.iloc[0] - 1.0), "vol": vol, "sharpe": sharpe,
            "n": int(len(rets))}


class SleeveAllocator:
    """Stateless weight calculator over the configured sleeves (see module docs)."""

    def __init__(self, cfg: dict[str, Any]):
        p = (cfg.get("portfolio", {}) or {}).get("sleeves", {}) or {}
        self.members: list[str] = list(p.get("members", ["donchian", "carry", "etf"]))
        self.mode = str(p.get("allocator_mode", "risk_parity")).lower()
        self.lookback_days = int(p.get("lookback_days", 60))
        self.min_weight = float(p.get("min_weight", 0.15))
        self.max_weight = float(p.get("max_weight", 0.60))
        self.rebalance_threshold = float(p.get("rebalance_threshold", 0.10))
        self.momentum_metric = str(p.get("momentum_metric", "sharpe")).lower()
        self.momentum_tilt = float(p.get("momentum_tilt", 0.5))
        self.regime_boost_factor = float(p.get("regime_boost_factor", 0.20))
        self.vol_floor = float(p.get("vol_floor", 0.001))

    # ------------------------------------------------------------------ #
    def _coerce_metrics(self, perf: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
        """Normalize the input contract to {sleeve: {ret, vol, sharpe}} for the
        configured members only. Accepts either summary dicts or {'equity': series}."""
        out: dict[str, dict[str, Any]] = {}
        for name in self.members:
            v = perf.get(name)
            if v is None:
                continue
            if isinstance(v, Mapping) and "equity" in v and "vol" not in v:
                m = metrics_from_equity(v["equity"], self.lookback_days)
            elif isinstance(v, Mapping):
                m = {"ret": float(v.get("ret", 0.0)),
                     "vol": float(v["vol"]) if v.get("vol") is not None else float("nan"),
                     "sharpe": float(v.get("sharpe", 0.0))}
            else:  # a bare series / list -> treat as an equity curve
                m = metrics_from_equity(v, self.lookback_days)
            out[name] = m
        return out

    def _raw_weights(self, metrics: dict[str, dict[str, Any]], mode: str) -> dict[str, float]:
        names = list(metrics)
        if mode == "momentum_of_strategies":
            key = "sharpe" if self.momentum_metric == "sharpe" else "ret"
            scores = {n: float(metrics[n].get(key, 0.0) or 0.0) for n in names}
            lo = min(scores.values())
            shifted = {n: scores[n] - lo + 1e-9 for n in names}   # make non-negative
            tot = sum(shifted.values()) or 1.0
            eq = 1.0 / len(names)
            # blend equal weight with the score-share (tilt=0 -> equal, 1 -> full tilt)
            return {n: (1 - self.momentum_tilt) * eq + self.momentum_tilt * shifted[n] / tot
                    for n in names}
        # default: risk parity (inverse vol)
        inv = {}
        for n in names:
            vol = metrics[n].get("vol")
            vol = self.vol_floor if (vol is None or vol != vol or vol <= 0) else max(vol, self.vol_floor)
            inv[n] = 1.0 / vol
        tot = sum(inv.values()) or 1.0
        return {n: inv[n] / tot for n in names}

    def _apply_regime(self, raw: dict[str, float], regime_state: Optional[dict]) -> dict[str, float]:
        if not regime_state or "donchian" not in raw or self.regime_boost_factor == 0:
            return raw
        if not bool(regime_state.get("risk_on", False)):
            return raw
        boosted = dict(raw)
        boosted["donchian"] = raw["donchian"] * (1.0 + self.regime_boost_factor)
        logger.debug("Sleeve regime overlay: risk-on -> Donchian raw weight x{:.2f}",
                     1.0 + self.regime_boost_factor)
        return boosted

    def _bound_and_normalize(self, raw: dict[str, float]) -> dict[str, float]:
        """Project raw weights onto {sum==1, min<=w<=max} via iterative
        clamp-and-redistribute (box-constrained simplex projection). After each
        clamp, the leftover budget is shared - proportionally to the raw weights -
        only among sleeves NOT pinned against the binding bound, so a coincident
        floor+ceiling never inflates a capped sleeve past its ceiling. Bounds are
        auto-relaxed when the member count makes them infeasible (one sleeve -> 1.0)."""
        keys = list(raw)
        n = len(keys)
        if n == 0:
            return {}
        lo = min(self.min_weight, 1.0 / n)      # ensure lo*n <= 1 (feasible)
        hi = max(self.max_weight, 1.0 / n)      # ensure hi*n >= 1 (feasible)
        base_raw = {k: max(raw[k], 0.0) for k in keys}
        tot = sum(base_raw.values()) or 1.0
        w = {k: base_raw[k] / tot for k in keys}
        for _ in range(2 * n + 2):
            w = {k: min(hi, max(lo, v)) for k, v in w.items()}
            gap = 1.0 - sum(w.values())
            if abs(gap) < 1e-12:
                break
            # Redistribute the gap among sleeves that can still move in that direction.
            if gap > 0:
                movable = [k for k in keys if w[k] < hi - 1e-15]   # room to take more
            else:
                movable = [k for k in keys if w[k] > lo + 1e-15]   # room to give back
            if not movable:
                break
            share_base = sum(base_raw[k] for k in movable) or float(len(movable))
            for k in movable:
                share = (base_raw[k] / share_base) if share_base else 1.0 / len(movable)
                w[k] += gap * share
        # Final clamp + tiny renormalize to wipe floating-point residue.
        w = {k: min(hi, max(lo, v)) for k, v in w.items()}
        s = sum(w.values()) or 1.0
        return {k: v / s for k, v in w.items()}

    # ------------------------------------------------------------------ #
    def compute_weights(self, performance: Mapping[str, Any],
                        regime_state: Optional[dict] = None,
                        mode: Optional[str] = None,
                        prev_weights: Optional[dict[str, float]] = None) -> dict[str, float]:
        """Return target weights {sleeve: weight} summing to 1.

        performance   : the data contract (see module docstring).
        regime_state  : optional {'risk_on': bool} - boosts Donchian when risk-on.
        mode          : override the configured allocator_mode for this call.
        prev_weights  : last applied weights; if drift < rebalance_threshold the
                        previous weights are returned unchanged (turnover control).
        """
        mode = (mode or self.mode).lower()
        metrics = self._coerce_metrics(performance)
        # Drop sleeves with no usable vol in risk-parity (can't size them).
        usable = {n: m for n, m in metrics.items()
                  if not (mode != "momentum_of_strategies" and (m.get("vol") is None or m.get("vol") != m.get("vol")))}
        if not usable:
            eq = {n: 1.0 / len(self.members) for n in self.members}
            logger.warning("SleeveAllocator: no usable sleeve metrics - equal weights {}.", _fmt(eq))
            return eq

        raw = self._raw_weights(usable, mode)
        raw = self._apply_regime(raw, regime_state)
        weights = self._bound_and_normalize(raw)

        if prev_weights:
            drift = max(abs(weights.get(k, 0.0) - prev_weights.get(k, 0.0))
                        for k in set(weights) | set(prev_weights))
            if drift < self.rebalance_threshold:
                logger.info("SleeveAllocator [{}]: drift {:.1%} < {:.1%} band - holding {}.",
                            mode, drift, self.rebalance_threshold, _fmt(prev_weights))
                return dict(prev_weights)

        logger.info("SleeveAllocator [{}]: inputs {} -> weights {}{}.", mode,
                    {n: {k: round(v, 3) if isinstance(v, float) else v for k, v in m.items()}
                     for n, m in usable.items()},
                    _fmt(weights),
                    " (regime risk-on boost)" if (regime_state and regime_state.get("risk_on")) else "")
        return weights


def _fmt(weights: Mapping[str, float]) -> str:
    return "{" + ", ".join(f"{k}:{v:.0%}" for k, v in weights.items()) + "}"


# --------------------------------------------------------------------------- #
# Thin read-only DB adapter: build each sleeve's recent equity curve from the   #
# data each bot ALREADY persists (no bot changes, no order/risk logic here).    #
# --------------------------------------------------------------------------- #
def aggregate_sleeve_equity(db_path: str, cfg: dict[str, Any],
                            lookback_days: int = 90) -> dict[str, pd.Series]:
    """Best-effort daily equity curve per sleeve, read READ-ONLY from the shared
    SQLite file. Returns only sleeves with >= 3 daily points.

      donchian : real per-day marks from `equity_history` (written by the spot bot).
      carry    : delta-neutral proxy = sleeve + cumulative funding income.
      etf      : coarse proxy = sleeve + cumulative realized PnL of closed positions.

    Anything missing / thin is simply omitted, and the allocator degrades to the
    sleeves it does have."""
    out: dict[str, pd.Series] = {}
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.OperationalError:
        return out
    conn.row_factory = sqlite3.Row
    try:
        out_don = _curve(conn, "SELECT day, equity AS v FROM equity_history ORDER BY day")
        if out_don is not None:
            out["donchian"] = out_don.tail(lookback_days + 1)

        sleeve_c = float(((cfg.get("carry", {}) or {}).get("capital", {}) or {}).get("sleeve_usd", 0.0))
        carry = _cum_curve(conn,
                           "SELECT substr(ts,1,10) AS day, SUM(amount_usd) AS v "
                           "FROM carry_funding GROUP BY day ORDER BY day", base=sleeve_c)
        if carry is not None:
            out["carry"] = carry.tail(lookback_days + 1)

        sleeve_e = float(((cfg.get("etf", {}) or {}).get("capital", {}) or {}).get("sleeve_usd", 0.0))
        etf = _cum_curve(conn,
                         "SELECT substr(closed_at,1,10) AS day, SUM(realized_pnl_usd) AS v "
                         "FROM etf_positions WHERE status='CLOSED' AND closed_at IS NOT NULL "
                         "GROUP BY day ORDER BY day", base=sleeve_e)
        if etf is not None:
            out["etf"] = etf.tail(lookback_days + 1)
    except sqlite3.OperationalError as exc:
        logger.debug("aggregate_sleeve_equity: partial (a sleeve table is absent): {}", exc)
    finally:
        conn.close()
    return out


def _curve(conn: sqlite3.Connection, sql: str) -> Optional[pd.Series]:
    try:
        rows = conn.execute(sql).fetchall()
    except sqlite3.OperationalError:
        return None
    vals = [(r["day"], float(r["v"])) for r in rows if r["v"] is not None]
    if len(vals) < 3:
        return None
    return pd.Series([v for _, v in vals], index=[d for d, _ in vals])


def _cum_curve(conn: sqlite3.Connection, sql: str, base: float) -> Optional[pd.Series]:
    try:
        rows = conn.execute(sql).fetchall()
    except sqlite3.OperationalError:
        return None
    daily = [(r["day"], float(r["v"] or 0.0)) for r in rows if r["day"]]
    if len(daily) < 3 or base <= 0:
        return None
    cum, eq, idx = base, [], []
    for day, amt in daily:
        cum += amt
        eq.append(cum)
        idx.append(day)
    return pd.Series(eq, index=idx)


def build_sleeve_performance(db_path: str, cfg: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Convenience: aggregate each sleeve's equity from the shared DB and convert
    to the allocator's metrics contract. Empty dict if nothing is available yet."""
    lookback = int(((cfg.get("portfolio", {}) or {}).get("sleeves", {}) or {}).get("lookback_days", 60))
    curves = aggregate_sleeve_equity(db_path, cfg, lookback_days=lookback)
    return {name: metrics_from_equity(series, lookback) for name, series in curves.items()}
