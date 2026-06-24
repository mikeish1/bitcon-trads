"""
Shared fixtures for the dashboard (web/) tests.

`seed_sample_db()` builds a real `trading_state.db` file with the bot's exact
schema (it actually constructs a RiskManager and records trades/decisions through
the real lifecycle, so the fixture can never drift from production), plus a couple
of equity snapshots. `spot_cfg()` returns a minimal full config the web layer needs.

Imported by web tests via `from tests.conftest_web import ...`.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

# Ensure repo root on path (mirrors tests/conftest.py).
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def spot_cfg(db_path: str) -> dict[str, Any]:
    """A complete spot config dict (the shape `load_config()` returns) for tests."""
    return {
        "quote_ccy": "USDT",
        "universe_symbols": ["BTC/USDT", "ETH/USDT", "SOL/USDT"],
        "market": {"symbol": "BTC/USDT", "primary_timeframe": "1d", "poll_seconds": 900},
        "portfolio": {"max_concurrent_positions": 3, "max_total_exposure_pct": 0.90,
                      "per_asset_alloc_pct": 0.30},
        "capital_policy": {"spot": {"max_pct": 0.90, "basis": "equity", "precedence": "min"}},
        "strategy": {"mode": "donchian",
                     "donchian": {"entry_period": 40, "atr_period": 14, "atr_trail_mult": 3.0,
                                  "min_history": 60},
                     "btc_regime": {"enabled": True, "ma_period": 100},
                     "vol_target": {"enabled": False, "target_daily_vol": 0.04},
                     "allocation": {"mode": "first_come"}},
        "risk": {"default_capital_usd": 250.0, "risk_per_trade_pct": 0.01,
                 "max_position_pct": 0.95, "min_notional_usd": 10.0,
                 "kelly_fraction": 0.25, "kelly_assumed_payoff": 2.0},
        "exits": {"atr_period": 14, "atr_stop_mult": 2.0, "min_stop_pct": 0.01,
                  "atr_trail_mult": 2.5, "take_profit_R": 3.0, "stop_limit_offset_pct": 0.003,
                  "use_exchange_stop": True},
        "safety": {"daily_loss_limit_pct": 0.03, "weekly_loss_limit_pct": 0.07,
                   "max_consecutive_losses": 4, "cooldown_minutes": 60,
                   "max_trades_per_day": 4, "max_open_positions": 1},
        "execution": {"taker_fee_pct": 0.001, "paper_slippage_pct": 0.0007},
        "claude": {"model": "claude-haiku-4-5", "max_tokens": 1024, "daily_summary_hour_utc": 0},
        "logging": {"level": "INFO"},
        "runtime": {
            "paper_trading": True, "live_trading_enabled": False, "exchange_id": "binanceus",
            "alpaca_paper": True, "place_orders": False, "real_money": False,
            "use_sandbox": False, "uses_broker": False,
            "api_key": "SECRET_KEY_VALUE", "api_secret": "SECRET_SECRET_VALUE",
            "anthropic_api_key": "SECRET_ANTHROPIC", "db_path": db_path,
            "telegram_enabled": False, "telegram_token": "SECRET_TG_TOKEN",
            "telegram_chat_id": "12345",
        },
    }


def seed_sample_db(db_path: str) -> dict[str, Any]:
    """Populate a real DB through the actual RiskManager lifecycle. Returns the cfg."""
    cfg = spot_cfg(db_path)
    from src.risk_manager import RiskManager

    rm = RiskManager(cfg)
    # One closed winning BTC trade.
    tid = rm.record_open("BTC/USDT", {"price": 60000.0, "qty": 0.003, "cost": 180.0, "fee": 0.18},
                         stop_price=57000.0, take_price=0.0, stop_order_id=None,
                         reason="donchian: 40-day breakout", peak_price=60000.0)
    rm.log_decision("BTC/USDT", "BUY", 1, False, "donchian: 40-day breakout")
    row = rm.open_position("BTC/USDT")
    rm.record_close(row, exit_price=63000.0, exit_fee=0.19, reason="chandelier trail")

    # One closed losing ETH trade.
    rm.record_open("ETH/USDT", {"price": 3000.0, "qty": 0.05, "cost": 150.0, "fee": 0.15},
                   stop_price=2850.0, take_price=0.0, stop_order_id=None,
                   reason="donchian: 40-day breakout", peak_price=3000.0)
    rm.log_decision("ETH/USDT", "BUY", 1, False, "donchian: 40-day breakout")
    row = rm.open_position("ETH/USDT")
    rm.record_close(row, exit_price=2850.0, exit_fee=0.14, reason="chandelier trail")

    # One OPEN SOL position (for live MTM tests).
    rm.record_open("SOL/USDT", {"price": 150.0, "qty": 1.0, "cost": 150.0, "fee": 0.15},
                   stop_price=140.0, take_price=0.0, stop_order_id=None,
                   reason="donchian: 40-day breakout", peak_price=155.0)
    rm.log_decision("SOL/USDT", "BUY", 1, False, "donchian: 40-day breakout")
    rm.conn.close()

    # A couple of equity snapshots so performance endpoints have data.
    from web.snapshots import ensure_schema
    import sqlite3
    ensure_schema(db_path)
    c = sqlite3.connect(db_path)
    c.executemany(
        "INSERT INTO equity_snapshots(ts, equity, open_value, cash, open_positions, "
        "day_return_pct, week_return_pct, regime_on, mode) VALUES(?,?,?,?,?,?,?,?,?)",
        [("2026-06-19T00:00:00+00:00", 250.0, 0.0, 250.0, 0, 0.0, 0.0, 1, "PAPER"),
         ("2026-06-20T00:00:00+00:00", 262.0, 150.0, 112.0, 1, 4.8, 4.8, 1, "PAPER"),
         ("2026-06-21T00:00:00+00:00", 258.0, 150.0, 108.0, 1, -1.5, 3.2, 0, "PAPER")])
    c.commit()
    c.close()
    return cfg


def seed_sleeve_tables(db_path: str) -> None:
    """Add representative CARRY and ETF rows to an existing DB, using each sleeve's
    documented schema (src/carry/risk.py, src/etf/risk.py) via direct SQL.

    We insert rows directly rather than constructing EtfRiskManager/CarryRiskManager
    so the fixture stays light and the web read-path is exercised exactly as in
    production (the dashboard never instantiates those read-write managers either)."""
    import sqlite3

    c = sqlite3.connect(db_path)
    c.executescript(
        """
        CREATE TABLE IF NOT EXISTS etf_state (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS etf_positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT, status TEXT, opened_at TEXT, closed_at TEXT,
            qty REAL, entry_price REAL, cost_usd REAL, entry_fee REAL,
            exit_price REAL, exit_fee REAL, realized_pnl_usd REAL, mode TEXT, reason TEXT);
        CREATE TABLE IF NOT EXISTS carry_state (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS carry_positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            asset TEXT, status TEXT, opened_at TEXT, closed_at TEXT,
            spot_qty REAL, spot_entry REAL, perp_qty REAL, perp_entry REAL,
            notional_usd REAL, capital_usd REAL, funding_accrued_usd REAL, fees_usd REAL,
            realized_pnl_usd REAL, low_reads INTEGER, last_accrual_ts REAL, mode TEXT, reason TEXT,
            perp_closed INTEGER DEFAULT 0, spot_closed INTEGER DEFAULT 0,
            perp_exit_price REAL, perp_exit_fee REAL, spot_exit_price REAL, spot_exit_fee REAL);
        CREATE TABLE IF NOT EXISTS carry_funding (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT, asset TEXT, rate_apr REAL, notional_usd REAL, amount_usd REAL);
        """
    )
    # ETF: paper-cash ledger + one OPEN equity holding + one CLOSED winner.
    c.executemany("INSERT INTO etf_state(key,value) VALUES(?,?)",
                  [("paper_cash", "8000.0"), ("etf_last_rebalance", "2026-06-20"),
                   ("etf_regime", "risk_on")])
    c.execute("INSERT INTO etf_positions(symbol,status,opened_at,qty,entry_price,cost_usd,"
              "entry_fee,mode,reason) VALUES('SPY','OPEN','2026-06-10T00:00:00+00:00',"
              "10,500.0,5000.0,0.0,'sim','momentum rotation: top-K entry')")
    c.execute("INSERT INTO etf_positions(symbol,status,opened_at,closed_at,qty,entry_price,"
              "cost_usd,exit_price,realized_pnl_usd,mode,reason) VALUES('QQQ','CLOSED',"
              "'2026-05-01T00:00:00+00:00','2026-06-01T00:00:00+00:00',12,400.0,4800.0,"
              "410.0,120.0,'sim','rotation: out of top-K')")
    # Carry: kill off + one OPEN delta-neutral BTC pair + one CLOSED ETH pair + funding.
    c.execute("INSERT INTO carry_state(key,value) VALUES('carry_kill','0')")
    c.execute("INSERT INTO carry_positions(asset,status,opened_at,spot_qty,spot_entry,perp_qty,"
              "perp_entry,notional_usd,capital_usd,funding_accrued_usd,fees_usd,realized_pnl_usd,"
              "low_reads,last_accrual_ts,mode,reason,perp_closed,spot_closed) VALUES('BTC','OPEN',"
              "'2026-06-21T00:00:00+00:00',0.01,60000.0,0.01,60050.0,600.0,720.0,3.5,0.6,0.0,0,"
              "0.0,'sim','net carry 14.2%/yr',0,0)")
    c.execute("INSERT INTO carry_positions(asset,status,opened_at,closed_at,spot_qty,spot_entry,"
              "perp_qty,perp_entry,notional_usd,capital_usd,funding_accrued_usd,fees_usd,"
              "realized_pnl_usd,low_reads,mode,reason,perp_closed,spot_closed) VALUES('ETH','CLOSED',"
              "'2026-05-10T00:00:00+00:00','2026-05-20T00:00:00+00:00',0.2,3000.0,0.2,3005.0,600.0,"
              "720.0,12.0,0.5,12.0,3,'sim','carry decayed',1,1)")
    c.executemany("INSERT INTO carry_funding(ts,asset,rate_apr,notional_usd,amount_usd) VALUES(?,?,?,?,?)",
                  [("2026-06-21T01:00:00+00:00", "BTC", 0.142, 600.0, 1.4),
                   ("2026-06-21T02:00:00+00:00", "BTC", 0.140, 600.0, 1.3),
                   ("2026-06-22T01:00:00+00:00", "BTC", 0.138, 600.0, 0.8)])
    c.commit()
    c.close()
