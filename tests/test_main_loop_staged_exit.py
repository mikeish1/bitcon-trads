"""End-to-end fidelity test for the LIVE staged-exit path (main_loop._manage).

Drives the real _manage / _scale_out / _exit control flow with stub executor +
notifier (no network) to lock in the live-grade behaviour:
  * a tier scale-out places a real partial sell,
  * the resting exchange stop is cancelled and RE-PLACED for the reduced qty,
  * the breakeven floor ratchets the stop up,
  * a later break of that stop exits the remainder, and
  * the final-tranche "dust" path exits fully WITHOUT double-closing.
"""
from __future__ import annotations

import pytest

from src.main_loop import TradingBot
from src.regime import RegimeState
from src.risk_manager import RiskManager


class StubExecutor:
    def __init__(self):
        self.sells, self.cancels, self.stops = [], [], []
        self._seq = 0

    def market_sell(self, symbol, qty, price, reason):
        self._seq += 1
        self.sells.append({"symbol": symbol, "qty": qty, "price": price, "reason": reason})
        return {"id": f"sell-{self._seq}", "qty": qty, "price": price,
                "proceeds": qty * price, "fee": 0.0}

    def cancel(self, symbol, order_id):
        self.cancels.append((symbol, order_id))

    def place_stop_limit_sell(self, symbol, qty, stop, limit):
        self._seq += 1
        sid = f"stop-{self._seq}"
        self.stops.append({"symbol": symbol, "qty": qty, "stop": stop, "id": sid})
        return sid


class StubNotifier:
    def exit(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass


def _cfg(min_notional=10.0):
    return {
        "runtime": {"uses_broker": False, "real_money": False, "place_orders": True,
                    "db_path": ":memory:"},
        "risk": {"default_capital_usd": 10_000.0, "risk_per_trade_pct": 0.01,
                 "max_position_pct": 0.95, "min_notional_usd": min_notional,
                 "kelly_fraction": 0.25, "kelly_assumed_payoff": 2.0},
        "safety": {"daily_loss_limit_pct": 0.03, "weekly_loss_limit_pct": 0.07,
                   "max_consecutive_losses": 4, "cooldown_minutes": 60, "max_trades_per_day": 4},
        "exits": {"atr_stop_mult": 2.0, "min_stop_pct": 0.01, "atr_trail_mult": 2.5,
                  "take_profit_R": 3.0, "stop_limit_offset_pct": 0.003},
        "portfolio": {"max_concurrent_positions": 3, "max_total_exposure_pct": 0.90,
                      "per_asset_alloc_pct": 0.30},
        "strategy": {"donchian": {"atr_trail_mult": 3.0}, "vol_target": {"enabled": False},
                     "profit_taking": {"enabled": True,
                                       "tiers": [{"profit_atr": 1.5, "scale_pct": 0.33},
                                                 {"profit_atr": 3.0, "scale_pct": 0.33}],
                                       "breakeven_after_tier": 1, "breakeven_buffer_atr": 0.5,
                                       "ratchet_trail_mults": [3.0, 2.5, 2.0]}},
        "quote_ccy": "USD",
    }


def _bot(cfg):
    rm = RiskManager(cfg)
    bot = TradingBot.__new__(TradingBot)        # bypass network __init__
    bot.cfg, bot.risk = cfg, rm
    bot.executor, bot.notifier = StubExecutor(), StubNotifier()
    bot.use_exchange_stop = True
    bot.regime_enabled = False
    bot._regime = RegimeState(True, 1.0, 1.0, "disabled", "")
    bot.regime_risk_off_exit, bot.regime_tighten_trail = True, None
    return bot, rm


def _open(rm, stop="stop-0"):
    fill = {"price": 100.0, "qty": 10.0, "cost": 1000.0, "fee": 0.0}
    rm.record_open("BTC/USD", fill, 90.0, 0.0, stop, "test", peak_price=100.0, entry_atr=10.0)


def test_tier_scaleout_replaces_stop_and_lifts_to_breakeven():
    bot, rm = _bot(_cfg())
    _open(rm)
    bot._manage("BTC/USD", price=116.0, atr=10.0, balances={})   # +1.6 ATR -> tier 1

    pos = rm.open_position("BTC/USD")
    assert pos is not None
    assert pos["qty"] == pytest.approx(6.7)            # 33% sold
    assert pos["tranches_done"] == 1
    # A real partial sell of ~3.3 units was placed.
    assert len(bot.executor.sells) == 1
    assert bot.executor.sells[0]["qty"] == pytest.approx(3.3)
    # The resting stop was cancelled and RE-PLACED for the reduced qty.
    assert bot.executor.cancels and bot.executor.stops[-1]["qty"] == pytest.approx(6.7)
    # Stop ratcheted to breakeven+buffer (entry 100 + 0.5*ATR).
    assert pos["current_stop"] == pytest.approx(105.0)


def test_break_of_breakeven_stop_exits_remainder():
    bot, rm = _bot(_cfg())
    _open(rm)
    bot._manage("BTC/USD", price=116.0, atr=10.0, balances={})   # arm breakeven at 105
    bot._manage("BTC/USD", price=104.0, atr=10.0, balances={})   # dips below 105 -> exit

    assert rm.open_position("BTC/USD") is None
    closed = rm.conn.execute("SELECT status, scaled_pnl FROM trades WHERE symbol='BTC/USD'").fetchone()
    assert closed["status"] == "CLOSED"
    assert closed["scaled_pnl"] == pytest.approx(52.8)           # tier-1 gain retained


def test_final_tranche_dust_exits_fully_without_double_close():
    # min_notional high enough that the post-tier remainder (6.7*116=777) is "dust".
    bot, rm = _bot(_cfg(min_notional=800.0))
    _open(rm)
    bot._manage("BTC/USD", price=116.0, atr=10.0, balances={})

    assert rm.open_position("BTC/USD") is None
    rows = rm.conn.execute("SELECT status FROM trades WHERE symbol='BTC/USD'").fetchall()
    assert len(rows) == 1 and rows[0]["status"] == "CLOSED"      # closed exactly once
    # Exactly one sell (the full close), not a partial + a duplicate close.
    assert len(bot.executor.sells) == 1
    assert bot.executor.sells[0]["qty"] == pytest.approx(10.0)
