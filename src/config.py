"""
Configuration loader.

Reads config/trading_config.yaml, then applies environment-variable overrides
for secrets and the live-trading switches. All secrets and "flip-it-fast"
settings live in env vars (.env locally, or Railway variables) so nothing
sensitive is ever committed.

A basic user only edits .env. Deeper tuning lives in the YAML.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

load_dotenv()  # load a local .env if present (no-op on Railway)

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

    # ---- Runtime mode + secrets (env only) ----
    paper = _env_bool("PAPER_TRADING", True)
    live_enabled = _env_bool("LIVE_TRADING_ENABLED", False)
    # The two-key tripwire: real orders ONLY when paper is off AND live is on.
    really_live = (not paper) and live_enabled

    cfg["runtime"] = {
        "paper_trading": paper,
        "live_trading_enabled": live_enabled,
        "really_live": really_live,
        # Spot-only refactor defaults to Binance.US.
        "exchange_id": os.getenv("EXCHANGE_ID", "binanceus").strip().lower(),
        "binance_api_key": os.getenv("BINANCE_API_KEY", ""),
        "binance_api_secret": os.getenv("BINANCE_API_SECRET", ""),
        "anthropic_api_key": os.getenv("ANTHROPIC_API_KEY", ""),
        "db_path": os.getenv("DB_PATH", "trading_state.db"),
    }

    # ---- Common overrides ----
    cfg["claude"]["model"] = os.getenv("CLAUDE_MODEL", cfg["claude"]["model"])
    cfg["risk"]["default_capital_usd"] = _env_float(
        "DEFAULT_CAPITAL_USD", cfg["risk"]["default_capital_usd"]
    )
    cfg["risk"]["risk_per_trade_pct"] = _env_float(
        "RISK_PER_TRADE_PCT", cfg["risk"]["risk_per_trade_pct"]
    )
    cfg["safety"]["daily_loss_limit_pct"] = _env_float(
        "DAILY_LOSS_LIMIT_PCT", cfg["safety"]["daily_loss_limit_pct"]
    )
    cfg["safety"]["weekly_loss_limit_pct"] = _env_float(
        "WEEKLY_LOSS_LIMIT_PCT", cfg["safety"]["weekly_loss_limit_pct"]
    )
    cfg["logging"]["level"] = os.getenv("LOG_LEVEL", cfg["logging"]["level"])

    return cfg
