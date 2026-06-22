"""Limit-order entries with timeout + market fallback (SpotExecutor.open_buy).

SIM path: models the limit filling at the limit price (price improvement vs the
market-slippage path). LIVE path (fake exchange): immediate fill, fill-on-poll, and
no-fill -> cancel + market fallback."""
from __future__ import annotations

import pytest

from src.executor import SpotExecutor


def _cfg(db_path, *, place=False, use_limit=True, offset=0.0, post_only=False,
         slip=0.0007, timeout=0.2, poll=0.05, fill_model="optimistic", fill_ratio=1.0,
         chase_bps=0.0):
    return {
        "runtime": {"place_orders": place, "real_money": False, "db_path": db_path},
        "execution": {"taker_fee_pct": 0.001, "maker_fee_pct": 0.0005,
                      "paper_slippage_pct": slip, "use_limit_orders_on_entry": use_limit,
                      "entry_limit_offset_bps": offset, "limit_order_timeout_sec": timeout,
                      "limit_poll_interval_sec": poll, "post_only": post_only,
                      "slippage_logging_enabled": True, "max_slippage_tolerance_bps": 50,
                      "paper_limit_fill_model": fill_model, "paper_limit_fill_ratio": fill_ratio,
                      "max_entry_chase_bps": chase_bps},
        "risk": {"min_notional_usd": 10.0},
    }


class FakeExchange:
    """Minimal ccxt-like stub for the live limit path."""
    def __init__(self, mode="immediate", mkt_price=100.0, ask=None):
        self.mode = mode            # "immediate" | "on_poll" | "never"
        self.mkt_price = mkt_price
        self.ask = ask if ask is not None else mkt_price
        self.cancelled = []
        self.market_orders = []

    def fetch_ticker(self, symbol):
        return {"ask": self.ask, "bid": self.ask * 0.999, "last": self.ask}

    def amount_to_precision(self, s, q):
        return float(q)

    def price_to_precision(self, s, p):
        return float(p)

    def market(self, s):
        return {"limits": {"cost": {"min": None}}}

    def create_order(self, symbol, otype, side, qty, price, params):
        self._qty, self._price = qty, price
        filled = qty if self.mode == "immediate" else 0.0
        status = "closed" if self.mode == "immediate" else "open"
        return {"id": "lim1", "filled": filled, "status": status,
                "cost": filled * price, "average": price}

    def fetch_order(self, oid, symbol):
        filled = self._qty if self.mode == "on_poll" else 0.0
        status = "closed" if self.mode == "on_poll" else "open"
        return {"id": oid, "filled": filled, "status": status,
                "cost": filled * self._price, "average": self._price}

    def create_market_buy_order(self, symbol, qty):
        self.market_orders.append(qty)
        return {"id": "mkt1", "filled": qty, "cost": qty * self.mkt_price, "average": self.mkt_price}

    def cancel_order(self, oid, symbol):
        self.cancelled.append(oid)


# --------------------------- SIM path --------------------------------------- #
def test_sim_limit_fills_at_limit_price_no_slippage(tmp_path):
    ex = SpotExecutor(_cfg(str(tmp_path / "t.db")), exchange=None)
    fill = ex.open_buy("BTC/USD", 1000.0, 100.0, intended_price=100.0)
    assert fill["order_type"] == "limit"
    assert fill["price"] == pytest.approx(100.0)          # offset 0 -> at signal price
    assert fill["slippage_bps"] == pytest.approx(0.0)


def test_sim_market_buy_pays_slippage(tmp_path):
    ex = SpotExecutor(_cfg(str(tmp_path / "t.db")), exchange=None)
    fill = ex.market_buy("BTC/USD", 1000.0, 100.0, intended_price=100.0)
    assert fill["price"] == pytest.approx(100.0 * 1.0007)
    assert fill["slippage_bps"] == pytest.approx(7.0, abs=0.05)   # +7 bps adverse


def test_limit_beats_market_on_slippage(tmp_path):
    ex = SpotExecutor(_cfg(str(tmp_path / "t.db")), exchange=None)
    lim = ex.open_buy("BTC/USD", 1000.0, 100.0)
    mkt = ex.market_buy("BTC/USD", 1000.0, 100.0)
    assert lim["slippage_bps"] < mkt["slippage_bps"]


def test_passive_offset_gives_price_improvement(tmp_path):
    ex = SpotExecutor(_cfg(str(tmp_path / "t.db"), offset=-10.0), exchange=None)
    fill = ex.open_buy("BTC/USD", 1000.0, 100.0, intended_price=100.0)
    assert fill["price"] == pytest.approx(99.9)
    assert fill["slippage_bps"] == pytest.approx(-10.0, abs=0.05)   # favorable


def test_use_limit_false_delegates_to_market(tmp_path):
    ex = SpotExecutor(_cfg(str(tmp_path / "t.db"), use_limit=False), exchange=None)
    fill = ex.open_buy("BTC/USD", 1000.0, 100.0)
    assert fill["order_type"] == "market"
    assert fill["price"] == pytest.approx(100.0 * 1.0007)


def test_sell_slippage_is_adverse_positive(tmp_path):
    ex = SpotExecutor(_cfg(str(tmp_path / "t.db")), exchange=None)
    fill = ex.market_sell("BTC/USD", 5.0, 100.0, "exit", intended_price=100.0)
    assert fill["slippage_bps"] == pytest.approx(7.0, abs=0.05)     # received less -> adverse


# --------------------------- LIVE path -------------------------------------- #
def test_live_limit_immediate_fill(tmp_path):
    fx = FakeExchange(mode="immediate")
    ex = SpotExecutor(_cfg(str(tmp_path / "t.db"), place=True), exchange=fx)
    fill = ex.open_buy("BTC/USD", 1000.0, 100.0, intended_price=100.0)
    assert fill["order_type"] == "limit"
    assert fill["qty"] == pytest.approx(10.0)
    assert fx.market_orders == []                         # no fallback needed


def test_live_limit_fills_on_poll(tmp_path):
    fx = FakeExchange(mode="on_poll")
    ex = SpotExecutor(_cfg(str(tmp_path / "t.db"), place=True), exchange=fx)
    fill = ex.open_buy("BTC/USD", 1000.0, 100.0)
    assert fill["order_type"] == "limit"
    assert fill["qty"] == pytest.approx(10.0)


def test_live_no_fill_cancels_and_market_fallback(tmp_path):
    fx = FakeExchange(mode="never", mkt_price=100.5)
    ex = SpotExecutor(_cfg(str(tmp_path / "t.db"), place=True), exchange=fx)
    fill = ex.open_buy("BTC/USD", 1000.0, 100.0, intended_price=100.0)
    assert fx.cancelled == ["lim1"]                       # resting order cancelled
    assert fx.market_orders                               # fell back to market
    assert fill["order_type"] == "market_fallback"
    assert fill["price"] == pytest.approx(100.5)


# ----------------- Realistic fill model + chase guard ----------------------- #
def test_sim_realistic_partial_then_market_fallback(tmp_path):
    ex = SpotExecutor(_cfg(str(tmp_path / "t.db"), fill_model="realistic", fill_ratio=0.7),
                      exchange=None)
    fill = ex.open_buy("BTC/USD", 1000.0, 100.0, intended_price=100.0)
    assert fill["order_type"] == "limit+market"           # 70% limit + 30% market
    assert fill["qty"] == pytest.approx(10.0)             # fully filled overall
    assert 100.0 < fill["price"] < 100.0 * 1.0007         # blended above limit, below market


def test_sim_chase_guard_abandons_runaway_remainder(tmp_path):
    # 30% remainder would market-fill at +7 bps, beyond a 1 bps chase tolerance.
    ex = SpotExecutor(_cfg(str(tmp_path / "t.db"), fill_model="realistic", fill_ratio=0.7,
                           chase_bps=1.0), exchange=None)
    fill = ex.open_buy("BTC/USD", 1000.0, 100.0, intended_price=100.0)
    assert fill["order_type"] == "limit"                  # only the limit portion filled
    assert fill["qty"] == pytest.approx(7.0)             # 70% of 10 units; remainder abandoned


def test_live_chase_guard_skips_runaway_breakout(tmp_path):
    # Limit never fills; fresh ask is 2% above the signal -> beyond a 10 bps chase cap.
    fx = FakeExchange(mode="never", mkt_price=102.0, ask=102.0)
    ex = SpotExecutor(_cfg(str(tmp_path / "t.db"), place=True, chase_bps=10.0), exchange=fx)
    fill = ex.open_buy("BTC/USD", 1000.0, 100.0, intended_price=100.0)
    assert fx.cancelled == ["lim1"]                       # limit cancelled
    assert fx.market_orders == []                         # did NOT chase
    assert fill is None                                   # entry skipped
