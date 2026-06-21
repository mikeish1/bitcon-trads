"""
ETF config + venue wiring. Reuses the base loader (and its two-key tripwire) and
layers the `etf:` block + `ETF_*` env overrides on top.

Run mode (mirrors the spot/carry tiers):
  * sim  - internal paper ledger against live prices, send nothing   (default)
  * live - real orders, real money. Requires ETF_ENABLED=true AND PAPER_TRADING=
           false AND LIVE_TRADING_ENABLED=true AND etf.execution.mode == "live".
"""
from __future__ import annotations

import os
from typing import Any

import ccxt
from loguru import logger

from src.config import _env_bool, _env_float, load_config


def load_etf_config() -> dict[str, Any]:
    cfg = load_config()
    cfg.setdefault("etf", {})
    e = cfg["etf"]

    e["enabled"] = _env_bool("ETF_ENABLED", bool(e.get("enabled", False)))
    e.setdefault("venue", "alpaca")
    e.setdefault("primary_timeframe", "1d")
    e.setdefault("backfill_days", 400)
    uni_env = os.getenv("ETF_UNIVERSE")
    if uni_env:
        e["universe"] = [s.strip().upper() for s in uni_env.split(",") if s.strip()]
    e.setdefault("universe", ["SPY", "QQQ", "IWM", "EFA", "EEM", "TLT", "IEF", "GLD", "DBC", "VNQ"])
    e.setdefault("poll_seconds", 3600)

    sel = e.setdefault("selection", {})
    sel.setdefault("entry_period", 40)
    sel.setdefault("atr_trail_mult", 3.0)
    sel.setdefault("min_history", 60)
    sel.setdefault("top_k", 5)
    sel.setdefault("rebalance_days", 5)
    sel.setdefault("lookback_days", 90)
    sel.setdefault("keep_band", 1)

    cap = e.setdefault("capital", {})
    cap["sleeve_usd"] = _env_float("ETF_SLEEVE_USD", cap.get("sleeve_usd", 2000.0))
    cap.setdefault("max_total_exposure_pct", 0.95)
    cap.setdefault("min_notional_usd", 10.0)

    e.setdefault("alpaca_feed", "iex")             # free Alpaca data uses the IEX feed

    ex = e.setdefault("execution", {})
    mode = os.getenv("ETF_EXECUTION_MODE", ex.get("mode", "sim")).strip().lower()
    ex["mode"] = mode
    ex.setdefault("taker_fee_pct", 0.0)            # Alpaca equities are commission-free
    ex.setdefault("paper_slippage_pct", 0.0005)

    venue = e["venue"]
    is_alpaca = venue == "alpaca"
    alpaca_paper = _env_bool("ALPACA_PAPER", True)
    base_rt = cfg["runtime"]
    two_key_live = (not base_rt["paper_trading"]) and base_rt["live_trading_enabled"]
    want_orders = bool(e["enabled"] and mode == "live")

    # Mirror the crypto bot's tiers. On Alpaca, paper mode places REAL PAPER orders
    # (safe, no money); real money additionally needs the two-key tripwire.
    if not want_orders:
        place_orders, real_money = False, False                       # sim
    elif is_alpaca and alpaca_paper:
        place_orders, real_money = True, False                        # paper-broker
    else:
        place_orders, real_money = two_key_live, two_key_live         # real money

    mode_label = "live" if real_money else ("paper-broker" if place_orders else "sim")
    cfg["etf_runtime"] = {
        "enabled": e["enabled"],
        "mode": mode_label,
        "real_money": real_money,
        "place_orders": place_orders,
        "alpaca_paper": alpaca_paper,
        "venue": venue,
        "quote": "USD",
        "api_key": os.getenv("ALPACA_API_KEY", "") if is_alpaca
        else os.getenv(f"{venue.upper()}_API_KEY", ""),
        "api_secret": os.getenv("ALPACA_API_SECRET", "") if is_alpaca
        else os.getenv(f"{venue.upper()}_API_SECRET", ""),
    }
    return cfg


def build_etf_exchange(cfg: dict[str, Any]) -> ccxt.Exchange:
    rt = cfg["etf_runtime"]
    params: dict[str, Any] = {"enableRateLimit": True}
    if rt["api_key"]:
        params["apiKey"] = rt["api_key"]
        params["secret"] = rt["api_secret"]
    try:
        exchange = getattr(ccxt, rt["venue"])(params)
    except AttributeError:
        raise SystemExit(f"Unknown ccxt exchange id '{rt['venue']}' in etf.venue.")
    try:
        exchange.load_markets()
    except Exception as exc:  # pragma: no cover - network
        logger.warning("Could not preload {} markets ({}); will retry on demand.",
                       rt["venue"], exc)
    return exchange
