# Multi-stage build: a builder stage installs dependencies into a venv,
# the runtime stage copies only that venv + app code onto a slim base —
# keeps the final image free of build tooling (gcc, pip cache, etc.).

FROM python:3.13-slim AS builder

WORKDIR /build

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.lock.txt .
RUN pip install --no-cache-dir -r requirements.lock.txt

FROM python:3.13-slim AS runtime

RUN groupadd --system app && useradd --system --gid app --create-home app

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /app
COPY app ./app

# /app/data holds SQLite files when DATABASE_URL/CHECKPOINT_DB_PATH are left
# at their defaults — irrelevant when compose points them at Postgres (see
# docker-compose.yml), but harmless either way since db/session.py and
# agent/runner.py both auto-create the parent directory on first use.
RUN mkdir -p /app/data && chown -R app:app /app
USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)" || exit 1

# gunicorn managing uvicorn workers — production-appropriate process model,
# unlike "uvicorn --reload" (the README's local-dev-only run command, which
# is a dev convenience flag that reloads on file changes and isn't meant to
# run in a container).
#
# --workers 1, deliberately: slowapi's Limiter (app/api/main.py) uses the
# default in-memory storage backend, which is per-process, not shared across
# workers — with 2+ workers each independently allows the configured
# request rate, so the REAL effective limit silently doubles (or more) with
# every additional worker. A shared store (Redis) would let this scale back
# up, but isn't part of this stack today — 1 worker is the simplest correct
# fix rather than adding new infrastructure for a small-team-scale app. This
# app's request handlers are I/O-bound (awaiting the LLM, the MCP gateway,
# Postgres) rather than CPU-bound, so a single async worker still serves
# many concurrent in-flight requests — it isn't the same limitation single-
# threaded CPU-bound work would be.
CMD ["gunicorn", "app.api.main:app", \
     "--workers", "1", \
     "--worker-class", "uvicorn.workers.UvicornWorker", \
     "--bind", "0.0.0.0:8000", \
     "--access-logfile", "-", \
     "--error-logfile", "-"]
