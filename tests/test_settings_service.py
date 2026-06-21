"""Tests for CapitalSettingsService: resolution precedence, persistence across
restarts, structured validation, audit trail, and hot-reload detection."""
from __future__ import annotations

import json

import pytest

from src.capital_policy import DeployableCapitalPolicy
from src.settings_service import CapitalSettingsService


def _cfg():
    """Minimal cfg covering all three sleeves (no network / DB)."""
    return {
        "portfolio": {"max_total_exposure_pct": 0.90},
        "capital_policy": {"spot": {"max_pct": 0.90, "basis": "equity", "precedence": "min"}},
        "carry": {"capital": {"sleeve_usd": 1000.0}},
        "etf": {"capital": {"sleeve_usd": 2000.0, "max_total_exposure_pct": 0.95}},
    }


def _svc(tmp_path):
    return CapitalSettingsService(
        _cfg(),
        override_path=tmp_path / "capital_limits.json",
        audit_path=tmp_path / "audit.log",
    )


# --- resolution / defaults ------------------------------------------------- #
def test_spot_default_matches_legacy(tmp_path):
    svc = _svc(tmp_path)
    mapping, source = svc.resolve_mapping("spot")
    assert source == "yaml"
    assert mapping["max_pct"] == 0.90
    pol = svc.policy("spot")
    assert float(pol.deployable_capital(1000, 1000)) == pytest.approx(900.0)


def test_carry_default_is_fixed_usd_sleeve(tmp_path):
    pol = _svc(tmp_path).policy("carry")
    assert float(pol.deployable_capital(99999, 99999)) == pytest.approx(1000.0)


def test_etf_default_is_pct_only(tmp_path):
    # The sleeve seeds cash; the envelope cap is equity * 0.95 (no USD cap).
    pol = _svc(tmp_path).policy("etf")
    assert float(pol.deployable_capital(2000, 2000)) == pytest.approx(1900.0)


# --- persistence across "restarts" ---------------------------------------- #
def test_update_persists_and_survives_restart(tmp_path):
    svc = _svc(tmp_path)
    res = svc.update({"max_usd": 300.0}, sleeve="spot", actor="tester")
    assert res["ok"] is True
    # A fresh service (simulating a process restart) sees the saved cap.
    svc2 = _svc(tmp_path)
    mapping, source = svc2.resolve_mapping("spot")
    assert source == "override_file"
    assert mapping["max_usd"] == 300.0
    # Combined with the YAML 90% via min -> $300 binds first on a $1000 account.
    assert float(svc2.policy("spot").remaining_capacity(1000, 1000, 0)) == pytest.approx(300.0)


def test_partial_update_keeps_other_fields(tmp_path):
    svc = _svc(tmp_path)
    svc.update({"max_usd": 500.0}, sleeve="spot", actor="t")
    # Only basis changes now; max_usd and max_pct must remain.
    svc.update({"basis": "cash"}, sleeve="spot", actor="t")
    mapping, _ = svc.resolve_mapping("spot")
    assert mapping["max_usd"] == 500.0
    assert mapping["max_pct"] == 0.90
    assert mapping["basis"] == "cash"


def test_clear_a_cap_with_null(tmp_path):
    svc = _svc(tmp_path)
    svc.update({"max_usd": 500.0}, sleeve="spot", actor="t")
    svc.update({"max_usd": None}, sleeve="spot", actor="t")
    mapping, _ = svc.resolve_mapping("spot")
    assert mapping["max_usd"] is None
    assert mapping["max_pct"] == 0.90   # still bounded -> valid


# --- validation ------------------------------------------------------------ #
def test_invalid_update_writes_nothing(tmp_path):
    svc = _svc(tmp_path)
    res = svc.update({"max_pct": 2.0}, sleeve="spot", actor="t")
    assert res["ok"] is False
    assert any(e["field"] == "max_pct" for e in res["errors"])
    assert not (tmp_path / "capital_limits.json").exists()  # nothing persisted


def test_update_cannot_remove_all_caps(tmp_path):
    svc = _svc(tmp_path)
    # Clearing both caps would leave the limit unbounded -> rejected.
    res = svc.update({"max_pct": None, "max_usd": None}, sleeve="spot", actor="t")
    assert res["ok"] is False
    assert any(e["code"] == "unbounded" for e in res["errors"])


# --- env precedence -------------------------------------------------------- #
def test_env_override_shadows_saved_value(tmp_path, monkeypatch):
    svc = _svc(tmp_path)
    svc.update({"max_usd": 300.0}, sleeve="spot", actor="t")
    monkeypatch.setenv("MAX_DEPLOYED_CAPITAL_USD", "150")
    mapping, source = svc.resolve_mapping("spot")
    assert source == "env"
    assert mapping["max_usd"] == "150"  # env value wins
    # And a save while shadowed flags it.
    res = svc.update({"max_usd": 400.0}, sleeve="spot", actor="t")
    assert res["shadowed_by_env"] is True


# --- audit trail ----------------------------------------------------------- #
def test_audit_line_written(tmp_path):
    svc = _svc(tmp_path)
    svc.update({"max_usd": 250.0}, sleeve="spot", actor="alice")
    lines = (tmp_path / "audit.log").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["actor"] == "alice" and rec["sleeve"] == "spot"
    assert rec["after"]["max_usd"] == 250.0


# --- hot reload ------------------------------------------------------------ #
def test_override_change_detected(tmp_path):
    # The bot (reader) and the settings service / frontend (writer) are separate
    # instances, modelling separate processes sharing the override file.
    reader = _svc(tmp_path)
    writer = _svc(tmp_path)
    reader.override_changed_on_disk()       # prime the reader's mtime cache (absent)
    writer.update({"max_usd": 300.0}, sleeve="spot", actor="t")
    assert reader.override_changed_on_disk() is True   # writer changed the file
    assert reader.override_changed_on_disk() is False  # no further change


def test_self_write_does_not_retrigger_reload(tmp_path):
    # A service that writes the file knows about its own change -> no redundant reload.
    svc = _svc(tmp_path)
    svc.override_changed_on_disk()
    svc.update({"max_usd": 300.0}, sleeve="spot", actor="t")
    assert svc.override_changed_on_disk() is False


# --- read API -------------------------------------------------------------- #
def test_get_all_returns_every_sleeve(tmp_path):
    out = _svc(tmp_path).get_all()
    assert set(out) == {"spot", "carry", "etf"}
    assert all(s["ok"] for s in out.values())
    again = DeployableCapitalPolicy.from_mapping(out["spot"]["policy"], label="spot")
    assert again.max_pct is not None
