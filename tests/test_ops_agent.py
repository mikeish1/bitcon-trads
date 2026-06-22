"""OpsAgent proposal logic: rule fallback, LLM-disabled path, and allowlist safety.
(The full live-vs-backtest comparison is exercised in the demo run, since it reads
cached market data; here we keep tests fast, offline, and deterministic.)"""
from __future__ import annotations

import pytest

from src.claude_orchestrator import OpsAgent


def _cfg(tmp_path):
    return {
        "runtime": {"db_path": str(tmp_path / "state.db"), "anthropic_api_key": ""},
        "claude": {"model": "claude-haiku-4-5", "max_tokens": 256},
        "strategy": {"donchian": {"entry_period": 40, "atr_trail_mult": 3.0}},
        "risk": {"risk_per_trade_pct": 0.0075, "default_capital_usd": 250.0},
        "execution": {"taker_fee_pct": 0.001, "paper_slippage_pct": 0.0007},
        "universe": {"bases": ["BTC", "ETH"]},
        "ops_agent": {
            "enabled": True,
            "comparison": {"live_lookback_days": 60, "backtest_window_months": 24, "min_live_days": 20},
            "thresholds": {"pvalue": 0.05, "dd_z": 2.0, "slippage_alert_bps": 25},
            "proposals": {
                "approval_mode": "manual", "max_per_run": 3,
                "proposals_dir": str(tmp_path / "proposals"), "audit_log": str(tmp_path / "audit.log"),
                "tunable_keys": {
                    "strategy.donchian.atr_trail_mult": {"min": 2.0, "max": 4.5},
                    "risk.risk_per_trade_pct": {"min": 0.003, "max": 0.02},
                },
                "blocked_keys": ["safety", "capital_policy"],
            },
        },
    }


def _degraded_cmp():
    return {"flags": {"severity": "high", "flags": [
        {"metric": "daily_return_distribution", "severity": "high", "detail": "live < backtest"}],
        "stats": {}}, "live": {}, "current_values": {}}


def test_rule_based_fallback_reduces_risk(tmp_path):
    agent = OpsAgent(_cfg(tmp_path))
    props = agent._rule_based_proposals(_degraded_cmp())
    assert len(props) == 1
    p = props[0]
    assert p.key == "risk.risk_per_trade_pct"
    assert p.proposed == pytest.approx(0.006)        # 0.0075 * 0.8
    assert p.source == "rule" and p.current == 0.0075


def test_generate_proposals_falls_back_when_llm_disabled(tmp_path):
    agent = OpsAgent(_cfg(tmp_path))                 # no API key -> Claude disabled
    assert agent.claude.enabled is False
    props = agent._generate_proposals(_degraded_cmp())
    assert props and props[0].source == "rule"       # deterministic fallback engaged


def test_proposals_respect_max_per_run(tmp_path):
    cfg = _cfg(tmp_path)
    cfg["ops_agent"]["proposals"]["max_per_run"] = 1
    agent = OpsAgent(cfg)
    props = agent._generate_proposals(_degraded_cmp())
    assert len(props) <= 1


def test_no_proposals_when_no_flags(tmp_path):
    agent = OpsAgent(_cfg(tmp_path))
    healthy = {"flags": {"severity": "none", "flags": [], "stats": {}}, "live": {}, "current_values": {}}
    assert agent._rule_based_proposals(healthy) == []        # defensive: no flags -> no proposal
    assert agent._generate_proposals(healthy) == []
