"""
Integration test: signal -> risk -> (sim) executor, end to end, no network.

Drives one asset through OPEN, funding accrual, then a confirmed UNWIND, asserting
the pair stays delta-neutral and that positive funding shows up as realized PnL.
This is the path the live loop runs, minus the data feed.
"""
from __future__ import annotations

import pytest

from src.carry.config_carry import build_carry_params
from src.carry.executor import CarryExecutor
from src.carry.risk import CarryRiskManager
from src.carry.signal import evaluate
from src.carry.types import FundingQuote
from tests.conftest import base_cfg


def _quote(funding_apr: float, price: float = 100.0) -> FundingQuote:
    return FundingQuote("BTC", funding_apr / 1095.0, funding_apr, price, price, 0.0, 5.0)


def test_full_open_accrue_unwind_cycle():
    cfg = base_cfg()
    params = build_carry_params(cfg)
    risk = CarryRiskManager(cfg)
    execu = CarryExecutor(cfg, spot=None, perp=None)

    # 1) Strong funding -> OPEN.
    q = _quote(0.25)
    decision = evaluate(q, held=False, low_reads=0, params=params)
    assert decision.action == "OPEN"
    sizing = risk.size(q.spot)
    assert sizing["viable"]
    pair = execu.open_pair("BTC", "BTC/USD", "BTC/USD:USD", sizing["notional"], q.spot, q.perp)
    risk.record_open(pair, sizing["capital"], decision.reason)
    assert risk.capital_used() > 0
    # delta-neutral: equal quantities on both legs.
    assert pair.spot.qty == pytest.approx(pair.perp.qty, rel=1e-9)

    # 2) Accrue a couple of funding intervals (positive => income).
    pos = risk.open_position("BTC")
    start = float(pos["last_accrual_ts"])
    risk.accrue_funding(pos, 0.25, now=start + 8 * 3600)
    pos = risk.open_position("BTC")
    risk.accrue_funding(pos, 0.25, now=start + 16 * 3600)
    accrued = float(risk.open_position("BTC")["funding_accrued_usd"])
    assert accrued > 0

    # 3) Funding turns clearly negative (below the tolerance band) -> confirmed
    #    UNWIND after flip_confirm_reads.
    low = 0
    qlow = _quote(-0.05)
    for _ in range(params.flip_confirm_reads):
        d = evaluate(qlow, held=True, low_reads=low, params=params)
        low = d.low_reads
    assert d.action == "UNWIND"

    pos = risk.open_position("BTC")
    exit_pair = execu.close_pair("BTC", "BTC/USD", "BTC/USD:USD", float(pos["spot_qty"]),
                                 float(pos["perp_qty"]), qlow.spot, qlow.perp)
    realized = risk.record_unwind(pos, exit_pair.spot, exit_pair.perp, d.reason)

    # Position closed.
    assert risk.open_position("BTC") is None

    # Faithful end-to-end accounting: realized = both legs' price PnL (the
    # slippage drag) + funding income - all fees (entry + exit), derived from the
    # actual fills the executor produced.
    spot_pnl = (exit_pair.spot.price - pair.spot.price) * pair.spot.qty
    perp_pnl = (pair.perp.price - exit_pair.perp.price) * pair.perp.qty
    total_fees = (pair.spot.fee + pair.perp.fee) + exit_pair.spot.fee + exit_pair.perp.fee
    expected = spot_pnl + perp_pnl - total_fees + accrued
    assert realized == pytest.approx(expected, rel=1e-9)

    # The legs cancel directionally: price PnL is pure slippage, not a bet on price.
    assert spot_pnl + perp_pnl < 0  # slippage always costs us a little
