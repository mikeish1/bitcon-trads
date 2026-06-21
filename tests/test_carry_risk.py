"""Unit tests for CarryRiskManager: sizing, gates, funding accrual, lifecycle."""
from __future__ import annotations

import time

import pytest

from src.carry.risk import CarryRiskManager, _YEAR_SECONDS
from src.carry.types import Fill, PairFill
from tests.conftest import base_cfg


def _rm(**kw) -> CarryRiskManager:
    return CarryRiskManager(base_cfg(**kw))


def _pair(asset="BTC", price=100.0, qty=4.0, fee=0.2) -> PairFill:
    notional = price * qty
    spot = Fill("spot", "buy", qty, price, notional, fee)
    perp = Fill("perp", "sell", qty, price, notional, fee)
    return PairFill(asset, spot, perp, notional)


def test_size_respects_per_asset_cap_and_leverage():
    rm = _rm()
    s = rm.size(spot_price=100.0)
    # lev 1 -> capital_mult 2 -> remaining/2 = 500, capped at per_asset 400.
    assert s["notional"] == pytest.approx(400.0)
    assert s["capital"] == pytest.approx(800.0)
    assert s["viable"] is True


def test_sleeve_depletes_as_capital_is_used():
    rm = _rm()
    rm.record_open(_pair(), capital_usd=800.0, reason="t")
    s = rm.size(spot_price=100.0)
    # 1000 - 800 = 200 remaining; /2 = 100 notional (below per-asset cap now).
    assert s["notional"] == pytest.approx(100.0)


def test_can_open_blocks_duplicate_and_kill():
    rm = _rm()
    rm.record_open(_pair("BTC"), capital_usd=800.0, reason="t")
    ok, why = rm.can_open("BTC")
    assert not ok and "already" in why
    rm.set_kill(True)
    ok, why = rm.can_open("ETH")
    assert not ok and "kill" in why


def test_funding_accrual_matches_formula():
    rm = _rm()
    rm.record_open(_pair(), capital_usd=800.0, reason="t")
    pos = rm.open_position("BTC")
    start = float(pos["last_accrual_ts"])
    dt = 8 * 3600.0                      # one 8h interval
    amount = rm.accrue_funding(pos, funding_apr=0.10, now=start + dt)
    expected = 400.0 * 0.10 * (dt / _YEAR_SECONDS)
    assert amount == pytest.approx(expected, rel=1e-9)
    assert float(rm.open_position("BTC")["funding_accrued_usd"]) == pytest.approx(expected, rel=1e-9)


def test_unwind_pnl_is_funding_minus_all_fees_when_flat_price():
    rm = _rm()
    rm.record_open(_pair(price=100.0, qty=4.0, fee=0.2), capital_usd=800.0, reason="t")
    pos = rm.open_position("BTC")
    # exit at same price -> delta-neutral price PnL is exactly zero.
    spot_exit = Fill("spot", "sell", 4.0, 100.0, 400.0, 0.2)
    perp_exit = Fill("perp", "buy", 4.0, 100.0, 400.0, 0.2)
    realized = rm.record_unwind(pos, spot_exit, perp_exit, "test")
    # entry fees 0.4 + exit fees 0.4 = 0.8, no funding accrued -> -0.8.
    assert realized == pytest.approx(-0.8, rel=1e-9)
    assert rm.open_position("BTC") is None


def test_unwind_is_delta_neutral_under_price_moves():
    rm = _rm()
    rm.record_open(_pair(price=100.0, qty=4.0, fee=0.0), capital_usd=800.0, reason="t")
    pos = rm.open_position("BTC")
    # price rips +10%: spot +40, short perp -40 => net zero, fees zero -> realized 0.
    spot_exit = Fill("spot", "sell", 4.0, 110.0, 440.0, 0.0)
    perp_exit = Fill("perp", "buy", 4.0, 110.0, 440.0, 0.0)
    realized = rm.record_unwind(pos, spot_exit, perp_exit, "neutral")
    assert realized == pytest.approx(0.0, abs=1e-9)


def test_daily_loss_limit_trips_after_a_losing_close():
    rm = _rm()
    rm.record_open(_pair(price=100.0, qty=4.0, fee=30.0), capital_usd=800.0, reason="t")
    pos = rm.open_position("BTC")
    big_loss_exit_s = Fill("spot", "sell", 4.0, 100.0, 400.0, 30.0)
    big_loss_exit_p = Fill("perp", "buy", 4.0, 100.0, 400.0, 30.0)
    realized = rm.record_unwind(pos, big_loss_exit_s, big_loss_exit_p, "loss")
    assert realized < -rm.daily_loss_limit
    ok, why = rm.can_open("ETH")
    assert not ok and "daily loss" in why


def test_resumable_unwind_persists_each_leg():
    rm = _rm()
    rm.record_open(_pair(price=100.0, qty=4.0, fee=0.1), capital_usd=800.0, reason="t")
    pos = rm.open_position("BTC")
    assert not rm.unwind_in_progress(pos)

    # Close the perp leg only; it must persist and the pair must stay OPEN.
    rm.mark_perp_closed(int(pos["id"]), Fill("perp", "buy", 4.0, 100.0, 400.0, 0.1))
    pos = rm.open_position("BTC")
    assert rm.unwind_in_progress(pos)            # exactly one leg closed
    assert pos is not None

    # Finalising before BOTH legs are closed is a hard error (no silent half-close).
    with pytest.raises(ValueError):
        rm.finalize_unwind(pos, "too early")

    # Close the spot leg, then settle.
    rm.mark_spot_closed(int(pos["id"]), Fill("spot", "sell", 4.0, 100.0, 400.0, 0.1))
    pos = rm.open_position("BTC")
    assert not rm.unwind_in_progress(pos)        # both closed (not "exactly one")
    realized = rm.finalize_unwind(pos, "done")
    assert realized == pytest.approx(-0.4, rel=1e-9)   # entry 0.2 + exit 0.2 fees, flat price
    assert rm.open_position("BTC") is None


def test_delta_breach_detects_qty_mismatch():
    rm = _rm()
    spot = Fill("spot", "buy", 4.0, 100.0, 400.0, 0.0)
    perp = Fill("perp", "sell", 4.2, 100.0, 420.0, 0.0)   # 5% mismatch > 3% tol
    rm.record_open(PairFill("BTC", spot, perp, 400.0), capital_usd=800.0, reason="t")
    assert rm.delta_breach(rm.open_position("BTC")) is True
