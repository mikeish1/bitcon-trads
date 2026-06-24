# =============================================================================
# Dockerfile - Railway. Long-running supervisor (src/run_all.py) runs the bots
# selected by RUN_BOTS in ONE container - keep numReplicas=1. No secrets baked in
# (Railway injects them as environment variables at runtime).
# =============================================================================

# --- Stage 1: build the React dashboard (only used when RUN_BOTS includes "web")
# Produces web/frontend/dist, which web/server.py serves at "/". Built on Linux so
# the bundle never carries the local Windows node_modules (see .dockerignore).
FROM node:20-slim AS frontend
WORKDIR /app/web/frontend
# Copy manifests first for layer caching; npm ci uses the committed lockfile (reproducible).
COPY web/frontend/package.json web/frontend/package-lock.json ./
RUN npm ci
COPY web/frontend/ ./
RUN npm run build

# --- Stage 2: Python runtime (bots + supervisor + dashboard API) -------------
FROM python:3.12-slim

# - PYTHONUNBUFFERED so logs stream to Railway live (no buffering delay).
# - PYTHONFAULTHANDLER prints a traceback if the process is killed by a signal.
# - PIP_NO_CACHE_DIR keeps the image small.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install ALL runtime deps up front for layer caching:
#   requirements.txt      - core bot
#   requirements-etf.txt   - alpaca-py (ETF bot; harmless otherwise)
#   requirements-web.txt   - fastapi/uvicorn (PREVIOUSLY MISSING -> the `web` child
#                            could not start; this line is the fix for that crash-loop).
COPY requirements.txt requirements-etf.txt requirements-web.txt ./
RUN pip install --no-cache-dir \
    -r requirements.txt -r requirements-etf.txt -r requirements-web.txt

# Copy the rest of the app.
COPY . .

# Bring the built SPA in from stage 1 so create_app() serves the REAL dashboard
# at "/" (it only mounts the SPA if web/frontend/dist exists; otherwise a JSON stub).
COPY --from=frontend /app/web/frontend/dist ./web/frontend/dist

# Create the volume mount point so SQLite can always open its file here. Mount a
# Railway Volume at /data to make it PERSIST across redeploys. Without a volume the
# bot still runs, but /data is ephemeral (state is lost on redeploy) - so ALWAYS
# attach the volume for real use.
RUN mkdir -p /data

# Safe defaults. Railway "Variables" override these.
#  * Real money needs BOTH PAPER_TRADING=false AND LIVE_TRADING_ENABLED=true (a
#    shared master tripwire; each bot is still individually gated by its *_ENABLED flag).
#  * RUN_BOTS selects which bots run: "spot" (default) | "spot,carry,etf" | "spot,web" ...
#  * DB_PATH / CAPITAL_LIMITS_PATH default ONTO the volume mount point, so attaching
#    the /data volume gives persistence even if these variables are not set
#    explicitly (fixes the silent data-loss footgun).
ENV PAPER_TRADING=true \
    LIVE_TRADING_ENABLED=false \
    RUN_BOTS=spot \
    DB_PATH=/data/trading_state.db \
    CAPITAL_LIMITS_PATH=/data/capital_limits.json

# Railway stops a deploy by sending SIGTERM (then SIGKILL after a grace period).
# The exec-form CMD makes the supervisor PID 1, so it receives SIGTERM directly
# and forwards it to every child bot for a clean shutdown.
STOPSIGNAL SIGTERM

CMD ["python", "-m", "src.run_all"]
