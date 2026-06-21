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
