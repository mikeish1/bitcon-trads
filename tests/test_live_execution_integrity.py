"""Regression tests for the three live-execution-integrity fixes:

  C1 - executor never books a 0/None fill as a full fill (no phantom positions).
  C2 - reconcile() adopts an untracked broker holding (orphaned fill) WITH a stop,
       and still closes coins that have left the account.
  H1 - signal decisions use the last CONFIRMED-closed daily bar, not the forming one.
"""
from __future__ import annotations

import pandas as pd
import pytest

from src.data_pipeline import DataPipeline
from src.executor import SpotExecutor
from src.risk_manager import RiskManager
from src.strategy import DonchianStrategy


# --------------------------------------------------------------------------- #
# C1: zero / partial / unknown fills                                          #
# --------------------------------------------------------------------------- #
def _exec_cfg(db_path):
    return {
        "runtime": {"place_orders": True, "real_money": False, "db_path": db_path},
        "execution": {"taker_fee_pct": 0.001, "maker_fee_pct": 0.001,
                      "paper_slippage_pct": 0.0007, "use_limit_orders_on_entry": False,
                      "slippage_logging_enabled": False, "max_slippage_tolerance_bps": 50},
        "risk": {"min_notional_usd": 10.0},
    }


class FillExchange:
    """ccxt-like stub whose market orders report a configurable fill."""
    def __init__(self, filled, status="closed", refetch_filled="unset"):
        self.filled = filled
        self.status = status
        self.refetch_filled = refetch_filled

    def amount_to_precision(self, s, q): return float(q)
    def price_to_precision(self, s, p): return float(p)
    def market(self, s): return {"limits": {"cost": {"min": None}}}

    def _order(self, oid):
        cost = None if self.filled is None else self.filled * 100.0
        return {"id": oid, "filled": self.filled, "status": self.status,
                "cost": cost, "average": 100.0}

    def create_market_buy_order(self, symbol, qty): return self._order("b1")
    def create_market_sell_order(self, symbol, qty): return self._order("s1")

    def fetch_order(self, oid, symbol):
        f = None if self.refetch_filled == "unset" else self.refetch_filled
        return {"id": oid, "filled": f, "status": self.status, "cost": None, "average": 100.0}


def test_buy_zero_fill_books_no_position(tmp_path):
    ex = SpotExecutor(_exec_cfg(str(tmp_path / "t.db")), exchange=FillExchange(filled=0))
    assert ex.market_buy("BTC/USD", 1000.0, 100.0, intended_price=100.0) is None


def test_buy_partial_fill_reports_actual_qty(tmp_path):
    # requested 10 units; venue fills only 4 -> we must book 4, never 10.
    ex = SpotExecutor(_exec_cfg(str(tmp_path / "t.db")), exchange=FillExchange(filled=4.0))
    fill = ex.market_buy("BTC/USD", 1000.0, 100.0, intended_price=100.0)
    assert fill is not None and fill["qty"] == pytest.approx(4.0)
    assert fill["cost"] == pytest.approx(400.0)


def test_buy_unknown_fill_resolved_zero_books_nothing(tmp_path):
    # filled=None at first; a re-fetch confirms 0 -> no position.
    ex = SpotExecutor(_exec_cfg(str(tmp_path / "t.db")),
                      exchange=FillExchange(filled=None, status="open", refetch_filled=0.0))
    assert ex.market_buy("BTC/USD", 1000.0, 100.0, intended_price=100.0) is None


def test_sell_zero_fill_keeps_position_open(tmp_path):
    # A rejected exit must NOT return a fill (else the DB would mark it CLOSED).
    ex = SpotExecutor(_exec_cfg(str(tmp_path / "t.db")), exchange=FillExchange(filled=0))
    assert ex.market_sell("BTC/USD", 5.0, 100.0, "exit", intended_price=100.0) is None


# --------------------------------------------------------------------------- #
# C2: reconcile adoption + closing                                            #
# --------------------------------------------------------------------------- #
def _risk_cfg(adopt=True):
    return {
        "runtime": {"uses_broker": True, "real_money": False, "db_path": ":memory:"},
        "risk": {"default_capital_usd": 10_000.0, "risk_per_trade_pct": 0.01,
                 "max_position_pct": 0.95, "min_notional_usd": 10.0,
                 "kelly_fraction": 0.25, "kelly_assumed_payoff": 2.0},
        "safety": {"daily_loss_limit_pct": 0.03, "weekly_loss_limit_pct": 0.07,
                   "max_consecutive_losses": 4, "cooldown_minutes": 60, "max_trades_per_day": 4},
        "exits": {"atr_stop_mult": 2.0, "min_stop_pct": 0.01, "atr_trail_mult": 2.5,
                  "take_profit_R": 3.0, "stop_limit_offset_pct": 0.003},
        "portfolio": {"max_concurrent_positions": 3, "max_total_exposure_pct": 0.90,
                      "per_asset_alloc_pct": 0.30},
        "strategy": {"donchian": {"atr_trail_mult": 3.0}, "vol_target": {"enabled": False}},
        "quote_ccy": "USD",
        "universe_symbols": ["BTC/USD", "ETH/USD"],
        "reconcile": {"adopt_orphans": adopt},
    }


def test_reconcile_adopts_orphaned_holding_with_stop():
    rm = RiskManager(_risk_cfg())
    # Account holds 0.5 BTC the DB never recorded (orphaned fill); USD cash too.
    rm.reconcile({"BTC": 0.5, "USD": 1000.0}, {"BTC": 100.0}, atrs={"BTC": 3.0})
    pos = rm.open_position("BTC/USD")
    assert pos is not None
    assert pos["qty"] == pytest.approx(0.5)
    assert pos["entry_price"] == pytest.approx(100.0)
    # Protective chandelier stop = price - 3.0*ATR (donchian atr_trail_mult=3.0).
    assert pos["current_stop"] == pytest.approx(91.0)
    assert "adopted" in (pos["reason"] or "")


def test_reconcile_ignores_coins_outside_universe():
    rm = RiskManager(_risk_cfg())
    rm.reconcile({"DOGE": 1000.0, "USD": 50.0}, {"DOGE": 0.1}, atrs={})
    assert rm.open_positions() == []     # DOGE not in universe -> never adopted


def test_reconcile_adoption_can_be_disabled():
    rm = RiskManager(_risk_cfg(adopt=False))
    rm.reconcile({"BTC": 0.5, "USD": 1000.0}, {"BTC": 100.0}, atrs={"BTC": 3.0})
    assert rm.open_positions() == []


def test_reconcile_closes_coin_gone_from_account():
    rm = RiskManager(_risk_cfg())
    rm.record_open("BTC/USD", {"price": 100.0, "qty": 0.5, "cost": 50.0, "fee": 0.0},
                   95.0, 0.0, None, "test", peak_price=100.0, entry_atr=3.0)
    # BTC no longer in the account -> the stop filled offline; close the stale row.
    rm.reconcile({"USD": 1000.0}, {"BTC": 100.0}, atrs={"BTC": 3.0})
    assert rm.open_position("BTC/USD") is None
    row = rm.conn.execute("SELECT status FROM trades WHERE symbol='BTC/USD'").fetchone()
    assert row["status"] == "CLOSED"


# --------------------------------------------------------------------------- #
# H1: closed-candle signalling                                                #
# --------------------------------------------------------------------------- #
class TFExchange:
    """Minimal exchange exposing the timeframe helpers signal_frames needs."""
    def __init__(self, now_ms): self._now = now_ms
    def parse_timeframe(self, tf): return 86400          # seconds in 1d
    def milliseconds(self): return self._now


def _daily_df(n, last_open: pd.Timestamp):
    ts = [last_open - pd.Timedelta(days=(n - 1 - i)) for i in range(n)]
    return pd.DataFrame({"timestamp": ts, "open": 100.0, "high": 110.0,
                         "low": 90.0, "close": 100.0, "volume": 1.0, "atr": 2.0})


def _pipeline(now_ms):
    cfg = {"market": {"primary_timeframe": "1d", "confirm_timeframes": [],
                      "backfill_candles": 400, "signal_on_closed_candle": True},
           "quote_ccy": "USD"}
    return DataPipeline(cfg, TFExchange(now_ms))


def test_is_bar_forming_detects_open_bar():
    now = pd.Timestamp("2026-06-23 12:00", tz="UTC")
    dp = _pipeline(int(now.value // 1_000_000))
    today = pd.Timestamp("2026-06-23 00:00", tz="UTC")
    yesterday = pd.Timestamp("2026-06-22 00:00", tz="UTC")
    assert dp.is_bar_forming(_daily_df(5, today), "1d") is True       # last bar still open
    assert dp.is_bar_forming(_daily_df(5, yesterday), "1d") is False  # last bar closed


def test_signal_frames_drops_forming_bar():
    now = pd.Timestamp("2026-06-23 12:00", tz="UTC")
    dp = _pipeline(int(now.value // 1_000_000))
    df = _daily_df(60, pd.Timestamp("2026-06-23 00:00", tz="UTC"))    # last bar forming
    out = dp.signal_frames({"1d": df})
    assert len(out["1d"]) == len(df) - 1                              # forming bar dropped
    # Off -> no truncation.
    dp.cfg["market"]["signal_on_closed_candle"] = False
    assert len(dp.signal_frames({"1d": df})["1d"]) == len(df)


def test_donchian_does_not_fire_on_forming_bar_breakout():
    """A breakout that exists only on the still-forming bar must NOT trigger once the
    forming bar is dropped - this is the whole point of closed-candle signalling."""
    cfg = {"market": {"primary_timeframe": "1d"},
           "strategy": {"donchian": {"entry_period": 40, "min_history": 60, "atr_trail_mult": 3.0}}}
    strat = DonchianStrategy(cfg)
    df = _daily_df(65, pd.Timestamp("2026-06-23 00:00", tz="UTC"))
    df.loc[df.index[-1], "close"] = 115.0     # forming bar pokes above the 110 prior high
    df.loc[df.index[-2], "close"] = 105.0     # last CONFIRMED close did NOT break out

    # Raw frames (forming bar last) -> false breakout fires (documents the bug).
    assert strat.decide({"1d": df}).action == "BUY"
    # Confirmed-close view (forming bar dropped) -> correctly FLAT (the fix).
    assert strat.decide({"1d": df.iloc[:-1].reset_index(drop=True)}).action == "FLAT"
