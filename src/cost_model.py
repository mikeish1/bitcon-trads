"""
Cost-aware pair preference for the low-turnover Donchian strategy.

A daily breakout system rebalances rarely, so per-trade SPREAD + FEES dominate its
execution drag far more than latency. This module estimates each coin's effective
ROUND-TRIP trading cost (in basis points) so the bot can, all else equal, prefer
cheaper-to-trade pairs - without ever overriding hard risk or regime rules.

Effective cost is intentionally lightweight and computed from data already on hand:

    effective_cost_bps = 2 * taker_fee_bps          # one entry + one exit, taker
                         + spread_factor * spread_proxy_bps

`spread_proxy_bps` is the median daily range (high-low)/close over a window, scaled
down by `spread_factor` to approximate an intraday bid/ask spread from the daily
candle. It is a RELATIVE liquidity proxy for RANKING coins (tighter-ranging majors
score cheaper than thin, choppy alts) - not an absolute spread measurement. Fees
are exact; the spread term is a proxy, so treat the score as ordinal.
"""
from __future__ import annotations

from typing import Any, Optional

import pandas as pd


def spread_proxy_bps(df: pd.DataFrame, window: int = 20) -> float:
    """Median (high-low)/close over the last `window` days, in bps. NaN if the
    frame is too short or malformed."""
    if df is None or len(df) < 3 or not {"high", "low", "close"} <= set(df.columns):
        return float("nan")
    rng = ((df["high"] - df["low"]) / df["close"]).tail(window)
    rng = rng[(rng >= 0) & (rng == rng)]
    if len(rng) == 0:
        return float("nan")
    return float(rng.median() * 1e4)


def symbol_fee_bps(symbol: str, exchange: Any, cfg: dict[str, Any]) -> float:
    """Per-symbol TAKER fee in bps, resolved (highest precedence first) from a
    config override, the venue's market metadata, then the flat config taker fee."""
    ex = cfg.get("execution", {}) or {}
    base = symbol.split("/")[0].upper()
    ov = (ex.get("fee_overrides", {}) or {}).get(base) or (ex.get("fee_overrides", {}) or {}).get(symbol)
    if ov and ov.get("taker") is not None:
        return float(ov["taker"]) * 1e4
    if exchange is not None:
        try:
            t = exchange.market(symbol).get("taker")
            if t is not None:
                return float(t) * 1e4
        except Exception:
            pass
    return float(ex.get("taker_fee_pct", 0.001)) * 1e4


def symbol_spread_bps(symbol: str, exchange: Any) -> float:
    """Live bid/ask half... full spread in bps from the venue ticker; NaN when no
    quote is available (offline / backtest)."""
    if exchange is None:
        return float("nan")
    try:
        t = exchange.fetch_ticker(symbol)
        bid, ask = t.get("bid"), t.get("ask")
        if bid and ask and float(bid) > 0 and float(ask) > 0:
            mid = (float(bid) + float(ask)) / 2.0
            return (float(ask) - float(bid)) / mid * 1e4
    except Exception:
        pass
    return float("nan")


def effective_cost_bps(df: pd.DataFrame, cfg: dict[str, Any],
                       fee_bps: Optional[float] = None,
                       spread_bps: Optional[float] = None) -> float:
    """Estimated round-trip effective cost (bps) for one coin.

    Uses REAL inputs when supplied: `fee_bps` (venue taker tier) and `spread_bps`
    (a live bid/ask spread, charged ~once per round trip). Otherwise falls back to
    the flat config taker fee and the scaled daily-range spread proxy. When no
    spread is available at all, returns the fee-only floor."""
    ex = cfg.get("execution", {}) or {}
    taker_bps = float(fee_bps) if (fee_bps is not None and fee_bps == fee_bps) \
        else float(ex.get("taker_fee_pct", 0.001)) * 1e4
    fee_cost = 2.0 * taker_bps
    if spread_bps is not None and spread_bps == spread_bps:      # real live spread
        return fee_cost + float(spread_bps)
    factor = float(ex.get("spread_proxy_factor", 0.1))
    window = int(ex.get("spread_proxy_window", 20))
    sp = spread_proxy_bps(df, window)
    if sp != sp:                       # NaN -> fee-only estimate
        return fee_cost
    return fee_cost + factor * sp


def universe_costs(frames_by_symbol: dict[str, dict[str, pd.DataFrame]], cfg: dict[str, Any],
                   primary_tf: str) -> dict[str, float]:
    """Map symbol -> effective_cost_bps from the daily-candle PROXY (offline-safe)."""
    out: dict[str, float] = {}
    for sym, frames in frames_by_symbol.items():
        df = frames.get(primary_tf) if isinstance(frames, dict) else frames
        cost = effective_cost_bps(df, cfg)
        if cost == cost:
            out[sym] = cost
    return out


def live_costs(frames_by_symbol: dict[str, dict[str, pd.DataFrame]], exchange: Any,
               cfg: dict[str, Any], primary_tf: str) -> dict[str, float]:
    """Map symbol -> effective_cost_bps from REAL venue fees + live spreads, with the
    daily-range proxy as a per-symbol fallback when a quote is unavailable. One
    ticker fetch per symbol - fine for a low-frequency daily strategy."""
    out: dict[str, float] = {}
    for sym, frames in frames_by_symbol.items():
        df = frames.get(primary_tf) if isinstance(frames, dict) else frames
        fee = symbol_fee_bps(sym, exchange, cfg)
        spread = symbol_spread_bps(sym, exchange)
        cost = effective_cost_bps(df, cfg, fee_bps=fee, spread_bps=spread)
        if cost == cost:
            out[sym] = cost
    return out


def cost_preference_mode(cfg: dict[str, Any]) -> str:
    mode = str((cfg.get("execution", {}) or {}).get("cost_preference_mode", "off")).lower()
    return mode if mode in ("off", "soft", "strict") else "off"


def filter_by_cost(costs: dict[str, float], cfg: dict[str, Any]) -> tuple[list[str], list[str]]:
    """STRICT mode: split symbols into (kept, dropped) at max_effective_cost_bps."""
    ceil = float((cfg.get("execution", {}) or {}).get("max_effective_cost_bps", 60.0))
    kept = [s for s, c in costs.items() if c <= ceil]
    dropped = [s for s, c in costs.items() if c > ceil]
    return kept, dropped


def cost_penalty(symbol: str, costs: dict[str, float], cfg: dict[str, Any]) -> float:
    """SOFT mode: a small non-negative score penalty proportional to effective cost
    (cost_penalty_weight x cost_bps / 1e4). Used as a tie-breaker on allocator/entry
    scores - large enough to break ties, small relative to real signal strength."""
    weight = float((cfg.get("execution", {}) or {}).get("cost_penalty_weight", 1.0))
    c = costs.get(symbol)
    if c is None or c != c:
        return 0.0
    return weight * c / 1e4
