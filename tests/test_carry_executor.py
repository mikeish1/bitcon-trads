"""Unit tests for CarryExecutor: sim fills + live leg-rollback safety."""
from __future__ import annotations

import pytest

from src.carry.executor import CarryExecutor
from tests.conftest import base_cfg


def test_sim_open_is_delta_neutral_with_fees():
    ex = CarryExecutor(base_cfg(), spot=None, perp=None)  # sim needs no exchanges
    pair = ex.open_pair("BTC", "BTC/USD", "BTC/USD:USD", notional=400.0,
                        spot_price=100.0, perp_price=100.0)
    assert pair is not None
    # equal quantities on both legs -> delta-neutral.
    assert pair.spot.qty == pytest.approx(pair.perp.qty, rel=1e-9)
    # slippage is against us on both legs.
    assert pair.spot.price > 100.0 and pair.perp.price < 100.0
    # taker fee applied per leg.
    assert pair.spot.fee == pytest.approx(pair.spot.notional * 0.0005, rel=1e-9)


def test_sim_close_covers_short_and_sells_spot():
    ex = CarryExecutor(base_cfg(), spot=None, perp=None)
    pair = ex.close_pair("BTC", "BTC/USD", "BTC/USD:USD", spot_qty=4.0, perp_qty=4.0,
                         spot_price=100.0, perp_price=100.0)
    assert pair is not None
    assert pair.spot.side == "sell" and pair.perp.side == "buy"


class _Fake:
    """A minimal ccxt-like stub recording orders; can be told to fail a leg."""
    def __init__(self, fail_side: str | None = None):
        self.fail_side = fail_side
        self.calls: list[tuple] = []

    def amount_to_precision(self, symbol, qty):
        return qty

    def create_order(self, symbol, type_, side, qty, price=None, params=None):
        self.calls.append((side, qty))
        if side == self.fail_side:
            raise RuntimeError(f"simulated {side} failure")
        return {"id": f"{side}-1", "filled": qty, "average": price or 100.0,
                "cost": qty * (price or 100.0), "fee": {"cost": 0.0}}


def _live_cfg():
    cfg = base_cfg(mode="live", place=True, real=True)
    return cfg


def test_live_open_rolls_back_spot_when_short_fails():
    spot = _Fake()                 # spot buy + rollback sell both succeed
    perp = _Fake(fail_side="sell")  # the short leg fails
    ex = CarryExecutor(_live_cfg(), spot=spot, perp=perp)
    pair = ex.open_pair("BTC", "BTC/USD", "BTC/USD:USD", notional=400.0,
                        spot_price=100.0, perp_price=100.0)
    assert pair is None
    sides = [c[0] for c in spot.calls]
    assert sides == ["buy", "sell"]   # bought, then rolled the spot back out


def test_sim_leg_methods_return_fills():
    ex = CarryExecutor(base_cfg(), spot=None, perp=None)
    cover = ex.cover_perp("BTC", "BTC/USD:USD", 4.0, 100.0)
    sell = ex.sell_spot("BTC", "BTC/USD", 4.0, 100.0)
    assert cover.side == "buy" and cover.qty == pytest.approx(4.0)
    assert sell.side == "sell" and sell.price < 100.0   # slippage against us


def test_live_single_legs_return_none_on_failure():
    # A failed close leg returns None so the loop can persist progress and retry.
    ex_sell = CarryExecutor(_live_cfg(), spot=_Fake(fail_side="sell"), perp=_Fake())
    assert ex_sell.sell_spot("BTC", "BTC/USD", 4.0, 100.0) is None
    ex_cover = CarryExecutor(_live_cfg(), spot=_Fake(), perp=_Fake(fail_side="buy"))
    assert ex_cover.cover_perp("BTC", "BTC/USD:USD", 4.0, 100.0) is None


def test_live_open_succeeds_when_both_legs_fill():
    spot, perp = _Fake(), _Fake()
    ex = CarryExecutor(_live_cfg(), spot=spot, perp=perp)
    pair = ex.open_pair("BTC", "BTC/USD", "BTC/USD:USD", notional=400.0,
                        spot_price=100.0, perp_price=100.0)
    assert pair is not None
    assert [c[0] for c in spot.calls] == ["buy"]
    assert [c[0] for c in perp.calls] == ["sell"]
