# syntax=docker/dockerfile:1
#
# StackFlowOS hub image.
#
#   docker build -f deploy/Dockerfile -t stackflowos:hub .
#   docker run --rm -p 17800:17800 -v sfo-data:/data stackflowos:hub
#
# The runtime footprint is stdlib + pynng only; pyzmq is intentionally NOT
# installed (the ZMQ bridge is optional and enables itself only when the
# package is present).  State lives in the /data volume (config, event log,
# project workspaces); nothing else is written by the hub.

# ---- build stage: compile pynng (and any future extension) --------------
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# pynng publishes wheels for most platforms, but keeping the toolchain in the
# builder stage makes source builds (new arch/libc) work without bloating the
# runtime image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential cmake \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY pyproject.toml README.md LICENSE ./
COPY stackflowos ./stackflowos
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install .

# ---- runtime stage ------------------------------------------------------
FROM python:3.12-slim

LABEL org.opencontainers.image.title="StackFlowOS hub" \
      org.opencontainers.image.description="Agent-native operating system hub (web console + L2 orchestration)" \
      org.opencontainers.image.licenses="MIT"

RUN groupadd --system --gid 10001 sfo \
    && useradd --system --uid 10001 --gid sfo --home-dir /data --shell /usr/sbin/nologin sfo \
    && mkdir -p /data /app \
    && chown -R sfo:sfo /data /app

COPY --from=builder /opt/venv /opt/venv

ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    SFO_ROOT=/app \
    SFO_STATE_DIR=/data \
    SFO_WEB_PORT=17800

WORKDIR /app
USER sfo

# Configuration/state: projects, event log, SQLite DB, logs, workspaces.
VOLUME ["/data"]

# Web console (default) and the TCP transport port pool (for remote workers).
EXPOSE 17800/tcp 17820-17899/tcp

# Anonymous liveness probe provided by the web server (no project data).
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:17800/healthz', timeout=3)"]

# The console binds 0.0.0.0 inside the container; publish it only on a
# loopback/private host interface or behind an authenticated TLS reverse proxy.
# SFO_WEB_TOKEN can pin the browser login token (see deploy/README.md).
# ``--allow-remote`` is required by the config layer for a non-loopback bind.
ENTRYPOINT ["stackflowos", "hub", "--host", "0.0.0.0", "--allow-remote"]
