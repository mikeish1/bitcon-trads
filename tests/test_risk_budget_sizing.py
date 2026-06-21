"""Feature A: explicit per-trade risk budgeting + global vol-target scalar in
RiskManager.size_for_asset / size_rotation.

Verifies the new layers are OFF by default (legacy sizing preserved), that the
ATR-stop risk budget and the clamped vol scalar size as specified, and - crucially
- that the hard caps (deployable-capital envelope, available cash) still bind so no
configuration can raise risk beyond what those caps already allow."""
from __future__ import annotations

import pytest

from src.risk_manager import RiskManager


def _cfg(*, risk_budget=None, capital_policy=None, vol_target=False):
    cfg = {
        "runtime": {"uses_broker": False, "real_money": False, "db_path": ":memory:"},
        "risk": {"default_capital_usd": 1000.0, "risk_per_trade_pct": 0.01,
                 "max_position_pct": 0.95, "min_notional_usd": 10.0,
                 "kelly_fraction": 0.25, "kelly_assumed_payoff": 2.0},
        "safety": {"daily_loss_limit_pct": 0.03, "weekly_loss_limit_pct": 0.07,
                   "max_consecutive_losses": 4, "cooldown_minutes": 60, "max_trades_per_day": 4},
        "exits": {"atr_stop_mult": 2.0, "min_stop_pct": 0.01, "atr_trail_mult": 2.5,
                  "take_profit_R": 3.0, "stop_limit_offset_pct": 0.003},
        "portfolio": {"max_concurrent_positions": 3, "max_total_exposure_pct": 0.90,
                      "per_asset_alloc_pct": 0.30},
        "strategy": {"donchian": {"atr_trail_mult": 3.0},
                     "vol_target": {"enabled": vol_target, "target_daily_vol": 0.04}},
        "quote_ccy": "USD",
    }
    if risk_budget is not None:
        cfg["risk"]["risk_budget"] = risk_budget
    if capital_policy is not None:
        cfg["capital_policy"] = capital_policy
    return cfg


def test_disabled_by_default_matches_legacy():
    rm = RiskManager(_cfg())
    # No risk_budget block -> identical to the legacy per-asset cap (30% of equity).
    s = rm.size_for_asset(1000.0, 1000.0, 0.0, atr_pct=0.05)
    assert s["spend_usd"] == pytest.approx(300.0)
    assert s["risk_notional"] is None
    assert s["vol_scalar"] == 1.0


def test_risk_budget_sizes_from_atr_stop_distance():
    rb = {"enabled": True, "risk_per_trade_pct": 0.0075, "atr_stop_mult": 2.0,
          "target_portfolio_vol": 0.0}  # scalar off for a deterministic check
    rm = RiskManager(_cfg(risk_budget=rb))
    # stop_distance_pct = 0.05*2 = 0.10 ; risk_notional = 1000*0.0075/0.10 = 75.
    s = rm.size_for_asset(1000.0, 1000.0, 0.0, atr_pct=0.05)
    assert s["risk_notional"] == pytest.approx(75.0)
    assert s["spend_usd"] == pytest.approx(75.0)   # binds below the 300 per-asset cap


def test_vol_scalar_shrinks_in_turbulence_and_is_clamped():
    rb = {"enabled": True, "risk_per_trade_pct": 0.0075, "atr_stop_mult": 2.0,
          "target_portfolio_vol": 0.025, "vol_scalar_min": 0.5, "vol_scalar_max": 2.0}
    rm = RiskManager(_cfg(risk_budget=rb))
    # High vol: target/vol = 0.025/0.05 = 0.5 -> base 75 * 0.5 = 37.5.
    hi = rm.size_for_asset(1000.0, 1000.0, 0.0, atr_pct=0.05, portfolio_vol=0.05)
    assert hi["vol_scalar"] == pytest.approx(0.5)
    assert hi["spend_usd"] == pytest.approx(37.5)
    # Calm: target/vol = 0.025/0.0125 = 2.0 (clamped at max) -> 75 * 2 = 150 (< caps).
    lo = rm.size_for_asset(1000.0, 1000.0, 0.0, atr_pct=0.05, portfolio_vol=0.0125)
    assert lo["vol_scalar"] == pytest.approx(2.0)
    assert lo["spend_usd"] == pytest.approx(150.0)


def test_regime_factor_scales_new_size():
    rb = {"enabled": True, "risk_per_trade_pct": 0.0075, "atr_stop_mult": 2.0,
          "target_portfolio_vol": 0.0}
    rm = RiskManager(_cfg(risk_budget=rb))
    s = rm.size_for_asset(1000.0, 1000.0, 0.0, atr_pct=0.05, regime_factor=0.2)
    assert s["spend_usd"] == pytest.approx(15.0)   # 75 * 0.2


def test_hard_cap_still_binds_above_scaled_size():
    # A scale-up regime cannot breach the deployable-capital envelope ($50).
    rb = {"enabled": True, "risk_per_trade_pct": 0.0075, "atr_stop_mult": 2.0,
          "target_portfolio_vol": 0.025, "vol_scalar_max": 2.0}
    rm = RiskManager(_cfg(risk_budget=rb,
                          capital_policy={"spot": {"max_usd": 50.0, "basis": "equity"}}))
    s = rm.size_for_asset(1000.0, 1000.0, 0.0, atr_pct=0.05, portfolio_vol=0.0125)
    assert s["spend_usd"] == pytest.approx(50.0)
    assert s["exposure_budget"] == pytest.approx(50.0)


def test_realized_portfolio_vol_from_equity_curve():
    import datetime as dt
    rb = {"enabled": True, "target_portfolio_vol": 0.025, "vol_source": "realized",
          "vol_lookback_days": 20}
    rm = RiskManager(_cfg(risk_budget=rb))
    # No equity history yet -> realized is undefined, falls back to the ATR proxy.
    assert rm.realized_portfolio_vol() is None
    assert rm.effective_portfolio_vol(0.04) == 0.04
    # Seed a curve whose daily returns alternate +1%/-1% (realized stdev ~= 0.01).
    base = dt.date(2026, 1, 1)
    eq = 1000.0
    for i in range(11):
        d = (base + dt.timedelta(days=i)).isoformat()
        rm.conn.execute("INSERT OR REPLACE INTO equity_history(day, equity, ts) VALUES(?,?,?)",
                        (d, eq, d))
        eq *= 1.01 if i % 2 == 0 else 0.99
    rm.conn.commit()
    rv = rm.realized_portfolio_vol()
    assert rv is not None and 0.005 < rv < 0.02
    # With vol_source="realized", the equity-curve vol overrides the proxy argument.
    assert rm.effective_portfolio_vol(0.04) == pytest.approx(rv)


def test_proxy_source_ignores_equity_curve():
    rm = RiskManager(_cfg(risk_budget={"enabled": True, "vol_source": "proxy"}))
    rm.conn.execute("INSERT OR REPLACE INTO equity_history(day, equity, ts) VALUES('2026-01-01',1000,'x')")
    rm.conn.commit()
    assert rm.effective_portfolio_vol(0.04) == 0.04   # proxy is authoritative


def test_rotation_sizing_respects_vol_scalar_and_regime():
    rb = {"enabled": True, "target_portfolio_vol": 0.025,
          "vol_scalar_min": 0.5, "vol_scalar_max": 2.0}
    rm = RiskManager(_cfg(risk_budget=rb))
    # 1/2 of 1000 = 500, scalar 0.5 -> 250, regime 0.5 -> 125.
    s = rm.size_rotation(1000.0, 1000.0, 0.0, top_k=2, portfolio_vol=0.05, regime_factor=0.5)
    assert s["spend_usd"] == pytest.approx(125.0)
