"""
Higher-timeframe market-regime gate (lightweight; research + live compatible).

This matures the original `strategy.btc_regime` skeleton (a bare BTC > MA check)
into a small, testable module that several call sites can share:

  * the live loop (`src/main_loop.py`) gates new entries and can flatten / tighten
    open positions when the reference asset turns risk-off;
  * the research backtesters can toggle and compare the gate apples-to-apples.

A regime is computed from one reference asset's DAILY frame (default BTC) and is
returned as a small immutable :class:`RegimeState`:

    risk_on     - bool: is the market in a tradable risk-on state right now?
    score       - float in [0, 1]: a soft risk-on score (1 = fully risk-on).
    size_factor - float: fraction of normal NEW-position size permitted now
                  (1.0 risk-on; `risk_off_size_factor`, e.g. 0.0 or 0.2, risk-off).
    method      - which detector produced the state.
    reason      - short human string for logs / audit.

Methods
-------
ma        : close > rolling MA(ma_period).                (== legacy btc_regime)
ma_slope  : close > MA  AND  MA rising over slope_lookback.
vol       : realized daily vol over vol_period <= vol_ceiling (step aside in turbulence).
composite : weighted blend of {ma, slope, vol} risk-on flags vs score_threshold.

Everything is computed from history available at the bar close (no lookahead).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

import pandas as pd


@dataclass(frozen=True)
class RegimeState:
    """Immutable snapshot of the market regime (see module docstring)."""
    risk_on: bool
    score: float
    size_factor: float
    method: str
    reason: str


def _last_float(series: pd.Series) -> Optional[float]:
    """Last finite value of a series, or None if empty / NaN."""
    if series is None or len(series) == 0:
        return None
    val = series.iloc[-1]
    try:
        f = float(val)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def _ma_flags(df: pd.DataFrame, ma_period: int, slope_lookback: int) -> tuple[Optional[bool], Optional[bool]]:
    """(close>MA, MA-rising) booleans; either is None when history is insufficient."""
    close = df["close"]
    if len(close) < ma_period + 1:
        return None, None
    ma = close.rolling(ma_period).mean()
    ma_now, close_now = _last_float(ma), _last_float(close)
    above = None if ma_now is None or close_now is None else close_now > ma_now
    rising = None
    if len(ma) > slope_lookback:
        ma_prev = float(ma.iloc[-1 - slope_lookback])
        if ma_now is not None and ma_prev == ma_prev:
            rising = ma_now > ma_prev
    return above, rising


def _realized_vol(df: pd.DataFrame, vol_period: int) -> Optional[float]:
    """Annualization-free realized DAILY vol = stdev of daily returns over the window."""
    close = df["close"]
    if len(close) < vol_period + 1:
        return None
    rets = close.pct_change().dropna().tail(vol_period)
    if len(rets) < 2:
        return None
    return _last_float(pd.Series([rets.std()]))


def get_regime_state(df_btc: Optional[pd.DataFrame], method: str = "ma",
                     params: Optional[dict[str, Any]] = None) -> RegimeState:
    """Compute the current regime from a reference asset's daily frame.

    Defaults to risk-ON when history is insufficient or the frame is missing - the
    same fail-open behaviour as the legacy `btc_regime` check, so a warm-up gap can
    never silently flatten the book.

    Parameters
    ----------
    df_btc : the reference asset's daily OHLC frame (needs a `close` column), or None.
    method : "ma" | "ma_slope" | "vol" | "composite".
    params : optional overrides (ma_period, slope_lookback, vol_period, vol_ceiling,
             weights, score_threshold, risk_off_size_factor).
    """
    p = params or {}
    ma_period = int(p.get("ma_period", 100))
    slope_lookback = int(p.get("slope_lookback", 20))
    vol_period = int(p.get("vol_period", 20))
    vol_ceiling = float(p.get("vol_ceiling", 0.05))
    score_threshold = float(p.get("score_threshold", 0.5))
    weights = p.get("weights", {"ma": 0.5, "slope": 0.25, "vol": 0.25}) or {}
    off_factor = float(p.get("risk_off_size_factor", 0.0))

    def _state(risk_on: bool, score: float, reason: str) -> RegimeState:
        return RegimeState(risk_on=risk_on, score=round(float(score), 3),
                           size_factor=1.0 if risk_on else max(0.0, off_factor),
                           method=method, reason=reason)

    if df_btc is None or "close" not in getattr(df_btc, "columns", []) or len(df_btc) < 2:
        return _state(True, 1.0, "insufficient regime history -> assume risk-on")

    above, rising = _ma_flags(df_btc, ma_period, slope_lookback)
    vol = _realized_vol(df_btc, vol_period)
    vol_ok = None if vol is None else vol <= vol_ceiling

    if method == "ma":
        if above is None:
            return _state(True, 1.0, "MA warming up -> risk-on")
        return _state(above, 1.0 if above else 0.0,
                      f"close {'>' if above else '<='} MA{ma_period}")

    if method == "ma_slope":
        if above is None:
            return _state(True, 1.0, "MA warming up -> risk-on")
        on = bool(above and (rising if rising is not None else True))
        return _state(on, 1.0 if on else 0.0,
                      f"close>MA{ma_period}={above}, MA-rising={rising}")

    if method == "vol":
        if vol_ok is None:
            return _state(True, 1.0, "vol warming up -> risk-on")
        return _state(vol_ok, 1.0 if vol_ok else 0.0,
                      f"daily vol {vol:.3f} {'<=' if vol_ok else '>'} {vol_ceiling:.3f}")

    if method == "composite":
        flags = {"ma": above, "slope": rising, "vol": vol_ok}
        num = 0.0
        den = 0.0
        for key, flag in flags.items():
            w = float(weights.get(key, 0.0))
            if w <= 0 or flag is None:
                continue
            den += w
            num += w * (1.0 if flag else 0.0)
        score = (num / den) if den > 0 else 1.0   # nothing decidable yet -> risk-on
        on = score >= score_threshold
        return _state(on, score, f"composite score {score:.2f} "
                                 f"{'>=' if on else '<'} {score_threshold:.2f}")

    # Unknown method -> safest reproducible default: legacy MA behaviour.
    if above is None:
        return _state(True, 1.0, f"unknown method '{method}', MA warming up -> risk-on")
    return _state(above, 1.0 if above else 0.0,
                  f"unknown method '{method}', fell back to close vs MA{ma_period}")


def regime_from_config(df_btc: Optional[pd.DataFrame], cfg: dict[str, Any]) -> RegimeState:
    """Build a RegimeState using the live `strategy.regime` config block. When
    `strategy.regime.enabled` is false this returns the LEGACY `btc_regime` MA gate
    so existing deployments behave identically until they opt into the new module."""
    s = cfg.get("strategy", {})
    rg = s.get("regime", {}) or {}
    if rg.get("enabled"):
        return get_regime_state(df_btc, method=rg.get("method", "ma"), params=rg)
    legacy = s.get("btc_regime", {}) or {}
    return get_regime_state(df_btc, method="ma",
                            params={"ma_period": legacy.get("ma_period", 100),
                                    "risk_off_size_factor": 0.0})
