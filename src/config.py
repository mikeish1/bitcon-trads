"""
Configuration loader.

Reads config/trading_config.yaml, then lets environment variables override the
handful of settings a user is most likely to want to change quickly. This keeps
all secrets and "flip-it-fast" settings in env vars (Railway-friendly) while
the bulk of the tuning lives in one readable YAML file.

A basic user should not need to edit this file.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

# Load a local .env if present (no-op on Railway, where vars come from the dashboard).
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
    """Load YAML config and apply environment-variable overrides."""
    with open(_CONFIG_PATH, "r", encoding="utf-8") as fh:
        cfg: dict[str, Any] = yaml.safe_load(fh)

    # ---- Secrets & runtime mode (env only) ----
    cfg["runtime"] = {
        "paper_trading": _env_bool("PAPER_TRADING", True),
        # Which exchange to use: "binance" (binance.com, futures) or
        # "binanceus" (Binance.US, spot - required for US users).
        "exchange_id": os.getenv("EXCHANGE_ID", "binance").strip().lower(),
        "binance_api_key": os.getenv("BINANCE_API_KEY", ""),
        "binance_api_secret": os.getenv("BINANCE_API_SECRET", ""),
        "binance_testnet": _env_bool("BINANCE_TESTNET", True),
        "anthropic_api_key": os.getenv("ANTHROPIC_API_KEY", ""),
        "db_path": os.getenv("DB_PATH", "trading_state.db"),
    }

    # ---- Optional overrides for common knobs ----
    cfg["claude"]["model"] = os.getenv("CLAUDE_MODEL", cfg["claude"]["model"])
    cfg["risk"]["starting_capital_usd"] = _env_float(
        "STARTING_CAPITAL_USD", cfg["risk"]["starting_capital_usd"]
    )
    cfg["risk"]["max_risk_per_trade"] = _env_float(
        "MAX_RISK_PER_TRADE", cfg["risk"]["max_risk_per_trade"]
    )
    cfg["risk"]["kelly_fraction"] = _env_float(
        "KELLY_FRACTION", cfg["risk"]["kelly_fraction"]
    )
    cfg["safety"]["daily_loss_limit_pct"] = _env_float(
        "DAILY_LOSS_LIMIT_PCT", cfg["safety"]["daily_loss_limit_pct"]
    )
    cfg["safety"]["weekly_loss_limit_pct"] = _env_float(
        "WEEKLY_LOSS_LIMIT_PCT", cfg["safety"]["weekly_loss_limit_pct"]
    )
    cfg["logging"]["level"] = os.getenv("LOG_LEVEL", cfg["logging"]["level"])

    return cfg
