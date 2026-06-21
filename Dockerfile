# =============================================================================
# Dockerfile - optimized for Railway
# Long-running worker(s). No web port. No secrets baked in (Railway injects them
# as environment variables at runtime). A process supervisor (src/run_all.py)
# runs the bots selected by RUN_BOTS in ONE container - keep numReplicas=1.
# =============================================================================
FROM python:3.12-slim

# - PYTHONUNBUFFERED so logs stream to Railway live (no buffering delay).
# - PYTHONFAULTHANDLER prints a traceback if the process is killed by a signal.
# - PIP_NO_CACHE_DIR keeps the image small.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install dependencies first for better layer caching. requirements-etf.txt adds
# alpaca-py, needed only when RUN_BOTS includes "etf" (harmless otherwise).
COPY requirements.txt requirements-etf.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-etf.txt

# Copy the rest of the app.
COPY . .

# Safe defaults. Railway "Variables" override these - real money needs BOTH
# PAPER_TRADING=false AND LIVE_TRADING_ENABLED=true (a shared master tripwire for
# all bots; each bot is still individually gated by its own *_ENABLED flag).
#   RUN_BOTS selects which bots run: "spot" (default) | "spot,carry,etf" | ...
ENV PAPER_TRADING=true \
    LIVE_TRADING_ENABLED=false \
    RUN_BOTS=spot

# Railway stops a deploy by sending SIGTERM (then SIGKILL after a grace period).
# The exec-form CMD makes the supervisor PID 1, so it receives SIGTERM directly
# and forwards it to every child bot for a clean shutdown.
STOPSIGNAL SIGTERM

CMD ["python", "-m", "src.run_all"]
