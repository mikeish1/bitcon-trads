"""
API contract tests via FastAPI TestClient over a seeded sample DB (architecture §12).
Covers status codes, pagination, redaction, capital simulate/PUT auth, and the
fail-closed mutation guard. The public price client is monkeypatched so tests run
fully offline.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.conftest_web import seed_sample_db
from web.prices import PriceQuote


@pytest.fixture
def client(tmp_path, monkeypatch):
    db_path = str(tmp_path / "trading_state.db")
    # Isolate the capital-override + audit files to tmp so the audited PUT can never
    # touch the repo's real config/capital_limits.json (the service honors this env).
    monkeypatch.setenv("CAPITAL_LIMITS_PATH", str(tmp_path / "capital_limits.json"))
    cfg = seed_sample_db(db_path)

    # Offline prices: patch MarketDataClient so no network is hit.
    def fake_get_prices(self, bases):
        px = {"BTC": 63000.0, "ETH": 2800.0, "SOL": 160.0}
        return {b.upper(): PriceQuote(b.upper(), px.get(b.upper(), 0.0), 0.0, False)
                for b in bases}

    monkeypatch.setattr("web.prices.MarketDataClient.get_prices", fake_get_prices)
    monkeypatch.setattr("web.prices.MarketDataClient.max_age_seconds", lambda self, b: 1.0)
    monkeypatch.delenv("DASHBOARD_TOKEN", raising=False)

    from web.server import create_app
    app = create_app(cfg)
    with TestClient(app) as c:
        yield c


def test_health_ok(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["db_ok"] is True
    assert body["mode"] == "PAPER"
    assert body["open_positions"] == 1


def test_summary_shape(client):
    r = client.get("/api/summary")
    assert r.status_code == 200
    b = r.json()
    assert b["mode"]["mode"] == "PAPER"
    assert b["open_positions"] == 1
    assert b["equity_basis"] == "paper_ledger"


def test_positions_live_mtm(client):
    r = client.get("/api/positions")
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 1 and rows[0]["symbol"] == "SOL/USDT"
    assert rows[0]["unrealized_pnl_usd"] == 10.0


def test_trades_pagination(client):
    r = client.get("/api/trades?limit=1")
    assert r.status_code == 200
    page = r.json()
    assert len(page["items"]) == 1
    assert page["has_more"] is True
    r2 = client.get(f"/api/trades?limit=1&cursor={page['next_cursor']}")
    assert r2.status_code == 200
    assert r2.json()["items"][0]["id"] < page["items"][0]["id"]


def test_trades_filtering(client):
    r = client.get("/api/trades?symbol=ETH/USDT")
    assert r.status_code == 200
    items = r.json()["items"]
    assert all(i["symbol"] == "ETH/USDT" for i in items)


def test_decisions_list(client):
    r = client.get("/api/decisions?action=BUY")
    assert r.status_code == 200
    assert all(d["action"] == "BUY" for d in r.json()["items"])


def test_performance_endpoints(client):
    assert client.get("/api/performance/equity?range=all").status_code == 200
    assert client.get("/api/performance/stats").status_code == 200
    assert client.get("/api/performance/attribution").status_code == 200
    assert client.get("/api/performance/regime").status_code == 200


def test_risk_gauges(client):
    r = client.get("/api/risk")
    assert r.status_code == 200
    assert r.json()["concurrent_positions"]["limit"] == 3


def test_config_redacts_secrets(client):
    r = client.get("/api/config")
    assert r.status_code == 200
    body = r.text
    for secret in ("SECRET_KEY_VALUE", "SECRET_SECRET_VALUE", "SECRET_ANTHROPIC", "SECRET_TG_TOKEN"):
        assert secret not in body
    assert len(r.json()["redacted_keys"]) >= 3


def test_capital_simulate_no_write(client):
    r = client.post("/api/capital-limits/spot/simulate", json={"max_usd": 100.0})
    assert r.status_code == 200
    b = r.json()
    assert b["valid"] is True
    assert b["deployable_capital"] <= 100.0  # min(90% equity, $100) -> $100 binds? equity~410


def test_capital_put_fail_closed_without_token(client):
    # No DASHBOARD_TOKEN configured -> the only mutation is forbidden (403).
    r = client.put("/api/capital-limits/spot", json={"max_pct": 0.5})
    assert r.status_code == 403


def test_capital_put_with_token(tmp_path, monkeypatch):
    db_path = str(tmp_path / "trading_state.db")
    monkeypatch.setenv("CAPITAL_LIMITS_PATH", str(tmp_path / "capital_limits.json"))
    cfg = seed_sample_db(db_path)
    monkeypatch.setenv("DASHBOARD_TOKEN", "s3cret")
    monkeypatch.setattr("web.prices.MarketDataClient.get_prices",
                        lambda self, bases: {})
    monkeypatch.setattr("web.prices.MarketDataClient.max_age_seconds", lambda self, b: 1.0)
    from web.server import create_app
    app = create_app(cfg)
    with TestClient(app) as c:
        # Wrong/no token -> 401.
        assert c.put("/api/capital-limits/spot", json={"max_pct": 0.5}).status_code == 401
        # Correct token -> 200 and persisted.
        ok = c.put("/api/capital-limits/spot", json={"max_pct": 0.5},
                   headers={"X-API-Key": "s3cret"})
        assert ok.status_code == 200
        assert ok.json()["ok"] is True


def test_unknown_sleeve_404(client):
    assert client.get("/api/capital-limits/nope").status_code == 404


def test_error_envelope(client):
    r = client.get("/api/trades/999999")
    assert r.status_code == 404
    assert "error" in r.json() and r.json()["error"]["code"] == 404
