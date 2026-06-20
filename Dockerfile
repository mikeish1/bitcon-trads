# =============================================================================
# Dockerfile - optimized for Railway
# Single process, slim image, no secrets baked in (they come from env vars).
# =============================================================================
FROM python:3.12-slim

# Don't write .pyc files; flush stdout/stderr immediately so Railway shows logs live.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install dependencies first (better build caching).
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application.
COPY . .

# Default to PAPER trading unless the deployment explicitly overrides it.
ENV PAPER_TRADING=true

# Run the autonomous heartbeat loop.
CMD ["python", "-m", "src.main_loop"]
