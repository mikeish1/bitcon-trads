"""Shared test fixtures + repo-root path wiring for the carry test suite."""
from __future__ import annotations

import os
import sys
from typing import Any

import pytest

# Ensure `import src.*` resolves when pytest runs from anywhere.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def base_cfg(*, db_path: str = ":memory:", mode: str = "sim",
             place: bool = False, real: bool = False) -> dict[str, Any]:
    """A minimal but complete cfg dict for carry components (no network needed)."""
    return {
        "runtime": {
            "db_path": db_path,
            "telegram_enabled": False, "telegram_token": "", "telegram_chat_id": "",
        },
        "logging": {"level": "INFO"},
        "carry": {
            "assets": ["BTC", "ETH", "SOL"],
            "venues": {"spot": "kraken", "perp": "krakenfutures"},
            "poll_seconds": 900,
            "funding_interval_hours": 8,
            "signal": {"min_entry_apr": 0.08, "min_hold_apr": 0.02, "unwind_apr": -0.01,
                       "flip_confirm_reads": 3, "funding_lookback": 9, "max_basis_bps": 75,
                       "expected_hold_days": 30},
            "capital": {"sleeve_usd": 1000.0, "per_asset_cap_usd": 400.0, "min_notional_usd": 25.0},
            "risk": {"target_leverage": 1.0, "max_leverage": 2.0, "margin_alert_ratio": 0.40,
                     "delta_tolerance_pct": 0.03, "daily_loss_limit_usd": 50.0,
                     "max_feed_staleness_seconds": 120},
            "execution": {"mode": mode, "taker_fee_pct": 0.0005, "paper_slippage_pct": 0.0005},
        },
        "carry_runtime": {
            "mode": mode, "real_money": real, "place_orders": place,
            "spot_id": "kraken", "perp_id": "krakenfutures",
            "spot_key": "", "spot_secret": "", "perp_key": "", "perp_secret": "",
        },
    }


@pytest.fixture
def cfg() -> dict[str, Any]:
    return base_cfg()


# --------------------------------------------------------------------------- #
# ETF momentum helpers                                                        #
# --------------------------------------------------------------------------- #
def etf_cfg(*, db_path: str = ":memory:", mode: str = "sim", place: bool = False,
            real: bool = False, top_k: int = 2, rebalance_days: int = 5,
            lookback_days: int = 20, entry_period: int = 20, min_history: int = 30,
            sleeve: float = 2000.0, universe: list[str] | None = None) -> dict[str, Any]:
    return {
        "runtime": {"db_path": db_path, "telegram_enabled": False,
                    "telegram_token": "", "telegram_chat_id": ""},
        "logging": {"level": "INFO"},
        "etf": {
            "enabled": False, "venue": "alpaca", "primary_timeframe": "1d", "backfill_days": 400,
            "universe": universe or ["AAA", "BBB", "CCC"], "poll_seconds": 3600,
            "selection": {"entry_period": entry_period, "atr_trail_mult": 3.0,
                          "min_history": min_history, "top_k": top_k,
                          "rebalance_days": rebalance_days, "lookback_days": lookback_days,
                          "keep_band": 1},
            "capital": {"sleeve_usd": sleeve, "max_total_exposure_pct": 0.95, "min_notional_usd": 10.0},
            "execution": {"mode": mode, "taker_fee_pct": 0.0, "paper_slippage_pct": 0.0005},
        },
        "etf_runtime": {"mode": mode, "real_money": real, "place_orders": place,
                        "alpaca_paper": True, "venue": "alpaca", "quote": "USD",
                        "api_key": "", "api_secret": ""},
    }


def make_bars(closes: list[float], start: str = "2024-01-01"):
    """Build a daily OHLCV frame with indicators (reuses the crypto indicator
    builder so `atr` etc. exist for the Donchian trend filter)."""
    import pandas as pd
    from src.data_pipeline import DataPipeline
    ts = pd.date_range(start=start, periods=len(closes), freq="D", tz="UTC")
    df = pd.DataFrame({
        "timestamp": ts,
        "open": closes,
        "high": [c * 1.003 for c in closes],
        "low": [c * 0.997 for c in closes],
        "close": closes,
        "volume": [1_000_000.0] * len(closes),
    })
    return DataPipeline.add_indicators(df)
