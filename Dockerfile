FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    BRIDGE_HOST=0.0.0.0 \
    BRIDGE_PORT=29471 \
    UPSTREAM_PORT=29472 \
    BRIDGE_STATE_DIR=/data \
    BRIDGE_SIGNATURE_MODE=auto \
    BRIDGE_SIGNATURE_CHECK_INTERVAL=86400 \
    BUN_BIN=/usr/local/bin/bun

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl python3 unzip \
    && rm -rf /var/lib/apt/lists/*

# Install only Bun (TLS/HTTP2 fingerprint); signature tracking uses anomalyco/opencode + bundled defaults
RUN curl -fsSL https://bun.sh/install | BUN_INSTALL=/usr/local bash \
    && rm -rf /root/.bun

RUN useradd --uid 1000 --create-home --shell /bin/bash bridge \
    && mkdir -p /app /data \
    && chown -R bridge:bridge /app /data

WORKDIR /app
COPY --chown=bridge:bridge src ./src

USER bridge
VOLUME ["/data"]
EXPOSE 29471

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python3 -c "import os, urllib.request; p = os.environ.get('BRIDGE_PORT') or os.environ.get('PORT') or '29471'; urllib.request.urlopen(f'http://127.0.0.1:{p}/health', timeout=3)"

CMD ["python3", "-m", "opencode_bridge"]
