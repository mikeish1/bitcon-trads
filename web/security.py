"""
Security utilities for the dashboard: token auth, CORS, rate limiting, redaction.

Defaults are tuned for "internal/trusted" use (the brief): if no `DASHBOARD_TOKEN`
is set, reads are open. But the one mutating route (capital-limit PUT) is
FAIL-CLOSED: it requires a token to be BOTH configured and presented, so an open
deployment cannot accidentally expose a write.

No third-party dependency is added for any of this (no slowapi): the rate limiter
is a tiny in-process sliding window, which is sufficient for a single-replica
Railway service.
"""
from __future__ import annotations

import os
import threading
import time
from collections import defaultdict, deque
from typing import Any, Optional

from loguru import logger

# Config keys whose values must never leave the process. Matched case-insensitively
# as substrings, so e.g. `anthropic_api_key`, `telegram_token`, `api_secret` all hit.
_SECRET_MARKERS = ("api_key", "api_secret", "secret", "token", "password", "private")


def get_token() -> Optional[str]:
    val = os.getenv("DASHBOARD_TOKEN", "").strip()
    return val or None


def trust_proxy() -> bool:
    """Trust X-Forwarded-For only when set (i.e. when a known proxy like Railway
    sits in front). Off by default, since XFF is client-spoofable with no proxy."""
    return os.getenv("DASHBOARD_TRUST_PROXY", "").strip().lower() in ("1", "true", "yes", "on")


def read_auth_required() -> bool:
    """Fail-closed reads: require the token on GETs too. Reads are open by default
    (internal/trusted brief); set this on a PUBLIC deployment so equity, positions
    and trades aren't world-readable."""
    return os.getenv("DASHBOARD_REQUIRE_AUTH", "").strip().lower() in ("1", "true", "yes", "on")


def client_ip(request: Any) -> str:
    """Best client IP for rate-limit keying and logs. Behind a TRUSTED proxy
    (DASHBOARD_TRUST_PROXY=1) use the left-most X-Forwarded-For hop, so the limiter
    keys on the real client rather than the shared proxy IP; otherwise use the socket
    peer. Never raises."""
    if trust_proxy():
        try:
            xff = request.headers.get("x-forwarded-for", "") or ""
        except Exception:
            xff = ""
        if xff:
            return xff.split(",")[0].strip()
    client = getattr(request, "client", None)
    return getattr(client, "host", None) or "unknown"


def allowed_origins() -> list[str]:
    """CORS allowlist. Defaults cover local dev; set DASHBOARD_ALLOWED_ORIGINS
    (comma-separated) on Railway to the deployed frontend origin(s)."""
    raw = os.getenv("DASHBOARD_ALLOWED_ORIGINS", "").strip()
    if raw:
        return [o.strip() for o in raw.split(",") if o.strip()]
    return ["http://localhost:5173", "http://localhost:3000", "http://127.0.0.1:5173"]


def is_secret_key(key: str) -> bool:
    k = key.lower()
    return any(marker in k for marker in _SECRET_MARKERS)


def redact_mapping(data: dict, _redacted: Optional[list[str]] = None,
                   _prefix: str = "") -> tuple[dict, list[str]]:
    """Deep-copy a config dict with every secret-looking value removed. Returns the
    cleaned dict and the list of dotted keys that were redacted (for transparency in
    the API response). Non-secret values are passed through unchanged."""
    redacted = _redacted if _redacted is not None else []
    out: dict = {}
    for key, value in data.items():
        dotted = f"{_prefix}{key}"
        if is_secret_key(str(key)):
            if value not in (None, "", 0, False):
                redacted.append(dotted)
            continue  # drop the key entirely (don't even ship "***")
        if isinstance(value, dict):
            cleaned, _ = redact_mapping(value, redacted, _prefix=f"{dotted}.")
            out[key] = cleaned
        else:
            out[key] = value
    return out, redacted


class SlidingWindowLimiter:
    """Per-key (IP + route-class) sliding-window limiter. Thread-safe; the store is
    pruned lazily so memory stays bounded for a small set of clients."""

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str, limit: int, window_seconds: float) -> bool:
        now = time.monotonic()
        cutoff = now - window_seconds
        with self._lock:
            dq = self._hits[key]
            while dq and dq[0] < cutoff:
                dq.popleft()
            if len(dq) >= limit:
                return False
            dq.append(now)
            return True


# Module-level singleton limiter (one per process).
limiter = SlidingWindowLimiter()

# Default budgets (architecture §6.1).
READ_LIMIT = (int(os.getenv("DASHBOARD_READ_RPM", "120")), 60.0)   # 120 req / 60s
WRITE_LIMIT = (int(os.getenv("DASHBOARD_WRITE_RPM", "5")), 60.0)   # 5 req / 60s


def log_request(method: str, path: str, status: int, ms: float, client: str) -> None:
    logger.info("{} {} -> {} ({:.1f}ms) [{}]", method, path, status, ms, client)
