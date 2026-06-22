"""
Read-only metric extraction for the ops agent.

Two sides of the comparison:

  LIVE  - from the shared SQLite the bots already write: the daily equity curve
          (`equity_history`), closed-trade stats (`trades`), and slippage
          aggregates (`fills`). Nothing here ever writes.

  BACKTEST - a walk-forward reference built in-process from cached daily candles
          using the SAME validated engine the bot trades (equal-weight Donchian via
          `src.universe._equal_weight_donchian`). From its equity curve we derive
          (a) recent daily returns aligned to the live window, and (b) a DISTRIBUTION
          of rolling-window metrics, so the live window can be z-scored against
          "what this strategy normally does."

All functions degrade gracefully (empty / NaN) when data is missing, so the agent
can run on a fresh system and simply report "insufficient data".
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import numpy as np
import pandas as pd

from src.ops_stats import daily_returns
from src.slippage import slippage_summary
from src.universe import _equal_weight_donchian, portfolio_stats

_BACKTEST_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backtests")


# --------------------------------------------------------------------------- #
# LIVE                                                                         #
# --------------------------------------------------------------------------- #
def _ro_conn(db_path: str) -> Optional[sqlite3.Connection]:
    try:
        c = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        c.row_factory = sqlite3.Row
        return c
    except sqlite3.OperationalError:
        return None


def live_equity_series(db_path: str, lookback_days: int) -> pd.Series:
    """Daily equity marks from `equity_history` (most recent `lookback_days`)."""
    conn = _ro_conn(db_path)
    if conn is None:
        return pd.Series(dtype="float64")
    try:
        rows = conn.execute(
            "SELECT day, equity FROM equity_history ORDER BY day DESC LIMIT ?",
            (lookback_days + 1,)).fetchall()
    except sqlite3.OperationalError:
        return pd.Series(dtype="float64")
    finally:
        conn.close()
    rows = list(reversed(rows))
    if len(rows) < 2:
        return pd.Series(dtype="float64")
    return pd.Series([float(r["equity"]) for r in rows], index=[r["day"] for r in rows])


def live_trade_stats(db_path: str, since_iso: Optional[str]) -> dict[str, Any]:
    """Closed-trade win rate / profit factor / counts over the window."""
    conn = _ro_conn(db_path)
    if conn is None:
        return {"closed": 0}
    where, params = "status='CLOSED'", []
    if since_iso:
        where += " AND closed_at >= ?"; params = [since_iso]
    try:
        rows = conn.execute(f"SELECT pnl_usd FROM trades WHERE {where}", params).fetchall()
    except sqlite3.OperationalError:
        return {"closed": 0}
    finally:
        conn.close()
    pnls = [float(r["pnl_usd"]) for r in rows if r["pnl_usd"] is not None]
    if not pnls:
        return {"closed": 0}
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_win = sum(wins); gross_loss = -sum(losses)
    return {
        "closed": len(pnls),
        "win_rate": round(len(wins) / len(pnls), 3),
        "profit_factor": round(gross_win / gross_loss, 3) if gross_loss > 0 else float("inf"),
        "avg_pnl_usd": round(float(np.mean(pnls)), 2),
        "total_pnl_usd": round(float(sum(pnls)), 2),
    }


def live_metrics(db_path: str, lookback_days: int, thresholds: dict[str, Any]) -> dict[str, Any]:
    """Bundle the live side: equity, returns, window metrics, trades, slippage."""
    eq = live_equity_series(db_path, lookback_days)
    since = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).isoformat()
    ret = daily_returns(eq.to_numpy()) if len(eq) else np.array([])
    wm = window_metrics(eq.to_numpy()) if len(eq) >= 5 else {}
    slip = slippage_summary(db_path, since)
    return {
        "days": int(len(eq)),
        "returns": ret,
        "window_metrics": wm,
        "trades": live_trade_stats(db_path, since),
        "slippage": slip if slip else {"fills": 0},
    }


# --------------------------------------------------------------------------- #
# BACKTEST reference                                                          #
# --------------------------------------------------------------------------- #
def window_metrics(equity: Any) -> dict[str, float]:
    """Total return + annualized vol / max_dd / cagr / calmar of an equity curve."""
    eq = np.asarray(equity, dtype="float64")
    eq = eq[np.isfinite(eq) & (eq > 0)]
    if len(eq) < 5:
        return {}
    s = portfolio_stats(eq)
    return {"total_return": float(eq[-1] / eq[0] - 1.0), "vol": s["vol"],
            "max_dd": s["max_dd"], "cagr": s["cagr"], "calmar": s["calmar"]}


def backtest_equity(cfg: dict[str, Any], window_months: int, years: float = 8.0,
                    exchange: str = "auto") -> np.ndarray:
    """Equal-weight Donchian portfolio equity over the recent `window_months`,
    using the live config's entry/atr_trail params. Empty array if data missing."""
    from src.backtester import _daily   # local import (pulls ccxt) keeps this module light
    dn = cfg["strategy"]["donchian"]
    entry, atr_mult = int(dn["entry_period"]), float(dn["atr_trail_mult"])
    ex = cfg.get("execution", {})
    fee, slip = float(ex.get("taker_fee_pct", 0.001)), float(ex.get("paper_slippage_pct", 0.0007))
    bases = [str(b).upper() for b in cfg["universe"]["bases"]]
    frames: dict[str, pd.DataFrame] = {}
    cutoff = pd.Timestamp(datetime.now(timezone.utc)) - pd.DateOffset(months=window_months)
    for b in bases:
        try:
            df = _daily(b, years, exchange)
        except Exception:
            continue
        idx = pd.DatetimeIndex(pd.to_datetime(df["timestamp"], utc=True))
        df = df[idx >= cutoff].reset_index(drop=True)        # both tz-aware UTC
        if len(df) > max(entry + 5, 30):
            frames[b] = df
    if len(frames) < 1:
        return np.array([])
    eq, _ = _equal_weight_donchian(frames, entry, atr_mult, fee, slip)
    return eq


def rolling_window_dist(equity: np.ndarray, window: int) -> dict[str, list[float]]:
    """Slide a `window`-day window across the backtest equity and collect the
    distribution of each window metric (for z-scoring the live window)."""
    eq = np.asarray(equity, dtype="float64")
    eq = eq[np.isfinite(eq) & (eq > 0)]
    out: dict[str, list[float]] = {"total_return": [], "calmar": [], "max_dd": [], "vol": []}
    if len(eq) < window + 5:
        return out
    step = max(1, window // 4)            # overlap to get more samples
    for start in range(0, len(eq) - window, step):
        wm = window_metrics(eq[start:start + window + 1])
        for k in out:
            v = wm.get(k)
            if v is not None and np.isfinite(v):
                out[k].append(float(v))
    return out


def _data_fingerprint(bases: list[str]) -> str:
    """Fingerprint the cached daily candles (mtime+size per base) so a refreshed
    cache invalidates a stored artifact."""
    parts = []
    for b in sorted(bases):
        p = os.path.join(_BACKTEST_DIR, f"{b}_1d.csv")
        if os.path.exists(p):
            st = os.stat(p)
            parts.append(f"{b}:{int(st.st_mtime)}:{st.st_size}")
    return ";".join(parts)


def artifact_key(cfg: dict[str, Any], window_months: int) -> str:
    """Stable 16-hex key over the inputs that define a backtest baseline: universe,
    entry/ATR params, costs, window, and the data fingerprint."""
    dn = cfg["strategy"]["donchian"]
    ex = cfg.get("execution", {})
    bases = [str(b).upper() for b in cfg["universe"]["bases"]]
    raw = json.dumps({
        "bases": sorted(bases), "entry": int(dn["entry_period"]),
        "atr": float(dn["atr_trail_mult"]), "fee": float(ex.get("taker_fee_pct", 0.001)),
        "slip": float(ex.get("paper_slippage_pct", 0.0007)), "window_months": int(window_months),
        "data": _data_fingerprint(bases),
    }, sort_keys=True)
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def save_backtest_artifact(art_dir: str, key: str, equity: np.ndarray,
                           meta: dict[str, Any]) -> str:
    """Persist the (expensive) backtest equity curve + provenance for reuse/audit."""
    os.makedirs(art_dir, exist_ok=True)
    path = os.path.join(art_dir, f"backtest_{key}.json")
    payload = {"created_at": datetime.now(timezone.utc).isoformat(), "key": key,
               "equity": [float(x) for x in equity], "meta": meta}
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    return path


def load_backtest_artifact(art_dir: str, key: str, ttl_hours: float) -> Optional[dict[str, Any]]:
    """Load a cached artifact if it exists and is within `ttl_hours`; else None."""
    path = os.path.join(art_dir, f"backtest_{key}.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            payload = json.load(fh)
        ttl = float(ttl_hours)
        if ttl <= 0:                       # 0/negative -> cache disabled (always recompute)
            return None
        created = datetime.fromisoformat(payload["created_at"])
        age_h = (datetime.now(timezone.utc) - created).total_seconds() / 3600.0
        if age_h > ttl:
            return None
        return payload
    except Exception:
        return None


def backtest_reference(cfg: dict[str, Any], live_days: int, window_months: int,
                       artifacts: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Backtest side: recent daily returns (aligned to the live window) + the
    rolling-window metric distribution.

    When `artifacts.enabled`, the expensive equity curve is loaded from a cached,
    fingerprinted artifact (reproducible + auditable) and only recomputed when the
    inputs/data change or the TTL lapses; the freshly computed curve is then saved.
    Returns include `from_cache` and `artifact_key` for the audit trail."""
    artifacts = artifacts or {}
    use_cache = bool(artifacts.get("enabled", False))
    art_dir = artifacts.get("dir", "ops/artifacts")
    ttl = float(artifacts.get("ttl_hours", 24))
    key = artifact_key(cfg, window_months) if use_cache else ""
    eq: np.ndarray
    from_cache = False
    cached = load_backtest_artifact(art_dir, key, ttl) if use_cache else None
    if cached is not None:
        eq = np.asarray(cached["equity"], dtype="float64")
        from_cache = True
    else:
        eq = backtest_equity(cfg, window_months)
        if use_cache and len(eq) >= 10:
            save_backtest_artifact(art_dir, key, eq, {"window_months": window_months,
                                                      "bases": cfg["universe"]["bases"]})
    if len(eq) < 10:
        return {"returns": np.array([]), "window_dist": {}, "days": int(len(eq)),
                "from_cache": from_cache, "artifact_key": key}
    ret = daily_returns(eq)
    aligned = ret[-live_days:] if (live_days and len(ret) > live_days) else ret
    return {"returns": aligned, "window_dist": rolling_window_dist(eq, max(live_days, 10)),
            "days": int(len(eq)), "full_returns": ret, "from_cache": from_cache,
            "artifact_key": key}
