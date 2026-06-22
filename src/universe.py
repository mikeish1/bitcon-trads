"""
Universe-expansion gates (liquidity + correlation + portfolio-benefit).

A disciplined, quantitative checklist a candidate coin must clear BEFORE it may be
added to the live Donchian universe. Nothing here changes runtime behaviour - it
is a validation library used by `src/universe_expansion_research.py` (and unit
tests). A symbol is APPROVED only when it passes every gate:

  1. Liquidity   - rolling-average daily dollar volume (close * volume) over
                   `volume_window` days >= `min_avg_daily_volume_usdt`.
  2. Correlation - max pairwise daily-return correlation with EXISTING members over
                   `correlation_lookback` days <= `max_pairwise_correlation`
                   (a too-correlated coin adds risk, not diversification).
  3. Portfolio   - adding the coin to an equal-weight Donchian portfolio measurably
     benefit       helps: realized vol falls OR Calmar improves (by the configured
                   margins), without blowing up turnover.

All thresholds live in `liquidity_filters` in config/trading_config.yaml.
"""
from __future__ import annotations

from typing import Any, Optional

import numpy as np
import pandas as pd


# --------------------------------------------------------------------------- #
# Small pure helpers                                                           #
# --------------------------------------------------------------------------- #
def _date_index(df: pd.DataFrame) -> pd.DatetimeIndex:
    return pd.DatetimeIndex(pd.to_datetime(df["timestamp"], utc=True)).tz_convert("UTC").tz_localize(None)


def daily_returns(df: pd.DataFrame) -> pd.Series:
    """Daily close-to-close returns indexed by (naive UTC) date."""
    return pd.Series(df["close"].pct_change().to_numpy(), index=_date_index(df)).dropna()


def rolling_avg_dollar_volume(df: pd.DataFrame, window: int) -> float:
    """Mean daily dollar volume (close * base volume) over the last `window` days.
    NaN if the frame lacks a usable `volume` column or has no rows in range."""
    if "volume" not in df.columns or len(df) == 0:
        return float("nan")
    dollar = (df["close"] * df["volume"]).tail(window)
    dollar = dollar[dollar > 0]
    return float(dollar.mean()) if len(dollar) else float("nan")


def liquidity_ok(df: pd.DataFrame, min_usdt: float, window: int) -> tuple[bool, float]:
    adv = rolling_avg_dollar_volume(df, window)
    ok = (adv == adv) and adv >= min_usdt   # NaN -> fail
    return bool(ok), adv


def max_correlation_with(candidate_df: pd.DataFrame, members: dict[str, pd.DataFrame],
                         lookback: int) -> tuple[float, Optional[str]]:
    """Largest pairwise daily-return correlation between the candidate and any
    existing member over the trailing `lookback` days. Returns (corr, member);
    (nan, None) if no member has >= 20 overlapping days."""
    cand = daily_returns(candidate_df).tail(lookback)
    worst, who = float("nan"), None
    for name, mdf in members.items():
        m = daily_returns(mdf).tail(lookback)
        joined = pd.concat([cand, m], axis=1, join="inner").dropna()
        if len(joined) < 20:
            continue
        c = float(joined.iloc[:, 0].corr(joined.iloc[:, 1]))
        if c == c and (worst != worst or c > worst):
            worst, who = c, name
    return worst, who


def correlation_ok(candidate_df: pd.DataFrame, members: dict[str, pd.DataFrame],
                   max_corr: float, lookback: int) -> tuple[bool, float, Optional[str]]:
    corr, who = max_correlation_with(candidate_df, members, lookback)
    if corr != corr:                       # undecidable (too little overlap) -> fail closed
        return False, corr, who
    return bool(corr <= max_corr), corr, who


# --------------------------------------------------------------------------- #
# Portfolio-benefit backtest (equal-weight Donchian, with vs without candidate) #
# --------------------------------------------------------------------------- #
def portfolio_stats(equity: np.ndarray) -> dict[str, float]:
    """Annualized vol, max drawdown, CAGR and Calmar (CAGR/|maxDD|) of a daily
    equity curve. Defensive against short/degenerate input."""
    eq = np.asarray(equity, dtype="float64")
    eq = eq[eq > 0]
    if len(eq) < 5:
        return {"vol": float("nan"), "max_dd": float("nan"), "cagr": float("nan"),
                "calmar": float("nan")}
    rets = np.diff(eq) / eq[:-1]
    vol = float(np.std(rets) * np.sqrt(365))
    peak = np.maximum.accumulate(eq)
    max_dd = float(((eq - peak) / peak).min())
    cagr = float((eq[-1] / eq[0]) ** (365.0 / len(eq)) - 1.0)
    calmar = float(cagr / abs(max_dd)) if max_dd < 0 else float("inf")
    return {"vol": vol, "max_dd": max_dd, "cagr": cagr, "calmar": calmar}


def _equal_weight_donchian(frames: dict[str, pd.DataFrame], entry: int, atr_mult: float,
                           fee: float, slip: float) -> tuple[np.ndarray, int]:
    """Equal-weight Donchian portfolio equity + total switches over the common
    calendar of `frames`. Reuses the validated exposure + sim engine so the gate
    matches how the bot actually trades."""
    import ta  # local imports keep `import src.universe` light (no ccxt/backtester)
    from src.strategy_search import simulate, expo_donchian

    bases = list(frames.keys())
    n = len(bases)
    eq_series, sw_total = {}, 0
    for b in bases:
        df = frames[b]
        d = {"close": df["close"].to_numpy(), "high_s": df["high"], "low_s": df["low"],
             "close_s": df["close"],
             "atr": ta.volatility.average_true_range(df["high"], df["low"], df["close"], 14).to_numpy()}
        expo = expo_donchian(d, {"entry": entry, "exit": 999, "atr_mult": atr_mult})
        run = simulate(b, expo, df["close"].to_numpy(), 1000.0 / n, fee, slip)
        eq_series[b] = pd.Series(run.equity, index=_date_index(df))
        sw_total += int(run.switch.sum())
    cstart = max(s.index.min() for s in eq_series.values())
    cend = min(s.index.max() for s in eq_series.values())
    cal = pd.date_range(cstart, cend, freq="D")
    port = np.sum([eq_series[b].reindex(cal, method="ffill").to_numpy() for b in bases], axis=0)
    return port, sw_total


def diversification_benefit(members: dict[str, pd.DataFrame], candidate: str,
                            candidate_df: pd.DataFrame, entry: int, atr_mult: float,
                            fee: float, slip: float) -> dict[str, Any]:
    """Compare an equal-weight Donchian portfolio WITHOUT vs WITH the candidate,
    over the candidate-inclusive common date range (apples-to-apples). Returns vol
    reduction %, Calmar improvement, turnover increase %, and the raw stats."""
    with_cand = {**members, candidate: candidate_df}
    # Align both portfolios to the SAME window (the candidate-inclusive overlap).
    cstart = max(_date_index(df).min() for df in with_cand.values())
    cend = min(_date_index(df).max() for df in with_cand.values())

    def _clip(frames: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
        out = {}
        for b, df in frames.items():
            idx = _date_index(df)
            out[b] = df[(idx >= cstart) & (idx <= cend)].reset_index(drop=True)
        return out

    base_eq, base_sw = _equal_weight_donchian(_clip(members), entry, atr_mult, fee, slip)
    cand_eq, cand_sw = _equal_weight_donchian(_clip(with_cand), entry, atr_mult, fee, slip)
    base, cand = portfolio_stats(base_eq), portfolio_stats(cand_eq)

    vol_red = (base["vol"] - cand["vol"]) / base["vol"] if base["vol"] else float("nan")
    calmar_impr = cand["calmar"] - base["calmar"] if np.isfinite(cand["calmar"]) and np.isfinite(base["calmar"]) else float("nan")
    turnover_inc = (cand_sw - base_sw) / base_sw if base_sw else float("nan")
    return {"base": base, "with_candidate": cand,
            "vol_reduction_pct": vol_red, "calmar_improvement": calmar_impr,
            "turnover_increase_pct": turnover_inc,
            "base_switches": base_sw, "candidate_switches": cand_sw,
            "window": (str(cstart.date()), str(cend.date()))}


# --------------------------------------------------------------------------- #
# The single gate the research script + tests call                            #
# --------------------------------------------------------------------------- #
def validate_universe_addition(candidate_symbol: str, candidate_df: pd.DataFrame,
                               members: dict[str, pd.DataFrame], cfg: dict[str, Any],
                               entry: Optional[int] = None,
                               atr_mult: Optional[float] = None) -> dict[str, Any]:
    """Run all gates for one candidate and return a structured verdict:

        {"approved": bool, "gates": {liquidity, correlation, diversification},
         "metrics": {...}, "reasons": [str, ...]}

    A symbol is approved only when EVERY gate passes. `members` is the existing
    universe's daily frames (each with open/high/low/close/volume/timestamp)."""
    lf = cfg.get("liquidity_filters", {}) or {}
    dn = (cfg.get("strategy", {}) or {}).get("donchian", {}) or {}
    ex = cfg.get("execution", {}) or {}
    entry = int(entry if entry is not None else dn.get("entry_period", 40))
    atr_mult = float(atr_mult if atr_mult is not None else dn.get("atr_trail_mult", 3.0))
    fee = float(ex.get("taker_fee_pct", 0.001))
    slip = float(ex.get("paper_slippage_pct", 0.0007))

    reasons: list[str] = []
    gates: dict[str, bool] = {}
    metrics: dict[str, Any] = {}

    # 1) Liquidity - candidate ADV must clear BOTH an absolute floor and (if set) a
    #    venue-relative floor (a fraction of the median EXISTING member's ADV). The
    #    relative floor auto-calibrates to the venue, so a thin venue (e.g. low
    #    Binance.US alt volume) doesn't need the absolute number hand-tuned.
    window = int(lf.get("volume_window", 30))
    abs_floor = float(lf.get("min_avg_daily_volume_usdt", 30e6))
    rel_pct = float(lf.get("min_relative_to_median_pct", 0.0))
    adv = rolling_avg_dollar_volume(candidate_df, window)
    member_advs = [v for v in (rolling_avg_dollar_volume(df, window) for df in members.values())
                   if v == v and v > 0]
    median_adv = float(np.median(member_advs)) if member_advs else float("nan")
    effective_floor = abs_floor
    if rel_pct > 0 and median_adv == median_adv:
        effective_floor = max(abs_floor, rel_pct * median_adv)
    liq_ok = (adv == adv) and adv >= effective_floor
    gates["liquidity"] = bool(liq_ok)
    metrics["avg_daily_volume_usdt"] = adv
    metrics["median_member_volume_usdt"] = median_adv
    metrics["effective_liquidity_floor_usdt"] = effective_floor
    reasons.append(f"liquidity {'PASS' if liq_ok else 'FAIL'}: ${adv:,.0f}/day vs floor "
                   f"${effective_floor:,.0f} (abs ${abs_floor:,.0f}; {rel_pct:.0%} of median "
                   f"member ${median_adv:,.0f})")

    # 2) Correlation
    corr_ok, corr, who = correlation_ok(candidate_df, members,
                                        float(lf.get("max_pairwise_correlation", 0.90)),
                                        int(lf.get("correlation_lookback", 180)))
    gates["correlation"] = corr_ok
    metrics["max_correlation"] = corr
    metrics["max_correlation_with"] = who
    reasons.append(f"correlation {'PASS' if corr_ok else 'FAIL'}: max {corr:.2f} with {who} "
                   f"vs <= {float(lf.get('max_pairwise_correlation', 0.90)):.2f}")

    # 3) Portfolio benefit (only run the backtest if the cheap gates passed)
    if liq_ok and corr_ok:
        div = diversification_benefit(members, candidate_symbol, candidate_df, entry, atr_mult, fee, slip)
        metrics["diversification"] = div
        vol_red, calmar_impr = div["vol_reduction_pct"], div["calmar_improvement"]
        turn_inc = div["turnover_increase_pct"]
        benefit = ((vol_red == vol_red and vol_red >= float(lf.get("min_vol_reduction_pct", 0.0)))
                   or (calmar_impr == calmar_impr and calmar_impr >= float(lf.get("min_calmar_improvement", 0.0))))
        turnover_ok = (turn_inc != turn_inc) or turn_inc <= float(lf.get("max_turnover_increase_pct", 0.50))
        gates["diversification"] = bool(benefit and turnover_ok)
        reasons.append(f"diversification {'PASS' if gates['diversification'] else 'FAIL'}: "
                       f"vol {vol_red:+.1%}, Calmar {calmar_impr:+.2f}, turnover {turn_inc:+.1%}")
    else:
        gates["diversification"] = False
        reasons.append("diversification SKIPPED (failed an earlier gate)")

    approved = all(gates.values())
    return {"approved": approved, "gates": gates, "metrics": metrics, "reasons": reasons}
