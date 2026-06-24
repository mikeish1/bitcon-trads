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
    # Decide signals on the last CONFIRMED-CLOSED daily bar (recommended). During the
    # session the most recent daily bar is still forming, so selecting on it diverges
    # from the close-based backtest; marking/sizing still use the live price.
    e["signal_on_closed_candle"] = _env_bool(
        "ETF_SIGNAL_ON_CLOSED_CANDLE", bool(e.get("signal_on_closed_candle", True)))

    sel = e.setdefault("selection", {})
    sel.setdefault("entry_period", 40)
    sel.setdefault("atr_trail_mult", 3.0)
    sel.setdefault("min_history", 60)
    sel.setdefault("top_k", 5)
    sel.setdefault("rebalance_days", 5)
    sel.setdefault("lookback_days", 90)
    sel.setdefault("keep_band", 1)
    # Selector: "rotation" (Donchian-gated top-K, original) | "dual_momentum" (GEM).
    sel["mode"] = os.getenv("ETF_SELECTION_MODE", sel.get("mode", "rotation")).strip().lower()

    # --- Dual-momentum (Antonacci GEM-style) parameters (mode: dual_momentum) ----
    # Absolute + relative momentum across an OFFENSIVE basket with a DEFENSIVE sleeve
    # fallback. Few-parameter, low-turnover (tax-friendly). Off unless mode selects it.
    # See docs/equities_replatform/strategy_options.md + data_bias_audit.md.
    dm = e.setdefault("dual_momentum", {})
    dm.setdefault("offensive", ["SPY", "EFA", "EEM"])
    dm.setdefault("defensive", ["TLT", "IEF", "GLD", "BIL"])
    dm.setdefault("abs_benchmark", "BIL")     # T-bill proxy = the absolute hurdle ("" -> 0.0)
    dm.setdefault("lookback_days", 252)       # ~12-month momentum (abs + rel share it)
    dm.setdefault("top_k", 1)                 # GEM classic = hold the single strongest
    dm.setdefault("rebalance_days", 20)       # ~monthly cadence
    dm.setdefault("keep_band", 0)             # rank hysteresis
    dm.setdefault("min_history", 260)         # need >= lookback + buffer bars

    # Pattern-Day-Trader guard: never round-trip a symbol the same day it was opened
    # (the design holds multi-day/weekly, so this only blocks accidental same-day
    # exits). Keeps a <$25k margin account clear of the 3-day-trades/5-day rule.
    e["pdt_guard"] = _env_bool("ETF_PDT_GUARD", bool(e.get("pdt_guard", True)))

    # --- Static fixed-weight allocation (mode: static_allocation) ----------------
    # The Stage-4-VALIDATED ETF sleeve: a diversified buy-and-hold-rebalance blend
    # (default 40% SPY / 40% AGG / 20% GLD). Drift-band + slow clock keep turnover
    # (and taxable realization) minimal. See docs/equities_replatform/validation_report.md.
    sa = e.setdefault("static_allocation", {})
    sa.setdefault("weights", {"SPY": 0.40, "AGG": 0.40, "GLD": 0.20})
    sa.setdefault("rebalance_days", 63)       # ~quarterly
    sa.setdefault("drift_band", 0.05)         # only trade a symbol drifted > +/-5% of equity

    # The tradable universe follows the selected mode's symbols (unless ETF_UNIVERSE
    # was set explicitly).
    if not uni_env and sel["mode"] == "dual_momentum":
        seen: list[str] = []
        for s in [*dm["offensive"], *dm["defensive"]]:
            if s.upper() not in seen:
                seen.append(s.upper())
        e["universe"] = seen
    elif not uni_env and sel["mode"] == "static_allocation":
        e["universe"] = [str(s).upper() for s in sa["weights"]]

    cap = e.setdefault("capital", {})
    cap["sleeve_usd"] = _env_float("ETF_SLEEVE_USD", cap.get("sleeve_usd", 2000.0))
    cap.setdefault("max_total_exposure_pct", 0.95)
    cap.setdefault("min_notional_usd", 10.0)

    e.setdefault("alpaca_feed", "iex")             # free Alpaca data uses the IEX feed
    # Split- AND dividend-adjust bars by default. RAW (unadjusted) bars would inject
    # phantom split/ex-div gaps that fire false trend exits - see data_bias_audit.md.
    e["alpaca_adjustment"] = os.getenv(
        "ETF_ALPACA_ADJUSTMENT", str(e.get("alpaca_adjustment", "all"))).strip().lower()

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
