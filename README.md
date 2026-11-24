# opencode-openai-bridge

OpenAI-compatible HTTP bridge to OpenCode's free inference models (`deepseek-v4.1-flash-free`, `big-pickle`, `beam-free`, etc.).

## Architecture

```
Client / 9router
   │  (HTTP/1.1 OpenAI API, port 29471)
   ▼
opencode_bridge.server      (Python 3 stdlib: auth, tool/message merge, SSE keepalive)
   │  (localhost port 29472)
   ▼
runtime/bun_upstream.js     (Bun.serve + fetch: native TLS/HTTP2 stack, zero-copy SSE)
   │  (direct HTTPS, or optional BRIDGE_UPSTREAM_PROXY)
   ▼
opencode.ai/inference/openai/v1/chat/completions
```

### Why this stack?
- **Python 3 stdlib** (`src/opencode_bridge/server.py`) keeps zero external `pip` dependencies and manages auth, OpenAI protocol translation (`tools`, `tool_calls`, `reasoning_content`), and 8s SSE `: keepalive\n\n` heartbeats (prevents Cloudflare `524` idle timeouts).
- **Bun** (`src/opencode_bridge/runtime/bun_upstream.js`) provides Bun's BoringSSL TLS/HTTP2 fingerprint required by `opencode.ai/inference/openai/v1/chat/completions` and streams SSE chunks live.
- **Signature & format monitor** (`src/opencode_bridge/signature.py`) ships with a bundled canonical OpenCode v2 signature snapshot (`src/opencode_bridge/defaults.py`) and tracks upstream releases and prompt/header files from [`anomalyco/opencode`](https://github.com/anomalyco/opencode) every `BRIDGE_SIGNATURE_CHECK_INTERVAL` seconds (default `86400`s / 24h) — **requiring zero local `opencode` CLI binary**. It auto-adapts `User-Agent` and `system_prompt` and logs `WARNING:` if headers or format drift. Setting `BRIDGE_SIGNATURE_MODE=cli` (with optional `OPENCODE_BIN`) captures from a local CLI instead.

## Prerequisites

- Python 3.10+
- [Bun](https://bun.sh/) (auto-detected via `BUN_BIN`, `~/.bun/bin/bun`, `/usr/local/bin/bun`, `/opt/homebrew/bin/bun`, or `PATH`)
- [OpenCode CLI](https://opencode.ai/) *(optional — only needed when `BRIDGE_SIGNATURE_MODE=cli`)*

## Installation

### Option 1: Docker (Zero Host Impact, Recommended for Containers / Cloud)

The included [`Dockerfile`](Dockerfile) bundles only Python 3, Bun, and the bridge scripts inside a non-root container (`BRIDGE_STATE_DIR=/data`) — no `opencode` CLI binary required (`~0 MB` CLI overhead, `~54 MB` standby RAM).

```bash
# Run with explicit Bearer auth key
BRIDGE_API_KEY="sk-your-secret-key" docker compose up -d --build

# Or build & run directly
docker build -t opencode-openai-bridge .
docker run -d --name opencode-openai-bridge \
  -p 29471:29471 \
  -e BRIDGE_API_KEY="sk-your-secret-key" \
  -v opencode_bridge_data:/data \
  opencode-openai-bridge
```

If `BRIDGE_API_KEY` is omitted, an auto-generated key is persisted in `/data/api_key`.

#### Free-Tier PaaS (Render / Koyeb / Railway / Fly.io)

`BRIDGE_PORT` automatically falls back to the platform `PORT` environment variable. A [`render.yaml`](render.yaml) Docker blueprint is included with `plan: free`, `healthCheckPath: /health`, and auto-generated `BRIDGE_API_KEY` auth.

> [!NOTE]
> Unprivileged PaaS containers cannot run `warp-svc` locally. On shared datacenter IPs, OpenCode may hit `429 FreeUsageLimitError` unless you point `BRIDGE_UPSTREAM_PROXY` (+ optional `BRIDGE_UPSTREAM_ROTATE_URL`) to an external proxy such as `warp-rotating-proxy`.

### Option 2: Native Linux (`systemd` user service)

```bash
bash install.sh
```

`install.sh` auto-detects `python3`, `bun`, optional `opencode`, and state directory paths, prompts interactively if a required binary cannot be found, and injects resolved paths into `~/.config/systemd/user/opencode-openai-bridge.service`. You can also pass explicit options:

```bash
bash install.sh \
  --dest ~/.local/share/opencode-bridge \
  --python /usr/bin/python3 \
  --bun ~/.bun/bin/bun \
  --signature-mode auto \
  --host 0.0.0.0 \
  --port 29471 \
  --upstream-port 29472 \
  --non-interactive
```

Run `bash install.sh --help` for all options.

To allow Docker containers (e.g. `9router` on `docker0`) to reach port `29471` when UFW is enabled:

```bash
sudo ufw allow in on docker0 to any port 29471 proto tcp
```

### Recommended Pairing: `warp-rotating-proxy` (Anti-`429` Egress)

While `opencode-openai-bridge` works standalone over direct HTTPS, OpenCode enforces per-IP free usage rate limits (`429 FreeUsageLimitError`). **It works best when combined with [`warp-rotating-proxy`](https://github.com/naobyprawira/warp-rotating-proxy)** so rate-limited requests automatically rotate Cloudflare WARP egress and retry:

```ini
Environment=BRIDGE_UPSTREAM_PROXY=http://127.0.0.1:40001
Environment=BRIDGE_UPSTREAM_ROTATE_URL=http://127.0.0.1:40001/_rotate
```

## Endpoints & Usage

- `GET /health`
- `GET /v1/models` (Bearer auth; auto-syncs live free chat models & capabilities every `BRIDGE_MODELS_TTL` seconds, supports `?refresh=1`; omits upstream soft `limit` metadata so clients can use each model's full backend context window)
- `POST /v1/chat/completions` (Bearer auth, supports `stream: true` and `stream: false`, `tools`, `tool_calls`, `reasoning_content`, and vision `image_url` on vision-capable models)

```bash
KEY=$(cat ~/.local/share/opencode-bridge/api_key)
curl -s http://127.0.0.1:29471/v1/models -H "Authorization: Bearer $KEY"

curl -N http://127.0.0.1:29471/v1/chat/completions \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "deepseek-v4.1-flash-free",
    "stream": true,
    "messages": [{"role": "user", "content": "Hello!"}]
  }'
```

## Configuration (Environment Variables)

| Variable | Default | Description |
|---|---|---|
| `BRIDGE_HOST` | `0.0.0.0` | Listen address |
| `BRIDGE_PORT` | `PORT` or `29471` | Public bridge port (falls back to PaaS `PORT` env var) |
| `UPSTREAM_PORT` | `29472` | Local Bun forwarder port |
| `BRIDGE_STATE_DIR` | `~/.local/share/opencode-bridge` | State & `api_key` directory |
| `BUN_BIN` | *(auto-detected)* | Override path to `bun` executable |
| `OPENCODE_BIN` | *(optional / auto-detected)* | Optional path to `opencode` executable (used when `BRIDGE_SIGNATURE_MODE=cli`) |
| `BRIDGE_SIGNATURE_MODE` | `auto` | `auto` / `github` (track `anomalyco/opencode` + bundled fallback), `cli`, or `static` |
| `OPENCODE_GITHUB_REPO` | `anomalyco/opencode` | Upstream GitHub repository tracked for signature updates |
| `OPENCODE_UPDATE_URL` | `https://opencode.ai/update/api/latest/cli/npm` | Upstream release version feed |
| `BRIDGE_API_KEY` | *(auto-generated)* | Override static Bearer key |
| `BRIDGE_UPSTREAM_PROXY` | *(empty / direct)* | Optional HTTP/HTTPS proxy URL for Bun `fetch()` |
| `BRIDGE_UPSTREAM_ROTATE_URL` | *(empty / disabled)* | Optional `POST` webhook URL triggered on `429` before retrying (e.g. `http://127.0.0.1:40001/_rotate`) |
| `BRIDGE_UPSTREAM_ROTATE_TOKEN` | *(empty)* | Optional Bearer token sent to `BRIDGE_UPSTREAM_ROTATE_URL` |
| `BRIDGE_MODELS_TTL` | `300` | Live `/v1/models` catalog refresh interval (seconds) |
| `BRIDGE_SIGNATURE_TTL` | `86400` | OpenCode signature cache TTL (seconds) |
| `BRIDGE_SIGNATURE_CHECK_INTERVAL` | `86400` | Periodic upstream version/format check interval (`0` disables) |
| `BRIDGE_RATE_LIMIT_RETRIES` | `2` | Maximum upstream IP rotation retries on `429` before failing |
| `BRIDGE_UPSTREAM_RETRIES` | `2` | Maximum retries on transient `502`/`503`/`504` or connection resets |
| `BRIDGE_RETRY_BUDGET_SEC` | `35` | Max total seconds spent retrying/rotating before returning upstream status (prevents downstream 120s connect timeouts) |
| `BRIDGE_MAX_CONCURRENT_UPSTREAM` | `3` | Maximum simultaneous upstream connection handshakes (prevents fresh-IP `429` stampedes after rotation) |
| `BRIDGE_ROTATE_STREAM_GRACE_SEC` | `10` | Max seconds a `429` rotation waits for active `200 OK` responses/streams to finish before disconnecting WARP |
| `BRIDGE_REQUEST_TIMEOUT` | `240` | Upstream request timeout (seconds) |

## Testing

```bash
python3 -m unittest discover -v
```
