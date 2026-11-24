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
- **Signature capture** (`bridge_signature.py`) runs `opencode --standalone` against an ephemeral localhost listener once every 24h to cache the current User-Agent, system prompt, base tools, and active free models.

## Prerequisites

- Python 3.10+
- [Bun](https://bun.sh/) (`~/.bun/bin/bun`)
- [OpenCode CLI](https://opencode.ai/) (`~/.opencode/bin/opencode`)

## Installation

```bash
bash install.sh
```

To allow Docker containers (e.g. `9router` on `docker0`) to reach port `29471` when UFW is enabled:

```bash
sudo ufw allow in on docker0 to any port 29471 proto tcp
```

## Endpoints & Usage

- `GET /health`
- `GET /v1/models` (Bearer auth)
- `POST /v1/chat/completions` (Bearer auth, supports `stream: true` and `stream: false`, `tools`, `tool_calls`, `reasoning_content`)

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
| `BRIDGE_API_KEY` | *(auto-generated)* | Override static Bearer key |
| `BRIDGE_UPSTREAM_PROXY` | *(empty / direct)* | Optional HTTP/HTTPS proxy URL for Bun `fetch()` |
| `BRIDGE_REQUEST_TIMEOUT` | `240` | Upstream request timeout (seconds) |

## Testing

```bash
python3 -m unittest discover -s tests -v
```
