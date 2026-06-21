"""Tests for EtfRiskManager: equal-weight sizing, exposure cap, paper ledger."""
from __future__ import annotations

import pytest

from src.etf.risk import EtfRiskManager
from tests.conftest import etf_cfg


def _rm(**kw) -> EtfRiskManager:
    return EtfRiskManager(etf_cfg(**kw))


def test_equal_weight_sizing_is_one_over_k():
    rm = _rm(top_k=2, sleeve=2000.0)
    s = rm.size(equity=2000.0, available_cash=2000.0, exposure_used=0.0)
    assert s["spend_usd"] == pytest.approx(1000.0)   # 1/2 of equity
    assert s["viable"] is True


def test_sizing_respects_exposure_cap_and_cash():
    rm = _rm(top_k=2, sleeve=2000.0)
    # already 1000 deployed; budget = 1900 - 1000 = 900 caps the next buy.
    s = rm.size(equity=2000.0, available_cash=1000.0, exposure_used=1000.0)
    assert s["spend_usd"] == pytest.approx(900.0)


def test_paper_ledger_open_then_close_pnl_and_equity():
    rm = _rm(top_k=2, sleeve=2000.0)
    rm.record_open("SPY", {"qty": 10.0, "price": 100.0, "cost": 1000.0, "fee": 0.0}, "entry")
    # equity unchanged right after buy: cash 1000 + holdings 10*100.
    assert rm.current_equity({}, {"SPY": 100.0}) == pytest.approx(2000.0)
    assert rm.held_symbols() == ["SPY"]

    pos = rm.open_position("SPY")
    pnl = rm.record_close(pos, {"qty": 10.0, "price": 110.0, "fee": 0.0}, "exit")
    assert pnl == pytest.approx(100.0)               # +$10 * 10 shares
    assert rm.open_position("SPY") is None
    assert rm.current_equity({}, {}) == pytest.approx(2100.0)   # all cash now


def test_min_notional_blocks_dust():
    rm = _rm(top_k=2, sleeve=2000.0)
    s = rm.size(equity=10.0, available_cash=10.0, exposure_used=0.0)  # 1/2 = $5 < min $10
    assert s["viable"] is False
