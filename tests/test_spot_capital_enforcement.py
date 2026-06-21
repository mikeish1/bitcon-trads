"""Integration: the spot RiskManager enforces the deployable-capital limit at
sizing time, and the limit is user-adjustable without touching sizing code.

Covers the concurrent-signals case: as positions are committed within one cycle
the remaining envelope shrinks, so a tight cap stops further deployment."""
from __future__ import annotations

import pytest

from src.risk_manager import RiskManager


def _cfg(*, capital_policy=None, default_capital=1000.0):
    cfg = {
        "runtime": {"uses_broker": False, "real_money": False, "db_path": ":memory:"},
        "risk": {"default_capital_usd": default_capital, "risk_per_trade_pct": 0.01,
                 "max_position_pct": 0.95, "min_notional_usd": 10.0,
                 "kelly_fraction": 0.25, "kelly_assumed_payoff": 2.0},
        "safety": {"daily_loss_limit_pct": 0.03, "weekly_loss_limit_pct": 0.07,
                   "max_consecutive_losses": 4, "cooldown_minutes": 60, "max_trades_per_day": 4},
        "exits": {"atr_stop_mult": 2.0, "min_stop_pct": 0.01, "atr_trail_mult": 2.5,
                  "take_profit_R": 3.0, "stop_limit_offset_pct": 0.003},
        "portfolio": {"max_concurrent_positions": 3, "max_total_exposure_pct": 0.90,
                      "per_asset_alloc_pct": 0.95},
        "strategy": {"donchian": {"atr_trail_mult": 3.0}, "vol_target": {"enabled": False}},
        "quote_ccy": "USD",
    }
    if capital_policy is not None:
        cfg["capital_policy"] = capital_policy
    return cfg


def test_legacy_default_envelope_unchanged():
    rm = RiskManager(_cfg())
    # 90% of 1000 equity = 900 envelope; per-asset cap 95% and cash 95% are looser.
    s = rm.size_for_asset(equity=1000.0, available_quote=1000.0, open_value=0.0)
    assert s["spend_usd"] == pytest.approx(900.0)


def test_fixed_usd_cap_binds_below_cash_and_pct():
    rm = RiskManager(_cfg(capital_policy={"spot": {"max_usd": 250.0, "max_pct": 0.90,
                                                   "basis": "equity", "precedence": "min"}}))
    # User caps deployable capital at $250 though equity/cash are $1000.
    s = rm.size_for_asset(equity=1000.0, available_quote=1000.0, open_value=0.0)
    assert s["spend_usd"] == pytest.approx(250.0)


def test_concurrent_signals_exhaust_the_envelope():
    rm = RiskManager(_cfg(capital_policy={"spot": {"max_usd": 250.0, "basis": "equity"}}))
    # First signal can take up to the $250 envelope...
    first = rm.size_for_asset(1000.0, 1000.0, open_value=0.0)
    assert first["spend_usd"] == pytest.approx(250.0)
    # ...after $250 is committed, a second concurrent signal gets nothing viable.
    second = rm.size_for_asset(1000.0, 750.0, open_value=250.0)
    assert second["spend_usd"] == pytest.approx(0.0)
    assert second["viable"] is False


def test_zero_capital_not_viable():
    rm = RiskManager(_cfg(default_capital=0.0))
    s = rm.size_for_asset(equity=0.0, available_quote=0.0, open_value=0.0)
    assert s["viable"] is False


def test_rotation_sizing_also_capped():
    rm = RiskManager(_cfg(capital_policy={"spot": {"max_usd": 400.0, "basis": "equity"}}))
    # Equal-weight 1/2 of $1000 = $500, but the $400 envelope caps it.
    s = rm.size_rotation(equity=1000.0, available_quote=1000.0, open_value=0.0, top_k=2)
    assert s["spend_usd"] == pytest.approx(400.0)
