# =============================================================================
# Dockerfile - optimized for Railway
# Single long-running worker. No web port. No secrets baked in (Railway injects
# them as environment variables at runtime).
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

# Install dependencies first for better layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the app.
COPY . .

# Safe defaults. Railway "Variables" override these - real money needs BOTH
# PAPER_TRADING=false AND LIVE_TRADING_ENABLED=true.
ENV PAPER_TRADING=true \
    LIVE_TRADING_ENABLED=false

# Railway stops a deploy by sending SIGTERM (then SIGKILL after a grace period).
# The exec-form CMD below makes Python PID 1, so it receives SIGTERM directly;
# the main loop catches it and shuts down cleanly within ~1 second.
STOPSIGNAL SIGTERM

CMD ["python", "-m", "src.main_loop"]
