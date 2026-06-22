"""
Configuration loader.

Reads config/trading_config.yaml, then applies environment-variable overrides
for secrets, the trading venue, and the live-trading switches. All secrets and
"flip-it-fast" settings live in env vars (.env locally, or Railway variables).

Supported venues (EXCHANGE_ID):
  * binanceus - Binance.US spot. No sandbox; internal simulation in paper mode,
    real orders only via the two-key tripwire.
  * alpaca    - Alpaca. Has a real PAPER brokerage endpoint, so paper orders are
    actually placed there (realistic fills, a real paper account to watch).

A basic user only edits .env.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

load_dotenv()

_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "trading_config.yaml"


def _env_bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _env_float(name: str, default: float) -> float:
    val = os.getenv(name)
    if val is None or val.strip() == "":
        return default
    try:
        return float(val)
    except ValueError:
        return default


def load_config() -> dict[str, Any]:
    with open(_CONFIG_PATH, "r", encoding="utf-8") as fh:
        cfg: dict[str, Any] = yaml.safe_load(fh)

    paper = _env_bool("PAPER_TRADING", True)
    live_enabled = _env_bool("LIVE_TRADING_ENABLED", False)
    two_key_live = (not paper) and live_enabled  # real-money tripwire

    exchange_id = os.getenv("EXCHANGE_ID", "binanceus").strip().lower()
    is_alpaca = exchange_id == "alpaca"
    alpaca_paper = _env_bool("ALPACA_PAPER", True)

    if is_alpaca:
        # Alpaca has a genuine PAPER brokerage endpoint - placing (paper) orders
        # there is safe and realistic.
        use_sandbox = alpaca_paper
        if alpaca_paper:
            place_orders, real_money = True, False
        else:  # Alpaca LIVE (real money) still requires the two-key tripwire.
            place_orders, real_money = two_key_live, two_key_live
        api_key = os.getenv("ALPACA_API_KEY", "")
        api_secret = os.getenv("ALPACA_API_SECRET", "")
        default_symbol = "BTC/USD"          # Alpaca crypto is quoted in USD
    else:
        # Binance.US: no sandbox. Internal simulation unless the tripwire is on.
        use_sandbox = False
        place_orders, real_money = two_key_live, two_key_live
        api_key = os.getenv("BINANCE_API_KEY", "")
        api_secret = os.getenv("BINANCE_API_SECRET", "")
        default_symbol = cfg["market"]["symbol"]

    cfg["market"]["symbol"] = os.getenv("SYMBOL", default_symbol)

    # --- Dynamic multi-asset universe ---
    quote = "USD" if is_alpaca else "USDT"          # quote currency per venue
    cfg["quote_ccy"] = quote
    cfg.setdefault("universe", {})
    bases_env = os.getenv("SYMBOLS")                 # e.g. "BTC,ETH,SOL"
    if bases_env:
        bases = [b.strip().upper() for b in bases_env.split(",") if b.strip()]
    else:
        bases = [str(b).upper() for b in cfg["universe"].get("bases", ["BTC"])]
    cfg["universe"]["bases"] = bases
    cfg["universe_symbols"] = [f"{b}/{quote}" for b in bases]
    cfg.setdefault("portfolio", {"max_concurrent_positions": 3,
                                 "max_total_exposure_pct": 0.90,
                                 "per_asset_alloc_pct": 0.30})

    # Allocation mode (env override flips it without editing YAML).
    cfg["strategy"].setdefault("allocation", {"mode": "first_come"})
    alloc_env = os.getenv("ALLOCATION_MODE")
    if alloc_env:
        cfg["strategy"]["allocation"]["mode"] = alloc_env.strip().lower()

    # --- Enhancement toggles: present-by-default sections + "flip-it-fast" env vars.
    # Every new feature is OFF by default, so these setdefaults keep behaviour
    # identical for existing configs while letting env vars flip features without
    # editing YAML (mirrors ALLOCATION_MODE above).
    cfg["strategy"].setdefault("regime", {"enabled": False})
    cfg["strategy"].setdefault("profit_taking", {"enabled": False})
    cfg["risk"].setdefault("risk_budget", {"enabled": False})
    if os.getenv("REGIME_ENABLED") is not None:
        cfg["strategy"]["regime"]["enabled"] = _env_bool("REGIME_ENABLED", False)
    if os.getenv("REGIME_METHOD"):
        cfg["strategy"]["regime"]["method"] = os.getenv("REGIME_METHOD").strip().lower()
    if os.getenv("PROFIT_TAKING_ENABLED") is not None:
        cfg["strategy"]["profit_taking"]["enabled"] = _env_bool("PROFIT_TAKING_ENABLED", False)
    if os.getenv("RISK_BUDGET_ENABLED") is not None:
        cfg["risk"]["risk_budget"]["enabled"] = _env_bool("RISK_BUDGET_ENABLED", False)
    mom_scoring = os.getenv("MOMENTUM_SCORING")
    if mom_scoring:
        cfg["strategy"]["allocation"].setdefault("momentum_rotation", {})
        cfg["strategy"]["allocation"]["momentum_rotation"]["scoring"] = mom_scoring.strip().lower()

    # --- Sleeve overlay + universe-expansion sections (present-by-default, off). --
    cfg.setdefault("portfolio", {})
    cfg["portfolio"].setdefault("sleeves", {"enabled": False})
    cfg.setdefault("liquidity_filters", {})
    cfg["universe"].setdefault("expansion", {"enabled": False, "candidates": [],
                                             "approved_expanded_universe": []})
    if os.getenv("SLEEVES_ENABLED") is not None:
        cfg["portfolio"]["sleeves"]["enabled"] = _env_bool("SLEEVES_ENABLED", False)
    sleeve_mode = os.getenv("SLEEVE_ALLOCATOR_MODE")
    if sleeve_mode:
        cfg["portfolio"]["sleeves"]["allocator_mode"] = sleeve_mode.strip().lower()

    # --- Execution-quality knobs: backward-compatible defaults + fast env flips. --
    ex = cfg.setdefault("execution", {})
    ex.setdefault("use_limit_orders_on_entry", False)   # market orders if YAML predates this
    ex.setdefault("slippage_logging_enabled", True)
    ex.setdefault("cost_preference_mode", "off")
    if os.getenv("USE_LIMIT_ORDERS") is not None:
        ex["use_limit_orders_on_entry"] = _env_bool("USE_LIMIT_ORDERS", False)
    if os.getenv("SLIPPAGE_LOGGING") is not None:
        ex["slippage_logging_enabled"] = _env_bool("SLIPPAGE_LOGGING", True)
    if os.getenv("COST_PREFERENCE_MODE"):
        ex["cost_preference_mode"] = os.getenv("COST_PREFERENCE_MODE").strip().lower()

    # --- Trading-ops agent (analysis + gated proposals; off by default). ---------
    cfg.setdefault("ops_agent", {"enabled": False})
    if os.getenv("OPS_AGENT_ENABLED") is not None:
        cfg["ops_agent"]["enabled"] = _env_bool("OPS_AGENT_ENABLED", False)

    cfg["runtime"] = {
        "paper_trading": paper,
        "live_trading_enabled": live_enabled,
        "exchange_id": exchange_id,
        "alpaca_paper": alpaca_paper,
        "place_orders": place_orders,   # actually send orders to the venue's API
        "real_money": real_money,       # those orders use real funds (warn loudly)
        "use_sandbox": use_sandbox,     # ccxt set_sandbox_mode (Alpaca paper)
        "uses_broker": place_orders,    # read equity from the venue, not internal sim
        "api_key": api_key,
        "api_secret": api_secret,
        "anthropic_api_key": os.getenv("ANTHROPIC_API_KEY", ""),
        "db_path": os.getenv("DB_PATH", "trading_state.db"),
        # Optional Telegram alerts (no-op unless token + chat id are set).
        "telegram_enabled": _env_bool("TELEGRAM_ENABLED", True),
        "telegram_token": os.getenv("TELEGRAM_BOT_TOKEN", ""),
        "telegram_chat_id": os.getenv("TELEGRAM_CHAT_ID", ""),
    }

    # ---- Common overrides ----
    cfg["claude"]["model"] = os.getenv("CLAUDE_MODEL", cfg["claude"]["model"])
    cfg["risk"]["default_capital_usd"] = _env_float(
        "DEFAULT_CAPITAL_USD", cfg["risk"]["default_capital_usd"])
    cfg["risk"]["risk_per_trade_pct"] = _env_float(
        "RISK_PER_TRADE_PCT", cfg["risk"]["risk_per_trade_pct"])
    cfg["safety"]["daily_loss_limit_pct"] = _env_float(
        "DAILY_LOSS_LIMIT_PCT", cfg["safety"]["daily_loss_limit_pct"])
    cfg["safety"]["weekly_loss_limit_pct"] = _env_float(
        "WEEKLY_LOSS_LIMIT_PCT", cfg["safety"]["weekly_loss_limit_pct"])
    cfg["logging"]["level"] = os.getenv("LOG_LEVEL", cfg["logging"]["level"])

    return cfg
