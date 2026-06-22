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
