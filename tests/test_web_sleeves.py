"""
Tests for the carry & ETF sleeve coverage (web/sleeves.py + /api/sleeves).

Proves: (1) the read path returns correct, enriched shapes for both sleeves;
(2) it degrades gracefully to `available=False` when a sleeve has never run (its
tables are absent); (3) ETF equity holdings the crypto feed can't price fall back
to cost basis and are flagged stale; (4) reading the sleeve tables never escapes
the read-only guarantee.
"""
from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from tests.conftest_web import seed_sample_db, seed_sleeve_tables
from web.db import ReadOnlyDB
from web.prices import PriceQuote
from web import sleeves as s


# --------------------------------------------------------------------------- #
# Direct query-layer tests (no HTTP)                                           #
# --------------------------------------------------------------------------- #
def _ro(db_path: str) -> ReadOnlyDB:
    return ReadOnlyDB(db_path)


def test_etf_sleeve_unpriced_falls_back_to_cost(tmp_path):
    db_path = str(tmp_path / "trading_state.db")
    seed_sample_db(db_path)
    seed_sleeve_tables(db_path)
    with _ro(db_path).conn() as c:
        etf = s.build_etf_sleeve(c, prices={}, cap={"source": "legacy"}, pol=None)
    assert etf.available is True
    assert etf.open_positions == 1
    assert etf.priced is False
    assert etf.holdings_cost_usd == 5000.0
    assert etf.holdings_market_value is None       # equities unpriced -> no MTM
    assert etf.realized_pnl_usd == 120.0           # the CLOSED QQQ winner
    assert etf.paper_cash == 8000.0
    h = etf.holdings[0]
    assert h.symbol == "SPY" and h.price_is_stale is True and h.unrealized_pnl_usd is None


def test_carry_sleeve_pairs_and_funding(tmp_path):
    db_path = str(tmp_path / "trading_state.db")
    seed_sample_db(db_path)
    seed_sleeve_tables(db_path)
    with _ro(db_path).conn() as c:
        carry = s.build_carry_sleeve(c, cap={"source": "yaml"}, pol=None)
    assert carry.available is True
    assert carry.open_pairs_count == 1
    assert carry.capital_used == 720.0
    assert carry.realized_total_usd == 12.0        # the CLOSED ETH pair (incl. its funding)
    assert round(carry.funding_total_usd, 2) == 3.5  # 1.4 + 1.3 + 0.8
    assert carry.kill_active is False
    assert len(carry.funding_series) == 2          # two distinct days
    pair = carry.pairs[0]
    assert pair.asset == "BTC" and pair.unwind_in_progress is False
    assert pair.funding_accrued_usd == 3.5 and pair.delta_drift_pct == 0.0


def test_sleeves_absent_degrade_gracefully(tmp_path):
    """A DB with only spot tables -> carry/etf report available=False, no errors."""
    db_path = str(tmp_path / "trading_state.db")
    seed_sample_db(db_path)  # spot only; no sleeve tables
    with _ro(db_path).conn() as c:
        etf = s.build_etf_sleeve(c, prices={}, cap={}, pol=None)
        carry = s.build_carry_sleeve(c, cap={}, pol=None)
    assert etf.available is False and etf.open_positions == 0 and etf.holdings == []
    assert carry.available is False and carry.open_pairs_count == 0 and carry.pairs == []


def test_overview_has_three_cards(tmp_path):
    db_path = str(tmp_path / "trading_state.db")
    cfg = seed_sample_db(db_path)
    seed_sleeve_tables(db_path)
    with _ro(db_path).conn() as c:
        ov = s.build_overview(c, cfg, prices={"BTC": 63000.0}, settings=_Settings())
    keys = {card.key for card in ov.cards}
    assert keys == {"spot", "carry", "etf"}
    by_key = {card.key: card for card in ov.cards}
    assert by_key["spot"].active is True
    assert by_key["etf"].active is True and by_key["etf"].open_positions == 1
    assert by_key["carry"].active is True and by_key["carry"].primary_label == "Capital used"


class _Settings:
    """Minimal stand-in for CapitalSettingsService for the overview builder."""

    def get(self, sleeve):
        return {"ok": True, "sleeve": sleeve, "source": "legacy",
                "description": f"{sleeve} cap"}

    def policy(self, sleeve):
        return None


# --------------------------------------------------------------------------- #
# HTTP contract tests (TestClient, offline)                                    #
# --------------------------------------------------------------------------- #
@pytest.fixture
def client(tmp_path, monkeypatch):
    db_path = str(tmp_path / "trading_state.db")
    monkeypatch.setenv("CAPITAL_LIMITS_PATH", str(tmp_path / "capital_limits.json"))
    cfg = seed_sample_db(db_path)
    seed_sleeve_tables(db_path)
    monkeypatch.setattr("web.prices.MarketDataClient.get_prices",
                        lambda self, bases: {b.upper(): PriceQuote(b.upper(), 0.0, 0.0, True)
                                             for b in bases})
    monkeypatch.setattr("web.prices.MarketDataClient.max_age_seconds", lambda self, b: 1.0)
    monkeypatch.delenv("DASHBOARD_TOKEN", raising=False)
    from web.server import create_app
    app = create_app(cfg)
    with TestClient(app) as c:
        yield c


def test_sleeves_overview_endpoint(client):
    r = client.get("/api/sleeves")
    assert r.status_code == 200
    cards = r.json()["cards"]
    assert {c["key"] for c in cards} == {"spot", "carry", "etf"}


def test_etf_endpoint(client):
    r = client.get("/api/sleeves/etf")
    assert r.status_code == 200
    b = r.json()
    assert b["available"] is True and b["open_positions"] == 1
    assert b["holdings"][0]["symbol"] == "SPY"


def test_carry_endpoint(client):
    r = client.get("/api/sleeves/carry")
    assert r.status_code == 200
    b = r.json()
    assert b["available"] is True and b["open_pairs_count"] == 1
    assert b["pairs"][0]["asset"] == "BTC"


def test_sleeve_tables_never_writable(tmp_path):
    """The read-only connection rejects writes to the sleeve tables too."""
    db_path = str(tmp_path / "trading_state.db")
    seed_sample_db(db_path)
    seed_sleeve_tables(db_path)
    with _ro(db_path).conn() as c:
        for stmt in ("UPDATE carry_state SET value='1' WHERE key='carry_kill'",
                     "INSERT INTO etf_positions(symbol,status) VALUES('XXX','OPEN')",
                     "DELETE FROM carry_funding"):
            with pytest.raises(sqlite3.OperationalError):
                c.execute(stmt)
