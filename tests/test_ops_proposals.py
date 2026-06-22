"""Proposal validation + the human approval gate - the safety core of the ops agent.

Covers: allowlist/blocklist/bounds/drift validation, path-aware comment-preserving
YAML edits (incl. duplicate leaf names), and the write -> approve -> apply flow with
verification, backup, audit log, and the guarantee that nothing applies unapproved."""
from __future__ import annotations

import json
import os

import pytest
import yaml

from src import ops_proposals as P
from src.ops_proposals import (ApprovalGate, Proposal, validate_proposal, set_yaml_scalar,
                               sanitize_allowlist)


_CFG_TEXT = """\
strategy:
  donchian:
    entry_period: 40          # comment A
    atr_trail_mult: 3.0       # comment B (the donchian trail)
exits:
  atr_trail_mult: 2.5         # comment C (a DIFFERENT key with the same leaf name)
risk:
  risk_per_trade_pct: 0.01    # comment D
safety:
  daily_loss_limit_pct: 0.03  # NEVER touch
"""

_TUNABLE = {
    "strategy.donchian.entry_period": {"min": 20, "max": 80, "type": "int"},
    "strategy.donchian.atr_trail_mult": {"min": 2.0, "max": 4.5},
    "exits.atr_trail_mult": {"min": 2.0, "max": 3.5},
    "risk.risk_per_trade_pct": {"min": 0.003, "max": 0.02},
}
_BLOCKED = ["safety", "capital_policy", "risk.max_position_pct"]


def _cfg():
    return yaml.safe_load(_CFG_TEXT)


# --------------------------- validation ------------------------------------- #
def test_validate_accepts_in_bounds_change():
    cfg = _cfg()
    p = Proposal("strategy.donchian.atr_trail_mult", 3.0, 2.5)
    ok, why = validate_proposal(p, cfg, _TUNABLE, _BLOCKED)
    assert ok, why


def test_validate_rejects_blocked_safety_key():
    cfg = _cfg()
    p = Proposal("safety.daily_loss_limit_pct", 0.03, 0.05)
    ok, why = validate_proposal(p, cfg, _TUNABLE, _BLOCKED)
    assert not ok and "blocklist" in why


def test_validate_rejects_non_allowlisted_key():
    cfg = _cfg()
    p = Proposal("strategy.donchian.min_history", 60, 50)
    ok, why = validate_proposal(p, cfg, _TUNABLE, _BLOCKED)
    assert not ok and "allowlist" in why


def test_validate_rejects_out_of_bounds():
    cfg = _cfg()
    p = Proposal("strategy.donchian.atr_trail_mult", 3.0, 9.0)
    ok, why = validate_proposal(p, cfg, _TUNABLE, _BLOCKED)
    assert not ok and "max" in why


def test_validate_rejects_stale_current():
    cfg = _cfg()
    p = Proposal("risk.risk_per_trade_pct", 0.02, 0.015)   # live is 0.01, not 0.02
    ok, why = validate_proposal(p, cfg, _TUNABLE, _BLOCKED)
    assert not ok and "stale" in why


def test_sanitize_allowlist_drops_blocked_and_phantom_keys():
    cfg = _cfg()
    dirty = {
        "strategy.donchian.atr_trail_mult": {"min": 2.0, "max": 4.5},   # ok
        "safety.daily_loss_limit_pct": {"min": 0.01, "max": 0.1},       # blocked prefix -> drop
        "risk.nonexistent_knob": {"min": 0, "max": 1},                  # phantom -> drop
    }
    clean, warns = sanitize_allowlist(dirty, _BLOCKED, cfg)
    assert set(clean) == {"strategy.donchian.atr_trail_mult"}
    assert any("safety" in w and "blocked" in w for w in warns)
    assert any("nonexistent" in w and "resolve" in w for w in warns)


# --------------------------- surgical YAML edit ----------------------------- #
def test_set_yaml_scalar_is_path_aware_and_preserves_comments():
    new_text, old = set_yaml_scalar(_CFG_TEXT, "strategy.donchian.atr_trail_mult", "2.5")
    assert old == "3.0"
    loaded = yaml.safe_load(new_text)
    assert loaded["strategy"]["donchian"]["atr_trail_mult"] == 2.5
    assert loaded["exits"]["atr_trail_mult"] == 2.5          # the OTHER key is untouched
    assert "# comment B (the donchian trail)" in new_text    # comment preserved
    assert "# comment C" in new_text


def test_set_yaml_scalar_targets_the_correct_duplicate_leaf():
    new_text, _ = set_yaml_scalar(_CFG_TEXT, "exits.atr_trail_mult", "3.2")
    loaded = yaml.safe_load(new_text)
    assert loaded["exits"]["atr_trail_mult"] == 3.2
    assert loaded["strategy"]["donchian"]["atr_trail_mult"] == 3.0   # donchian untouched


# --------------------------- approval gate flow ----------------------------- #
def test_full_gate_flow_write_approve_apply(tmp_path):
    cfg_path = tmp_path / "trading_config.yaml"
    cfg_path.write_text(_CFG_TEXT, encoding="utf-8")
    gate = ApprovalGate(str(tmp_path / "proposals"), str(tmp_path / "audit.log"))
    props = [Proposal("strategy.donchian.atr_trail_mult", 3.0, 2.5, "tighten", "less giveback",
                      "medium", "rule")]
    review = gate.write_pending(props, {"note": "demo"}, "daily")
    assert os.path.exists(review)

    # Nothing applies before approval.
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    res0 = gate.apply_approved(review, str(cfg_path), cfg, _TUNABLE, _BLOCKED, "tester")
    assert res0["applied"] == [] and res0["skipped"]         # skipped: not approved

    # Approve, then apply.
    assert gate.approve(review, "alice") == 1
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    res = gate.apply_approved(review, str(cfg_path), cfg, _TUNABLE, _BLOCKED, "alice")
    assert len(res["applied"]) == 1 and res["applied"][0]["to"] == 2.5

    after = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    assert after["strategy"]["donchian"]["atr_trail_mult"] == 2.5
    assert after["exits"]["atr_trail_mult"] == 2.5          # untouched
    assert "# comment B (the donchian trail)" in cfg_path.read_text(encoding="utf-8")
    # A backup was made and the audit log recorded the apply.
    assert any(f.startswith("trading_config.yaml.bak-") for f in os.listdir(tmp_path))
    audit = (tmp_path / "audit.log").read_text(encoding="utf-8").strip().splitlines()
    actions = [json.loads(line)["action"] for line in audit]
    assert "apply" in actions and "approve" in actions


def test_gate_never_applies_blocked_even_if_approved(tmp_path):
    cfg_path = tmp_path / "trading_config.yaml"
    cfg_path.write_text(_CFG_TEXT, encoding="utf-8")
    gate = ApprovalGate(str(tmp_path / "proposals"), str(tmp_path / "audit.log"))
    # A (malicious/buggy) approved proposal targeting a safety key must be refused.
    review = gate.write_pending(
        [Proposal("safety.daily_loss_limit_pct", 0.03, 0.10, "bad", "bad", "high", "llm")],
        {}, "daily")
    gate.approve(review, "mallory")
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    res = gate.apply_approved(review, str(cfg_path), cfg, _TUNABLE, _BLOCKED, "mallory")
    assert res["applied"] == []
    assert any("blocklist" in s["why"] for s in res["skipped"])
    after = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    assert after["safety"]["daily_loss_limit_pct"] == 0.03   # unchanged
