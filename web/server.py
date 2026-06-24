"""
FastAPI application factory for the dashboard.

`create_app(cfg)` builds the app: middleware (CORS, request logging, rate limiting),
all routers, the SSE endpoint, the background equity-snapshot sampler, a uniform
error envelope, and (if present) the built React SPA mounted at `/`.

Co-location: this app runs as the `web` child of `src.run_all` (RUN_BOTS=spot,web),
sharing the volume-mounted SQLite file with the bot. It opens that file READ-ONLY,
so it can never affect trading. See docs/DASHBOARD_ARCHITECTURE.md §11.
"""
from __future__ import annotations

import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger
from starlette.exceptions import HTTPException as StarletteHTTPException

from src.config import load_config

from web import __version__
from web.deps import AppState
from web.security import READ_LIMIT, allowed_origins, client_ip, limiter, log_request
from web.snapshots import SnapshotSampler
from web.routers import (
    capital,
    config as config_router,
    decisions,
    health,
    performance,
    positions,
    risk,
    sleeves,
    stream,
    summary,
    trades,
)

_FRONTEND_DIST = Path(__file__).resolve().parent / "frontend" / "dist"


def create_app(cfg: Optional[dict[str, Any]] = None) -> FastAPI:
    state = AppState(cfg if cfg is not None else load_config())

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Start the equity-snapshot sampler (the only writer; isolated table).
        sampler = SnapshotSampler(state.db.path, state.cfg, state.prices, state.regime_on)
        await sampler.start()
        app.state.sampler = sampler
        logger.info("Dashboard v{} up. Reading {} (read-only).", __version__, state.db.path)
        try:
            yield
        finally:
            await sampler.stop()
            state.close()
            logger.info("Dashboard shut down cleanly.")

    app = FastAPI(
        title="Bitcon-Trads Operations Dashboard",
        version=__version__,
        description="Read-only monitoring & light-ops for the spot trend-following bot.",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )
    app.state.ctx = state

    # --- CORS (local dev + configured Railway origins) ---
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins(),
        allow_credentials=False,           # token auth via header, not cookies
        allow_methods=["GET", "POST", "PUT", "OPTIONS"],
        allow_headers=["Authorization", "X-API-Key", "Content-Type"],
    )

    # --- Request logging + read rate limiting + security headers ---
    @app.middleware("http")
    async def _observe(request: Request, call_next):
        client = client_ip(request)   # proxy-aware (DASHBOARD_TRUST_PROXY); else socket peer
        # Cheap global read limiter (the capital PUT has its own stricter budget).
        if request.url.path.startswith("/api/") and request.method == "GET":
            if not limiter.allow(f"read:{client}", *READ_LIMIT):
                return JSONResponse(status_code=429,
                                    content={"error": {"code": 429, "message": "rate limited"}})
        start = time.perf_counter()
        response = await call_next(request)
        ms = (time.perf_counter() - start) * 1000
        if request.url.path.startswith("/api/"):
            log_request(request.method, request.url.path, response.status_code, ms, client)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        return response

    # --- Uniform error envelope ---
    @app.exception_handler(StarletteHTTPException)
    async def _http_exc(_request: Request, exc: StarletteHTTPException):
        return JSONResponse(status_code=exc.status_code,
                            content={"error": {"code": exc.status_code, "message": exc.detail}})

    @app.exception_handler(Exception)
    async def _unhandled(_request: Request, exc: Exception):  # pragma: no cover - safety net
        logger.exception("Unhandled error: {}", exc)
        return JSONResponse(status_code=500,
                            content={"error": {"code": 500, "message": "internal error"}})

    # --- Routers (one per domain) ---
    for r in (summary, positions, trades, decisions, performance, risk,
              config_router, capital, health, sleeves, stream):
        app.include_router(r.router)

    # --- Static SPA (optional; present only after the frontend build) ---
    if _FRONTEND_DIST.is_dir():
        app.mount("/", StaticFiles(directory=str(_FRONTEND_DIST), html=True), name="spa")
        logger.info("Serving built SPA from {}", _FRONTEND_DIST)
    else:
        @app.get("/")
        def _root() -> dict[str, str]:
            return {"service": "bitcon-trads dashboard", "version": __version__,
                    "docs": "/api/docs", "note": "frontend not built; API is live"}

    return app
