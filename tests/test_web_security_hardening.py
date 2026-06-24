"""Dashboard security hardening (L2 proxy-aware client IP, M3 read-auth gate)."""
from __future__ import annotations

import types

from fastapi.testclient import TestClient

from tests.conftest_web import seed_sample_db
from web.security import client_ip, read_auth_required, trust_proxy


# --------------------------------------------------------------------------- #
# L2: proxy-aware client IP for rate-limit keying                              #
# --------------------------------------------------------------------------- #
def _req(host="9.9.9.9", xff=None):
    return types.SimpleNamespace(
        client=types.SimpleNamespace(host=host) if host else None,
        headers={"x-forwarded-for": xff} if xff else {})


def test_client_ip_ignores_xff_when_proxy_untrusted(monkeypatch):
    monkeypatch.delenv("DASHBOARD_TRUST_PROXY", raising=False)
    assert trust_proxy() is False
    # XFF is client-spoofable with no proxy -> must use the socket peer.
    assert client_ip(_req(host="9.9.9.9", xff="1.1.1.1, 2.2.2.2")) == "9.9.9.9"


def test_client_ip_uses_leftmost_xff_when_proxy_trusted(monkeypatch):
    monkeypatch.setenv("DASHBOARD_TRUST_PROXY", "1")
    assert client_ip(_req(host="9.9.9.9", xff="1.1.1.1, 2.2.2.2")) == "1.1.1.1"


def test_client_ip_falls_back_to_peer_when_no_xff(monkeypatch):
    monkeypatch.setenv("DASHBOARD_TRUST_PROXY", "1")
    assert client_ip(_req(host="9.9.9.9", xff=None)) == "9.9.9.9"


def test_client_ip_unknown_without_client(monkeypatch):
    monkeypatch.delenv("DASHBOARD_TRUST_PROXY", raising=False)
    assert client_ip(_req(host=None)) == "unknown"


# --------------------------------------------------------------------------- #
# M3: read-auth gate (open by default; fail-closed when opted in)             #
# --------------------------------------------------------------------------- #
def _app(tmp_path, monkeypatch, **env):
    db_path = str(tmp_path / "trading_state.db")
    monkeypatch.setenv("CAPITAL_LIMITS_PATH", str(tmp_path / "capital_limits.json"))
    monkeypatch.delenv("DASHBOARD_TOKEN", raising=False)
    monkeypatch.delenv("DASHBOARD_REQUIRE_AUTH", raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    cfg = seed_sample_db(db_path)
    from web.server import create_app
    return create_app(cfg)


def test_reads_open_by_default(tmp_path, monkeypatch):
    assert read_auth_required() is False
    c = TestClient(_app(tmp_path, monkeypatch))
    assert c.get("/api/capital-limits").status_code == 200          # open (legacy default)


def test_require_auth_fails_closed_without_token(tmp_path, monkeypatch):
    c = TestClient(_app(tmp_path, monkeypatch, DASHBOARD_REQUIRE_AUTH="1"))
    assert c.get("/api/capital-limits").status_code == 403          # opted in but no token


def test_require_auth_with_token_enforces_header(tmp_path, monkeypatch):
    c = TestClient(_app(tmp_path, monkeypatch, DASHBOARD_REQUIRE_AUTH="1", DASHBOARD_TOKEN="s3cret"))
    assert c.get("/api/capital-limits").status_code == 401          # missing header
    assert c.get("/api/capital-limits", headers={"X-API-Key": "s3cret"}).status_code == 200
