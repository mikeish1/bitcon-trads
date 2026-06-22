"""Concentration cap on the whole-position momentum book (main_loop._trim_to_cap).

Live rotation lets winners RUN (never re-weighted). The cap trims any held name that
grows past cap_mult x (equity/top_k) back to the cap via a partial sell (PnL
conserved through reduce_position), bounding single-name tail risk. Validated in
src/profit_taking_research.py (C2 wp+cap beats uncapped whole-position on return+DD).
"""
from __future__ import annotations

import pytest

from src.main_loop import TradingBot
from src.momentum_allocator import MomentumRotation
from src.risk_manager import RiskManager


class _StubExecutor:
    def __init__(self):
        self.sells: list[dict] = []
        self._seq = 0

    def market_sell(self, symbol, qty, price, reason):
        self._seq += 1
        self.sells.append({"symbol": symbol, "qty": qty, "price": price, "reason": reason})
        return {"id": f"sell-{self._seq}", "qty": qty, "price": price,
                "proceeds": qty * price, "fee": 0.0}

    def cancel(self, symbol, order_id):
        pass

    def place_stop_limit_sell(self, *a):
        return "stop-x"


class _StubNotifier:
    def exit(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass


class _StubData:
    def fetch_balances(self):
        return {}


def _cfg(cap_mult=1.5, top_k=4):
    return {
        "runtime": {"uses_broker": False, "real_money": False, "place_orders": False,
                    "db_path": ":memory:"},
        "market": {"primary_timeframe": "1d"},
        "risk": {"default_capital_usd": 1000.0, "risk_per_trade_pct": 0.01,
                 "max_position_pct": 0.95, "min_notional_usd": 10.0,
                 "kelly_fraction": 0.25, "kelly_assumed_payoff": 2.0},
        "safety": {"daily_loss_limit_pct": 0.03, "weekly_loss_limit_pct": 0.07,
                   "max_consecutive_losses": 4, "cooldown_minutes": 60, "max_trades_per_day": 4},
        "exits": {"atr_stop_mult": 2.0, "min_stop_pct": 0.01, "atr_trail_mult": 2.5,
                  "take_profit_R": 3.0, "stop_limit_offset_pct": 0.003},
        "portfolio": {"max_concurrent_positions": 4, "max_total_exposure_pct": 0.95,
                      "per_asset_alloc_pct": 0.30},
        "strategy": {"donchian": {"atr_trail_mult": 3.0}, "vol_target": {"enabled": False},
                     "profit_taking": {"enabled": False},
                     "allocation": {"momentum_rotation": {
                         "top_k": top_k, "rebalance_days": 2, "lookback_days": 90,
                         "keep_band": 1, "concentration_cap_mult": cap_mult}}},
        "quote_ccy": "USDT",
    }


def _bot(cfg):
    rm = RiskManager(cfg)
    bot = TradingBot.__new__(TradingBot)        # bypass network __init__
    bot.cfg, bot.risk = cfg, rm
    bot.executor, bot.notifier, bot.data = _StubExecutor(), _StubNotifier(), _StubData()
    bot.use_exchange_stop = False
    bot.rotation = MomentumRotation(cfg)
    return bot, rm


def _open(rm, qty, price=100.0):
    fill = {"price": price, "qty": qty, "cost": qty * price, "fee": 0.0}
    rm.record_open("SOL/USDT", fill, price * 0.9, 0.0, None, "rotation",
                   peak_price=price, entry_atr=5.0)


def test_trim_to_cap_reduces_an_overweight_winner():
    bot, rm = _bot(_cfg(cap_mult=1.5, top_k=4))
    _open(rm, qty=6.0)                            # $600 of a $1000 book (60%)
    bot._trim_to_cap({"SOL/USDT": {"price": 100.0, "atr": 5.0, "ts": None}})
    # cap = 1.5 * 1000/4 = $375 -> trim a $600 name down to $375 (qty 6 -> 3.75).
    assert rm.open_position("SOL/USDT")["qty"] == pytest.approx(3.75, rel=1e-6)
    assert len(bot.executor.sells) == 1
    assert bot.executor.sells[0]["qty"] == pytest.approx(2.25, rel=1e-6)


def test_within_cap_is_not_trimmed():
    bot, rm = _bot(_cfg(cap_mult=1.5, top_k=4))
    _open(rm, qty=3.0)                            # $300 = 30% < cap 37.5%
    bot._trim_to_cap({"SOL/USDT": {"price": 100.0, "atr": 5.0, "ts": None}})
    assert rm.open_position("SOL/USDT")["qty"] == pytest.approx(3.0)
    assert bot.executor.sells == []


def test_cap_off_when_unset():
    cfg = _cfg(cap_mult=1.5)
    cfg["strategy"]["allocation"]["momentum_rotation"]["concentration_cap_mult"] = None
    bot, rm = _bot(cfg)
    assert bot.rotation.cap_mult is None          # disabled -> _rotate never calls the trim
