# syntax=docker/dockerfile:1
# ---------------------------------------------------------------------------
# dlbot — Discord bot + yt-dlp download worker, containerised.
#
#   docker build -t dlbot:local .
#   docker compose up -d --build          # bot + worker (recommended entrypoint)
#
# The image is built for the app's combined mode: `dlbot` (no arguments) runs the
# Discord bot and spawns the queue worker as a child process. Pass `bot` or
# `worker` as the container command to run only one half.
# ---------------------------------------------------------------------------

# Static uv binary, copied out of the official image (see the uv docs pattern).
FROM ghcr.io/astral-sh/uv:latest AS uv

FROM python:3.11-slim

LABEL org.opencontainers.image.title="dlbot" \
      org.opencontainers.image.description="Discord bot that queues YouTube/Instagram downloads via yt-dlp" \
      org.opencontainers.image.source="https://github.com/dotneko/dlbot"

# ffmpeg is mandatory at runtime: video jobs merge video+audio into mp4 and
# audio jobs re-encode to mp3. Without it every download job fails.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Run as a non-root user. The uid/gid default to 1000 so the bind-mounted
# data/, downloads/ and logs/ directories stay writable on a typical single-user
# Linux host. Override when needed:
#   --build-arg DLBOT_UID="$(id -u)" --build-arg DLBOT_GID="$(id -g)"
ARG DLBOT_UID=1000
ARG DLBOT_GID=1000
RUN groupadd --gid "${DLBOT_GID}" dlbot \
    && useradd --uid "${DLBOT_UID}" --gid dlbot \
         --home-dir /home/dlbot --create-home --shell /usr/sbin/nologin dlbot

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

COPY --from=uv /uv /usr/local/bin/uv

# Dependencies first (resolved from uv.lock), then the project itself, so editing
# src/ does not re-download discord.py / yt-dlp.
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev \
    && mkdir -p /app/data /app/downloads /app/logs \
    && chown -R dlbot:dlbot /app

ENV PATH="/app/.venv/bin:${PATH}"

USER dlbot

# config.py resolves relative DLBOT_* paths against /app, so these defaults land
# in the directories the compose file mounts. docker-compose.yml sets absolute
# paths anyway; they are repeated here so a bare `docker run` works too.
ENV DLBOT_DATABASE=/app/data/dlbot.db \
    DLBOT_DOWNLOAD_DIR=/app/downloads \
    DLBOT_LOG_DIR=/app/logs

# `dlbot` -> bot + worker; `dlbot bot` / `dlbot worker` -> one component.
ENTRYPOINT ["dlbot"]
CMD []
