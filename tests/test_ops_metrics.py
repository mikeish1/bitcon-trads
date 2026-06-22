"""Read-only live-metric extraction + window/rolling metric helpers."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from src import ops_metrics as M


def _seed_db(path, days=25):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE equity_history(day TEXT PRIMARY KEY, equity REAL, ts TEXT)")
    con.execute("CREATE TABLE trades(id INTEGER PRIMARY KEY, status TEXT, pnl_usd REAL, closed_at TEXT)")
    con.execute("CREATE TABLE fills(id INTEGER PRIMARY KEY, ts TEXT, symbol TEXT, side TEXT, "
                "order_type TEXT, intended_price REAL, fill_price REAL, qty REAL, "
                "slippage_bps REAL, slippage_usd REAL, fee_usd REAL, mode TEXT, reason TEXT)")
    # Seed relative to NOW so rows land inside the agent's lookback window.
    base = datetime.now(timezone.utc) - timedelta(days=days)
    eq = 1000.0
    for i in range(days):
        d = (base + timedelta(days=i)).date().isoformat()
        eq *= 1.001 if i % 2 == 0 else 0.999
        con.execute("INSERT INTO equity_history VALUES(?,?,?)", (d, eq, d))
    for i, pnl in enumerate([12.0, -5.0, 8.0, -3.0]):
        ts = (base + timedelta(days=i)).isoformat()
        con.execute("INSERT INTO trades(status, pnl_usd, closed_at) VALUES('CLOSED',?,?)", (pnl, ts))
    for i in range(6):
        ts = (base + timedelta(days=i)).isoformat()
        con.execute("INSERT INTO fills(ts, symbol, side, order_type, slippage_bps, slippage_usd, fee_usd) "
                    "VALUES(?,?,?,?,?,?,?)", (ts, "BTC/USDT", "buy", "limit", 2.0 * i, 0.1 * i, 0.5))
    con.commit(); con.close()


def test_live_metrics_on_seeded_db(tmp_path):
    db = str(tmp_path / "state.db")
    _seed_db(db, days=25)
    m = M.live_metrics(db, lookback_days=60, thresholds={})
    assert m["days"] == 25
    assert len(m["returns"]) == 24
    assert m["trades"]["closed"] == 4
    assert m["trades"]["win_rate"] == pytest.approx(0.5)
    assert m["slippage"]["fills"] == 6
    assert "calmar" in m["window_metrics"]


def test_live_metrics_empty_db_is_safe(tmp_path):
    db = str(tmp_path / "empty.db")
    sqlite3.connect(db).close()
    m = M.live_metrics(db, lookback_days=60, thresholds={})
    assert m["days"] == 0 and len(m["returns"]) == 0
    assert m["trades"] == {"closed": 0} and m["slippage"] == {"fills": 0}


def test_window_metrics_and_rolling_dist():
    eq = np.array([1000.0 * (1.002 ** i) for i in range(120)])
    wm = M.window_metrics(eq)
    assert set(wm) == {"total_return", "vol", "max_dd", "cagr", "calmar"}
    assert wm["total_return"] > 0
    dist = M.rolling_window_dist(eq, window=30)
    assert len(dist["total_return"]) >= 2     # several overlapping windows sampled


def test_backtest_artifact_save_load_roundtrip_and_ttl(tmp_path):
    d = str(tmp_path / "artifacts")
    eq = np.array([1000.0 * (1.001 ** i) for i in range(50)])
    path = M.save_backtest_artifact(d, "abc123", eq, {"window_months": 24})
    assert path.endswith("backtest_abc123.json")
    loaded = M.load_backtest_artifact(d, "abc123", ttl_hours=24)
    assert loaded is not None and len(loaded["equity"]) == 50
    # Expired TTL -> miss.
    assert M.load_backtest_artifact(d, "abc123", ttl_hours=0.0) is None
    # Unknown key -> miss.
    assert M.load_backtest_artifact(d, "nope", ttl_hours=24) is None


def test_artifact_key_changes_with_params():
    cfg = {"strategy": {"donchian": {"entry_period": 40, "atr_trail_mult": 3.0}},
           "execution": {"taker_fee_pct": 0.001, "paper_slippage_pct": 0.0007},
           "universe": {"bases": ["BTC", "ETH"]}}
    k1 = M.artifact_key(cfg, 24)
    cfg2 = {**cfg, "strategy": {"donchian": {"entry_period": 55, "atr_trail_mult": 3.0}}}
    k2 = M.artifact_key(cfg2, 24)
    assert k1 != k2 and len(k1) == 16          # param change -> different key
