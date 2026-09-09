# syntax=docker/dockerfile:1
#
# GramVault production image.
#
#   docker compose up -d --build
#
# See docker-compose.yml for the bind mounts (config.yaml, secrets.yaml,
# data/, the vault, the Instagram session dir) and INTEGRATION-PLAN.md §H2
# for the deployment notes (host networking vs. a compose network, Ollama
# on the host vs. in a container).

# ---- Stage 1: build the React frontend ----
FROM node:22-alpine AS frontend

WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build


# ---- Stage 2: runtime ----
FROM python:3.12-slim

# ffmpeg: video keyframe extraction + audio decode for faster-whisper.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Backend, installed editable so package-data (db/schema.sql, migrations,
# digest templates) resolves exactly as in a dev checkout. The [instagram]
# extra bundles instaloader for the opt-in §F "pull your saved posts" path;
# it stays dormant unless `pull.enabled` is set in config.yaml.
COPY pyproject.toml README.md ./
COPY backend ./backend
RUN pip install --no-cache-dir -e ".[instagram]"

# Built SPA from stage 1 — gramvault.main mounts ./frontend/dist when present
# (Path(__file__).parents[2] / "frontend" / "dist" == /app/frontend/dist).
COPY --from=frontend /frontend/dist ./frontend/dist

# config.yaml + secrets.yaml arrive as bind mounts at runtime. Everything
# else is pinned into the /data volume so nothing state-bearing lives in
# the image layer.
ENV GRAMVAULT_CONFIG_PATH=/app/config.yaml \
    HF_HOME=/data/hf \
    GRAMVAULT_PATHS__LIBRARY_DIR=/data/library \
    GRAMVAULT_PATHS__DB_PATH=/data/gramvault.db \
    GRAMVAULT_PATHS__CHROMA_DIR=/data/chroma

# Non-root by default. docker-compose.yml overrides this with `user:` so the
# process matches the host uid that owns the bind-mounted data/ and vault.
RUN useradd --create-home --uid 1000 app \
    && mkdir -p /data \
    && chown app:app /data
USER app

EXPOSE 8000

# server.host / server.port come from config.yaml (or GRAMVAULT_SERVER__*).
# With network_mode: host, set server.host to 127.0.0.1 or your Tailscale IP
# in config.yaml — never 0.0.0.0 without server.auth_token set.
CMD ["gramvault", "serve"]
