# Changelog

All notable changes to `opencode-openai-bridge` will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.5.2] - 2026-11-24

### Fixed
- Escalated `POST /_rotate` cooldown/unchanged responses (`cooldown: true` or `rotated: false`) to `force=1` inside `_trigger_rotate_webhook()` so a `429` retry never wastes an attempt on the same rate-limited IP.
- Added generation-based `_ROTATE_LOCK` / `_ROTATE_GEN` deduplication so concurrent `429` requests share one rotation instead of triggering back-to-back WARP disconnects that tear down in-flight connections.
- Retried transient `502`/`503`/`504` upstream responses (e.g., `ECONNRESET` during proxy rotation) up to `BRIDGE_UPSTREAM_RETRIES` (default `3`), and raised rate-limit rotation retries to `BRIDGE_RATE_LIMIT_RETRIES` (default `4`).

## [0.5.1] - 2026-11-24

### Changed
- Reorganized repository into a standard `src/opencode_bridge/` Python package (`__main__.py` entrypoint + `runtime/bun_upstream.js`), `deploy/systemd/`, `tests/__init__.py`, and `pyproject.toml` (still zero third-party `pip` dependencies).
- Updated `Dockerfile` and root `install.sh` to run `python3 -m opencode_bridge` with `PYTHONPATH` and clean up legacy flat files in `$BRIDGE_STATE_DIR` while preserving `api_key` and signature cache.

## [0.5.0] - 2026-11-24

### Added
- Bundled canonical OpenCode v2 signature snapshot (`bridge_defaults.py`) and GitHub upstream tracker (`anomalyco/opencode` via `OPENCODE_UPDATE_URL` + `raw.githubusercontent.com`) in `bridge_signature.py`, removing `opencode` CLI as a hard runtime dependency.
- Lightweight non-root `Dockerfile` (Python 3 + Bun only), `.dockerignore`, `docker-compose.yml`, and `render.yaml` free-tier blueprint with `BRIDGE_API_KEY` auth.
- PaaS `PORT` fallback (`BRIDGE_PORT` -> `PORT` -> `29471`), `signature_source` on `/health`, and auth/signature source startup log.

### Changed
- Changed default `BRIDGE_SIGNATURE_CHECK_INTERVAL` to `86400` seconds (24h).
- Kept `OPENCODE_BIN` optional (`install.sh` `--signature-mode auto|github|cli|static`); `BRIDGE_SIGNATURE_MODE=cli` preserves local CLI capture.

## [0.4.1] - 2026-11-24

### Added
- Automatic `BUN_BIN` and `OPENCODE_BIN` binary path resolution across env overrides, common install locations, and `PATH` in `bridge_config.py`, with unit tests in `tests/test_config.py`.
- Portable `install.sh` CLI flags (`--dest`, `--python`, `--bun`, `--opencode`, `--host`, `--port`, `--upstream-port`, `--non-interactive`) with auto-detection and interactive prompt fallback.

## [0.4.0] - 2026-11-24

### Added
- Generic rate-limit rotation webhook support (`BRIDGE_UPSTREAM_ROTATE_URL`, optional `BRIDGE_UPSTREAM_ROTATE_TOKEN`) to trigger external proxy rotation on `429` before retrying, plus `rotate_webhook_configured` status on `/health`.
- Documented recommended pairing with [`warp-rotating-proxy`](https://github.com/naobyprawira/warp-rotating-proxy) in `README.md`.

## [0.3.0] - 2026-11-24

### Added
- Periodic OpenCode CLI version and call-format monitor (`BRIDGE_SIGNATURE_CHECK_INTERVAL`, default `3600`s) that re-captures immediately when `opencode --version` changes.
- Automatic adaptation for `User-Agent`, static `x-*` headers, system prompt, and base tools, plus explicit `WARNING:` logs when OpenCode's endpoint path, headers, body keys, system message shape, or tool schema drift.

## [0.2.1] - 2026-11-24

### Changed
- Omitted upstream soft `limit` metadata from `/v1/models` responses so clients are not artificially capped below a model's real backend context window.

## [0.2.0] - 2026-11-24

### Added
- Live `/v1/models` catalog auto-sync from OpenCode's upstream models endpoint every 5 minutes (`BRIDGE_MODELS_TTL`, configurable via `OPENCODE_MODELS_URL`, plus `?refresh=1` on-demand refresh) with full `modalities` and `capabilities` metadata.
- Configurable HTTP method support (`GET`/`HEAD`/`POST`) in `bun_upstream.js` and `UpstreamManager.fetch_json`.

## [0.1.0] - 2026-11-24

### Added
- OpenAI-compatible HTTP server (`/health`, `/v1/models`, `/v1/chat/completions`) with Bearer token auth.
- Bun TLS/HTTP2 upstream forwarder (`bun_upstream.js`) with optional `BRIDGE_UPSTREAM_PROXY` support.
- Automatic 24h OpenCode signature and active free-model catalog capture (`bridge_signature.py`).
- Full OpenAI `tools`, `tool_calls`, `reasoning_content`, and multi-turn message preservation (`bridge_protocol.py`).
- Live token-by-token SSE streaming with 8-second `: keepalive\n\n` heartbeats to prevent idle timeouts, plus non-streaming JSON assembly.
- Portable `systemd` user service unit, `install.sh`, and unit test suite (`tests/test_protocol.py`, `tests/test_upstream.py`).
