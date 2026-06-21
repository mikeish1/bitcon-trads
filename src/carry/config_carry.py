"""
Carry config + exchange wiring.

Reuses the base loader (src.config.load_config) so the two-key live tripwire and
all the existing env conventions apply unchanged, then layers the `carry:` block,
carry-specific env overrides, and a `carry_runtime` sub-dict on top.

Run mode resolution (mirrors the spot bot's three execution tiers):
  * SIM  - simulate fills against LIVE funding/prices, send nothing  (default)
  * LIVE - real orders, real money. Requires ALL of:
             CARRY_ENABLED=true  AND  PAPER_TRADING=false  AND
             LIVE_TRADING_ENABLED=true  AND  carry.execution.mode == "live"
Anything short of that hard-falls back to SIM.
"""
from __future__ import annotations

import os
from typing import Any

import ccxt
from loguru import logger

from src.config import _env_bool, _env_float, load_config
from .types import CarryParams


def load_carry_config() -> dict[str, Any]:
    """Return the full cfg dict with a resolved `carry` + `carry_runtime` block."""
    cfg = load_config()
    cfg.setdefault("carry", {})
    c = cfg["carry"]

    # --- env overrides (flip fast without editing YAML) ---
    c["enabled"] = _env_bool("CARRY_ENABLED", bool(c.get("enabled", False)))
    assets_env = os.getenv("CARRY_ASSETS")
    if assets_env:
        c["assets"] = [a.strip().upper() for a in assets_env.split(",") if a.strip()]
    c.setdefault("assets", ["BTC", "ETH", "SOL"])
    c.setdefault("venues", {"spot": "kraken", "perp": "krakenfutures"})
    c.setdefault("poll_seconds", 900)
    c.setdefault("funding_interval_hours", 8)

    sig = c.setdefault("signal", {})
    sig["min_entry_apr"] = _env_float("CARRY_MIN_ENTRY_APR", sig.get("min_entry_apr", 0.08))
    sig.setdefault("min_hold_apr", 0.02)
    sig.setdefault("unwind_apr", -0.01)
    sig.setdefault("flip_confirm_reads", 3)
    sig.setdefault("funding_lookback", 9)
    sig.setdefault("max_basis_bps", 75)
    sig.setdefault("expected_hold_days", 30)

    cap = c.setdefault("capital", {})
    cap["sleeve_usd"] = _env_float("CARRY_SLEEVE_USD", cap.get("sleeve_usd", 1000.0))
    cap.setdefault("per_asset_cap_usd", 400.0)
    cap.setdefault("min_notional_usd", 25.0)

    rsk = c.setdefault("risk", {})
    rsk.setdefault("target_leverage", 1.0)
    rsk.setdefault("max_leverage", 2.0)
    rsk.setdefault("margin_alert_ratio", 0.40)
    rsk.setdefault("delta_tolerance_pct", 0.03)
    rsk.setdefault("daily_loss_limit_usd", 50.0)
    rsk.setdefault("max_feed_staleness_seconds", 120)

    ex = c.setdefault("execution", {})
    mode = os.getenv("CARRY_EXECUTION_MODE", ex.get("mode", "sim")).strip().lower()
    ex["mode"] = mode
    ex.setdefault("taker_fee_pct", 0.0005)
    ex.setdefault("paper_slippage_pct", 0.0005)

    # --- resolve the live tripwire ---
    base_rt = cfg["runtime"]
    two_key_live = (not base_rt["paper_trading"]) and base_rt["live_trading_enabled"]
    live = bool(c["enabled"] and two_key_live and mode == "live")

    spot_id = c["venues"]["spot"]
    perp_id = c["venues"]["perp"]
    cfg["carry_runtime"] = {
        "enabled": c["enabled"],
        "mode": "live" if live else "sim",
        "real_money": live,
        "place_orders": live,                       # only send orders when truly live
        "spot_id": spot_id,
        "perp_id": perp_id,
        "spot_key": os.getenv(f"{spot_id.upper()}_API_KEY", ""),
        "spot_secret": os.getenv(f"{spot_id.upper()}_API_SECRET", ""),
        "perp_key": os.getenv(f"{perp_id.upper()}_API_KEY", ""),
        "perp_secret": os.getenv(f"{perp_id.upper()}_API_SECRET", ""),
    }
    return cfg


def build_carry_params(cfg: dict[str, Any]) -> CarryParams:
    """Translate config into the immutable thresholds the signal consumes."""
    c = cfg["carry"]
    sig, ex, rsk = c["signal"], c["execution"], c["risk"]
    roundtrip = 4.0 * (float(ex["taker_fee_pct"]) + float(ex["paper_slippage_pct"]))
    return CarryParams(
        min_entry_apr=float(sig["min_entry_apr"]),
        min_hold_apr=float(sig["min_hold_apr"]),
        unwind_apr=float(sig["unwind_apr"]),
        flip_confirm_reads=int(sig["flip_confirm_reads"]),
        max_basis_bps=float(sig["max_basis_bps"]),
        expected_hold_days=float(sig["expected_hold_days"]),
        funding_interval_hours=float(c["funding_interval_hours"]),
        roundtrip_cost_frac=roundtrip,
        max_feed_staleness_seconds=float(rsk["max_feed_staleness_seconds"]),
    )


def _build_one(exchange_id: str, key: str, secret: str, *, futures: bool) -> ccxt.Exchange:
    params: dict[str, Any] = {"enableRateLimit": True}
    if key:
        params["apiKey"] = key
        params["secret"] = secret
    if futures:
        params["options"] = {"defaultType": "swap"}
    try:
        exchange = getattr(ccxt, exchange_id)(params)
    except AttributeError:
        raise SystemExit(f"Unknown ccxt exchange id '{exchange_id}' in carry.venues.")
    try:
        exchange.load_markets()
    except Exception as exc:  # pragma: no cover - network
        logger.warning("Could not preload {} markets ({}); will retry on demand.",
                       exchange_id, exc)
    return exchange


def build_carry_exchanges(cfg: dict[str, Any]) -> tuple[ccxt.Exchange, ccxt.Exchange]:
    """Build (spot_exchange, perp_exchange) ccxt clients for the carry legs."""
    rt = cfg["carry_runtime"]
    spot = _build_one(rt["spot_id"], rt["spot_key"], rt["spot_secret"], futures=False)
    perp = _build_one(rt["perp_id"], rt["perp_key"], rt["perp_secret"], futures=True)
    return spot, perp
