"""Tests for the centralized DeployableCapitalPolicy.

Covers the safety-critical edge cases called out in the refactor brief: zero
capital, a limit lower than available cash, a limit changing between signals,
fixed-USD vs percentage vs combined caps with precedence, and the validation
invariants (no unbounded limit, ranges)."""
from __future__ import annotations

from decimal import Decimal

import pytest

from src.capital_policy import CapitalPolicyError, DeployableCapitalPolicy


def _pct(p, basis="equity"):
    return DeployableCapitalPolicy.from_mapping({"max_pct": p, "basis": basis}, label="t")


def _usd(u):
    return DeployableCapitalPolicy.from_mapping({"max_usd": u}, label="t")


# --- percentage caps ------------------------------------------------------- #
def test_pct_of_equity_envelope():
    pol = _pct(0.90)
    assert pol.deployable_capital(equity=1000, available_cash=1000) == Decimal("900.0")


def test_pct_of_cash_basis():
    pol = _pct(0.50, basis="cash")
    # basis=cash -> percentage applies to free cash, not total equity.
    assert pol.deployable_capital(equity=1000, available_cash=400) == Decimal("200.0")


def test_remaining_subtracts_committed():
    pol = _pct(0.90)
    # envelope 900, already 600 committed -> 300 headroom.
    assert pol.remaining_capacity(1000, 1000, 600) == Decimal("300.0")


def test_remaining_never_negative_when_over_committed():
    pol = _pct(0.90)
    assert pol.remaining_capacity(1000, 1000, 950) == Decimal("0")


# --- fixed USD caps -------------------------------------------------------- #
def test_fixed_usd_cap():
    pol = _usd(1000)
    assert pol.deployable_capital(equity=999999, available_cash=999999) == Decimal("1000")


def test_limit_lower_than_available_cash_binds():
    # User sets a $250 ceiling but has $5000 cash; only $250 may be deployed.
    pol = _usd(250)
    assert pol.remaining_capacity(equity=5000, available_cash=5000, committed=0) == Decimal("250")


# --- combined caps + precedence ------------------------------------------- #
def test_combined_min_is_default_and_conservative():
    pol = DeployableCapitalPolicy.from_mapping(
        {"max_pct": 0.90, "max_usd": 500, "basis": "equity"})
    # min(900, 500) = 500.
    assert pol.deployable_capital(1000, 1000) == Decimal("500")


def test_combined_max_precedence():
    pol = DeployableCapitalPolicy.from_mapping(
        {"max_pct": 0.90, "max_usd": 500, "precedence": "max"})
    assert pol.deployable_capital(1000, 1000) == Decimal("900.0")


def test_combined_usd_precedence_ignores_pct():
    pol = DeployableCapitalPolicy.from_mapping(
        {"max_pct": 0.10, "max_usd": 500, "precedence": "usd"})
    assert pol.deployable_capital(1000, 1000) == Decimal("500")


def test_combined_pct_precedence_ignores_usd():
    pol = DeployableCapitalPolicy.from_mapping(
        {"max_pct": 0.10, "max_usd": 500, "precedence": "pct"})
    assert pol.deployable_capital(1000, 1000) == Decimal("100.0")


# --- zero / degenerate capital -------------------------------------------- #
def test_zero_equity_yields_zero_envelope():
    assert _pct(0.90).deployable_capital(0, 0) == Decimal("0")


def test_negative_inputs_floored_to_zero():
    # A broker glitch reporting negative equity must never widen the envelope.
    assert _pct(0.90).deployable_capital(-100, -100) == Decimal("0")


def test_clamp_to_cash_prevents_overspend():
    # Envelope 900 but only $200 cash on hand -> clamp keeps us honest.
    pol = _pct(0.90)
    assert pol.remaining_capacity(1000, 200, 0, clamp_to_cash=True) == Decimal("200")


# --- limit changes between signals ---------------------------------------- #
def test_limit_change_between_signals():
    # Signal 1 under a 90% policy, then the user tightens to a $300 cap; the new
    # policy must immediately bind without any code change.
    loose = _pct(0.90)
    assert loose.remaining_capacity(1000, 1000, 0) == Decimal("900.0")
    tight = _usd(300)
    assert tight.remaining_capacity(1000, 1000, 0) == Decimal("300")


# --- validation invariants ------------------------------------------------ #
def test_unbounded_policy_rejected():
    with pytest.raises(CapitalPolicyError) as ei:
        DeployableCapitalPolicy.from_mapping({})
    assert any(e["code"] == "unbounded" for e in ei.value.errors)


def test_pct_out_of_range_rejected():
    with pytest.raises(CapitalPolicyError) as ei:
        DeployableCapitalPolicy.from_mapping({"max_pct": 1.5})
    assert any(e["field"] == "max_pct" for e in ei.value.errors)


def test_negative_usd_rejected():
    with pytest.raises(CapitalPolicyError) as ei:
        DeployableCapitalPolicy.from_mapping({"max_usd": -10})
    assert any(e["code"] == "negative" for e in ei.value.errors)


def test_bad_basis_rejected():
    with pytest.raises(CapitalPolicyError) as ei:
        DeployableCapitalPolicy.from_mapping({"max_pct": 0.5, "basis": "margin"})
    assert any(e["field"] == "basis" for e in ei.value.errors)


def test_non_numeric_rejected():
    with pytest.raises(CapitalPolicyError) as ei:
        DeployableCapitalPolicy.from_mapping({"max_usd": "lots"})
    assert any(e["code"] == "not_a_number" for e in ei.value.errors)


def test_structured_errors_are_machine_readable():
    try:
        DeployableCapitalPolicy.from_mapping({"max_pct": 2, "max_usd": -1, "basis": "x"})
    except CapitalPolicyError as exc:
        fields = {e["field"] for e in exc.errors}
        assert {"max_pct", "max_usd", "basis"} <= fields
        for e in exc.errors:
            assert {"field", "value", "code", "msg"} <= set(e)


# --- serialization --------------------------------------------------------- #
def test_to_public_dict_roundtrips():
    pol = DeployableCapitalPolicy.from_mapping(
        {"max_pct": 0.9, "max_usd": 500, "basis": "equity", "precedence": "min"}, label="spot")
    d = pol.to_public_dict()
    assert d == {"label": "spot", "max_pct": 0.9, "max_usd": 500.0,
                 "basis": "equity", "precedence": "min"}
    # And it can be fed straight back in.
    again = DeployableCapitalPolicy.from_mapping(d, label="spot")
    assert again == pol
