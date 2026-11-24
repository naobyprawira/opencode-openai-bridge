# opencode-openai-bridge

OpenAI-compatible HTTP bridge to OpenCode's free inference models (`deepseek-v4.1-flash-free`, `big-pickle`, `beam-free`, etc.).

## Architecture

```
Client / 9router
   │  (HTTP/1.1 OpenAI API, port 29471)
   ▼
opencode_openai_bridge.py   (Python 3 stdlib: auth, tool/message merge, SSE keepalive)
   │  (localhost port 29472)
   ▼
bun_upstream.js             (Bun.serve + fetch: native TLS/HTTP2 stack, zero-copy SSE)
   │  (direct HTTPS, or optional BRIDGE_UPSTREAM_PROXY)
   ▼
opencode.ai/inference/openai/v1/chat/completions
```

### Why this stack?
- **Python 3 stdlib** (`opencode_openai_bridge.py`) keeps zero external `pip` dependencies and manages auth, OpenAI protocol translation (`tools`, `tool_calls`, `reasoning_content`), and 8s SSE `: keepalive\n\n` heartbeats (prevents Cloudflare `524` idle timeouts).
- **Bun** (`bun_upstream.js`) provides Bun's BoringSSL TLS/HTTP2 fingerprint required by `opencode.ai/inference/openai/v1/chat/completions` and streams SSE chunks live.
- **Signature & format monitor** (`bridge_signature.py`) runs `opencode --standalone` against an ephemeral localhost listener at startup, on `BRIDGE_SIGNATURE_TTL`, or whenever `opencode --version` changes (checked every `BRIDGE_SIGNATURE_CHECK_INTERVAL` seconds). It auto-adapts `User-Agent`, static `x-*` headers, system prompt, and base tools, and logs `WARNING:` if OpenCode's request path or JSON call format drifts.

## Prerequisites

- Python 3.10+
- [Bun](https://bun.sh/) (auto-detected via `BUN_BIN`, `~/.bun/bin/bun`, `/usr/local/bin/bun`, `/opt/homebrew/bin/bun`, or `PATH`)
- [OpenCode CLI](https://opencode.ai/) (auto-detected via `OPENCODE_BIN`, `~/.opencode/bin/opencode`, `/usr/local/bin/opencode`, `/opt/homebrew/bin/opencode`, or `PATH`)

## Installation

```bash
bash install.sh
```

`install.sh` auto-detects `python3`, `bun`, `opencode`, and state directory paths, prompts interactively if any binary cannot be found, and injects the resolved paths into `~/.config/systemd/user/opencode-openai-bridge.service`. You can also pass explicit options:

```bash
bash install.sh \
  --dest ~/.local/share/opencode-bridge \
  --python /usr/bin/python3 \
  --bun ~/.bun/bin/bun \
  --opencode ~/.opencode/bin/opencode \
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
| `BRIDGE_PORT` | `29471` | Public bridge port |
| `UPSTREAM_PORT` | `29472` | Local Bun forwarder port |
| `BRIDGE_STATE_DIR` | `~/.local/share/opencode-bridge` | State & `api_key` directory |
| `BUN_BIN` | *(auto-detected)* | Override path to `bun` executable |
| `OPENCODE_BIN` | *(auto-detected)* | Override path to `opencode` executable |
| `BRIDGE_API_KEY` | *(auto-generated)* | Override static Bearer key |
| `BRIDGE_UPSTREAM_PROXY` | *(empty / direct)* | Optional HTTP/HTTPS proxy URL for Bun `fetch()` |
| `BRIDGE_UPSTREAM_ROTATE_URL` | *(empty / disabled)* | Optional `POST` webhook URL triggered on `429` before retrying (e.g. `http://127.0.0.1:40001/_rotate`) |
| `BRIDGE_UPSTREAM_ROTATE_TOKEN` | *(empty)* | Optional Bearer token sent to `BRIDGE_UPSTREAM_ROTATE_URL` |
| `BRIDGE_MODELS_TTL` | `300` | Live `/v1/models` catalog refresh interval (seconds) |
| `BRIDGE_SIGNATURE_TTL` | `86400` | OpenCode signature refresh interval (seconds) |
| `BRIDGE_SIGNATURE_CHECK_INTERVAL` | `3600` | Periodic `opencode --version` / format check interval (`0` disables) |
| `BRIDGE_REQUEST_TIMEOUT` | `240` | Upstream request timeout (seconds) |

## Testing

```bash
python3 -m unittest discover -s tests -v
```
