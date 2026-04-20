# Multi-stage build. Two independent stages, two very different images:
#
#   target: api    → python:3.12-slim + uv deps. NO browser. ~200 MB.
#                    Runs uvicorn on 0.0.0.0:8000. Serves dashboard + JSON API.
#                    Enqueues runs into Redis + finalizes when agents drain them.
#
#   target: agent  → mcr.microsoft.com/playwright/python + uv deps.
#                    Chromium + all system deps + Playwright-tested seccomp.
#                    Runs `andera agent`, which pulls from the shared Redis
#                    queue and drives the full LangGraph per sample.
#                    ~1.2 GB. Scale via `docker compose up -d --scale agent=N`.
#
# Build either one:
#   docker build -f Dockerfile --target api   -t andera-api   .
#   docker build -f Dockerfile --target agent -t andera-agent .

########################################
# API (slim)
########################################
FROM python:3.12-slim AS api
WORKDIR /app

# --- uv ---
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

# --- python deps ---
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --extra redis --extra postgres --no-dev --no-install-project

# --- source ---
COPY src/ src/
COPY config/ config/
RUN uv sync --frozen --extra redis --extra postgres --no-dev

EXPOSE 8000
CMD ["uv", "run", "uvicorn", "andera.api.app:app", \
     "--host", "0.0.0.0", "--port", "8000"]

########################################
# Agent (includes Chromium)
########################################
FROM mcr.microsoft.com/playwright/python:v1.58.0-jammy AS agent
WORKDIR /app

# --- uv ---
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

# --- python deps ---
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --extra redis --extra postgres --no-dev --no-install-project

# --- source ---
COPY src/ src/
COPY config/ config/
RUN uv sync --frozen --extra redis --extra postgres --no-dev

# Playwright image already has chromium installed. No extra step needed.
CMD ["uv", "run", "andera", "agent"]
