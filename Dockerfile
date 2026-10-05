# APEX backend image (API and Celery worker share this image; the
# command decides the role -- see docker-compose.yml / DEPLOYMENT.md).
#
# Security notes:
# - Secrets are NEVER baked in: `.dockerignore` excludes `.env*` (except
#   `.env.example`), local venvs and caches. Configuration comes from
#   environment variables at runtime.
# - The app runs as an unprivileged user, not root.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# build-essential + libpq-dev for psycopg2, git for VCS installs, curl for the HEALTHCHECK.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
        git \
        curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# Unprivileged runtime user.
RUN groupadd --system apex && useradd --system --gid apex --home-dir /app --no-create-home apex

COPY --chown=apex:apex . .

USER apex

EXPOSE 8000

# Liveness only (no DB/Redis touch). Readiness is GET /ready -- point the
# platform's readiness probe there.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/health || exit 1

# Graceful shutdown: uvicorn stops accepting connections on SIGTERM and
# waits up to 20s for in-flight requests. Pipeline work is NOT lost on
# restart -- it lives in the `pipeline_jobs` table with a lease, and an
# expired lease is reclaimed by the next worker run.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--timeout-graceful-shutdown", "20"]
