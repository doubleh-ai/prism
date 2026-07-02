# Prism — production container
#
# Usage:
#   docker build -t prism:latest .
#   docker run -d --name prism -p 8088:8088 \
#     -v /opt/prism/data:/app/data \
#     -v /opt/prism/logs:/app/logs \
#     -e DEEPSEEK_API_KEY=... \
#     -e PRISM_COOKIE_SECURE=1 \
#     prism:latest
#
# The container is non-root, runs uvicorn directly, and mounts a writable
# volume for the SQLite database and rotated logs.

# ── Builder stage — installs deps in an isolated env ──
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --upgrade pip && pip install --no-cache-dir -r requirements.txt


# ── Runtime stage — minimal image, non-root user ──
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PRISM_DATA_DIR=/app/data \
    PRISM_LOG_FILE=/app/logs/app.log \
    PRISM_LOG_LEVEL=INFO

# Create non-root user
RUN groupadd --system --gid 1001 prism && \
    useradd --system --uid 1001 --gid prism --no-create-home --shell /bin/false prism

# Copy installed packages from builder
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

WORKDIR /app

# Copy application code
COPY server/ ./server/
COPY static/ ./static/
COPY templates/ ./templates/

# Create writable directories and chown
RUN mkdir -p /app/data /app/logs && \
    chown -R prism:prism /app

USER prism

EXPOSE 8088

# Healthcheck — endpoint returns 401 unauthed, which counts as the service being up
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8088/api/me', timeout=3)" || exit 1

# Run uvicorn. The host is 0.0.0.0 inside the container; the reverse proxy
# in front should handle TLS termination.
CMD ["uvicorn", "server.main:app", "--host", "0.0.0.0", "--port", "8088", "--workers", "1"]
