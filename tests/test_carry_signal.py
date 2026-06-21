"""Unit tests for the pure carry signal + its fee/annualisation math."""
from __future__ import annotations

import pytest

from src.carry.config_carry import build_carry_params
from src.carry.signal import annualize_funding, evaluate, fee_drag_apr, net_carry_apr
from src.carry.types import FundingQuote
from tests.conftest import base_cfg


@pytest.fixture
def params():
    return build_carry_params(base_cfg())


def _quote(funding_apr: float, *, basis_bps: float = 0.0, age: float = 5.0) -> FundingQuote:
    return FundingQuote(asset="BTC", funding_rate=funding_apr / 1095.0,
                        funding_apr=funding_apr, spot=100.0, perp=100.0,
                        basis_bps=basis_bps, age_seconds=age)


def test_annualize_funding_8h():
    # 1bp per 8h -> ~10.95%/yr (1095 intervals).
    assert annualize_funding(0.0001, 8) == pytest.approx(0.1095, rel=1e-3)


def test_fee_drag_amortises_over_hold():
    # 0.40% round trip held 30 days -> ~4.87%/yr drag.
    assert fee_drag_apr(0.004, 30) == pytest.approx(0.004 * 365 / 30, rel=1e-9)


def test_net_is_gross_minus_drag(params):
    gross = 0.20
    assert net_carry_apr(gross, params) == pytest.approx(gross - fee_drag_apr(
        params.roundtrip_cost_frac, params.expected_hold_days))


def test_open_when_net_clears_entry(params):
    d = evaluate(_quote(0.20), held=False, low_reads=0, params=params)
    assert d.action == "OPEN" and d.net_apr >= params.min_entry_apr


def test_skip_when_thin(params):
    d = evaluate(_quote(0.10), held=False, low_reads=0, params=params)  # net ~0.05 < 0.08
    assert d.action == "SKIP"


def test_skip_when_basis_too_wide(params):
    d = evaluate(_quote(0.20, basis_bps=200), held=False, low_reads=0, params=params)
    assert d.action == "SKIP" and "basis" in d.reason


def test_stale_feed_always_skips(params):
    d = evaluate(_quote(0.20, age=10_000), held=False, low_reads=0, params=params)
    assert d.action == "SKIP" and "stale" in d.reason


def test_held_unwinds_only_after_confirm_reads(params):
    q = _quote(-0.05)  # clearly below the unwind band (-1%) -> counts toward unwind
    low = 0
    actions = []
    for _ in range(params.flip_confirm_reads):
        d = evaluate(q, held=True, low_reads=low, params=params)
        actions.append(d.action)
        low = d.low_reads
    assert actions[:-1] == ["HOLD"] * (params.flip_confirm_reads - 1)
    assert actions[-1] == "UNWIND"


def test_tolerance_band_holds_without_counting(params):
    # Funding soft but inside [unwind_apr, min_hold_apr): hold forever, never count.
    q = _quote(0.005)               # +0.5%/yr, in the band
    low = 0
    for _ in range(params.flip_confirm_reads + 2):
        d = evaluate(q, held=True, low_reads=low, params=params)
        assert d.action == "HOLD"
        low = d.low_reads
    assert low == 0                 # counter never advanced -> no churn


def test_mildly_negative_funding_is_tolerated(params):
    # Slightly negative but above unwind_apr (-1%): still tolerated, no unwind.
    d = evaluate(_quote(-0.005), held=True, low_reads=0, params=params)
    assert d.action == "HOLD" and "tolerance band" in d.reason


def test_held_healthy_resets_counter(params):
    d = evaluate(_quote(0.20), held=True, low_reads=2, params=params)
    assert d.action == "HOLD" and d.low_reads == 0
