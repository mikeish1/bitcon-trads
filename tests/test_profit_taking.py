"""Feature C: staged profit-taking / ratcheting exits in RiskManager.

Covers the tier schedule (profit measured in ATR-at-entry), the breakeven floor and
tightening trail multiple, partial-exit bookkeeping (proportional cost basis), and -
the key invariant - that total trade PnL is conserved across scale-outs + final
close (so win/loss + circuit-breaker stats stay correct)."""
from __future__ import annotations

import pytest

from src.risk_manager import RiskManager


def _cfg(*, enabled=True):
    return {
        "runtime": {"uses_broker": False, "real_money": False, "db_path": ":memory:"},
        "risk": {"default_capital_usd": 10_000.0, "risk_per_trade_pct": 0.01,
                 "max_position_pct": 0.95, "min_notional_usd": 10.0,
                 "kelly_fraction": 0.25, "kelly_assumed_payoff": 2.0},
        "safety": {"daily_loss_limit_pct": 0.03, "weekly_loss_limit_pct": 0.07,
                   "max_consecutive_losses": 4, "cooldown_minutes": 60, "max_trades_per_day": 4},
        "exits": {"atr_stop_mult": 2.0, "min_stop_pct": 0.01, "atr_trail_mult": 2.5,
                  "take_profit_R": 3.0, "stop_limit_offset_pct": 0.003},
        "portfolio": {"max_concurrent_positions": 3, "max_total_exposure_pct": 0.90,
                      "per_asset_alloc_pct": 0.30},
        "strategy": {"donchian": {"atr_trail_mult": 3.0},
                     "vol_target": {"enabled": False},
                     "profit_taking": {
                         "enabled": enabled,
                         "tiers": [{"profit_atr": 1.5, "scale_pct": 0.33},
                                   {"profit_atr": 3.0, "scale_pct": 0.33}],
                         "breakeven_after_tier": 1, "breakeven_buffer_atr": 0.5,
                         "ratchet_trail_mults": [3.0, 2.5, 2.0]}},
        "quote_ccy": "USD",
    }


def _open(rm, price=100.0, qty=10.0, atr=10.0):
    fill = {"price": price, "qty": qty, "cost": price * qty, "fee": 0.0}
    tid = rm.record_open("BTC/USD", fill, price - atr, 0.0, None, "test", peak_price=price,
                         entry_atr=atr)
    return rm.open_position("BTC/USD"), tid


def test_plan_no_tier_below_first_threshold():
    rm = RiskManager(_cfg())
    pos, _ = _open(rm)
    plan = rm.profit_taking_plan(pos, price=110.0, atr=10.0)   # +1.0 ATR < 1.5
    assert plan["scale_fraction"] == 0.0
    assert plan["new_tranches"] == 0
    assert plan["trail_mult"] == 3.0
    assert plan["breakeven_floor"] is None


def test_plan_first_tier_fires_with_breakeven_and_tighter_trail():
    rm = RiskManager(_cfg())
    pos, _ = _open(rm)
    plan = rm.profit_taking_plan(pos, price=116.0, atr=10.0)   # +1.6 ATR
    assert plan["scale_fraction"] == pytest.approx(0.33)
    assert plan["new_tranches"] == 1
    assert plan["trail_mult"] == 2.5                            # ratchet[1]
    assert plan["breakeven_floor"] == pytest.approx(105.0)      # entry 100 + 0.5*ATR


def test_plan_both_tiers_fire_from_a_big_gap():
    rm = RiskManager(_cfg())
    pos, _ = _open(rm)
    plan = rm.profit_taking_plan(pos, price=131.0, atr=10.0)   # +3.1 ATR
    assert plan["scale_fraction"] == pytest.approx(0.66)
    assert plan["new_tranches"] == 2
    assert plan["trail_mult"] == 2.0                            # ratchet[2]


def test_disabled_is_a_noop():
    rm = RiskManager(_cfg(enabled=False))
    pos, _ = _open(rm)
    plan = rm.profit_taking_plan(pos, price=200.0, atr=10.0)
    assert plan["scale_fraction"] == 0.0


def test_reduce_position_books_partial_pnl_and_shrinks_basis():
    rm = RiskManager(_cfg())
    pos, _ = _open(rm, price=100.0, qty=10.0)        # cost 1000
    sell = {"qty": 3.3, "price": 116.0, "proceeds": 3.3 * 116.0, "fee": 0.0}
    realized = rm.reduce_position(pos, sell, new_tranches=1, reason="tier1")
    # frac 0.33: sold cost 330, proceeds 382.8 -> realized 52.8.
    assert realized == pytest.approx(52.8)
    after = rm.open_position("BTC/USD")
    assert after["qty"] == pytest.approx(6.7)
    assert after["cost_usd"] == pytest.approx(670.0)
    assert after["tranches_done"] == 1
    assert after["scaled_pnl"] == pytest.approx(52.8)


def test_total_pnl_conserved_across_scaleout_then_close():
    rm = RiskManager(_cfg())
    pos, _ = _open(rm, price=100.0, qty=10.0)        # cost 1000
    sell = {"qty": 3.3, "price": 116.0, "proceeds": 3.3 * 116.0, "fee": 0.0}
    rm.reduce_position(pos, sell, new_tranches=1, reason="tier1")
    pos = rm.open_position("BTC/USD")
    pnl = rm.record_close(pos, exit_price=130.0, exit_fee=0.0, reason="trail")
    # Total = all proceeds (382.8 + 6.7*130=871) - original cost 1000 = 253.8.
    assert pnl == pytest.approx(253.8)
    closed = rm.conn.execute("SELECT status FROM trades WHERE symbol='BTC/USD'").fetchone()
    assert closed["status"] == "CLOSED"
