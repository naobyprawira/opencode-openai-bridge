# Changelog

All notable changes to `opencode-openai-bridge` will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
