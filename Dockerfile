# =============================================================================
# Dockerfile - optimized for Railway
# Single process, slim image, no secrets baked in (they come from env vars).
# =============================================================================
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Default to fully-safe PAPER mode unless the deployment overrides BOTH switches.
ENV PAPER_TRADING=true \
    LIVE_TRADING_ENABLED=false \
    EXCHANGE_ID=binanceus

CMD ["python", "-m", "src.main_loop"]
