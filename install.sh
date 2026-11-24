#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="${BRIDGE_STATE_DIR:-$HOME/.local/share/opencode-bridge}"
UNIT_DIR="$HOME/.config/systemd/user"
UNIT_FILE="$UNIT_DIR/opencode-openai-bridge.service"

PYTHON_BIN="${PYTHON_BIN:-}"
BUN_BIN="${BUN_BIN:-}"
OPENCODE_BIN="${OPENCODE_BIN:-}"
BRIDGE_HOST="${BRIDGE_HOST:-0.0.0.0}"
BRIDGE_PORT="${BRIDGE_PORT:-29471}"
UPSTREAM_PORT="${UPSTREAM_PORT:-29472}"
NON_INTERACTIVE="${NON_INTERACTIVE:-0}"

usage() {
  cat <<EOF
Usage: bash install.sh [options]

Options:
  --dest DIR              Install/state directory (default: $DEST)
  --python PATH           Python 3 binary path (auto-detected)
  --bun PATH              Bun binary path (auto-detected)
  --opencode PATH         OpenCode CLI binary path (auto-detected)
  --host ADDR             Bridge listen host (default: $BRIDGE_HOST)
  --port PORT             Bridge listen port (default: $BRIDGE_PORT)
  --upstream-port PORT    Local Bun forwarder port (default: $UPSTREAM_PORT)
  --non-interactive       Fail instead of prompting if a path is missing
  -h, --help              Show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dest) DEST="$2"; shift 2 ;;
    --python) PYTHON_BIN="$2"; shift 2 ;;
    --bun) BUN_BIN="$2"; shift 2 ;;
    --opencode) OPENCODE_BIN="$2"; shift 2 ;;
    --host) BRIDGE_HOST="$2"; shift 2 ;;
    --port) BRIDGE_PORT="$2"; shift 2 ;;
    --upstream-port) UPSTREAM_PORT="$2"; shift 2 ;;
    --non-interactive) NON_INTERACTIVE=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 1 ;;
  esac
done

detect_or_prompt() {
  local label="$1"
  local env_hint="$2"
  local current="$3"
  shift 3

  if [[ -n "$current" && -x "$current" ]]; then
    echo "$current"
    return 0
  fi

  local cand
  for cand in "$@"; do
    [[ -n "$cand" ]] || continue
    if [[ -x "$cand" ]]; then
      echo "$cand"
      return 0
    fi
  done

  if [[ "$NON_INTERACTIVE" != "1" && -t 0 ]]; then
    local entered=""
    while true; do
      read -rp "$label not auto-detected. Enter full path to $label: " entered
      entered="${entered/#\~/$HOME}"
      if [[ -n "$entered" && -x "$entered" ]]; then
        echo "$entered"
        return 0
      fi
      echo "Executable not found at '$entered'. Try again." >&2
    done
  fi

  echo "Error: $label not found. Pass --${env_hint} or set ${env_hint^^}_BIN." >&2
  return 1
}

PYTHON_BIN="$(detect_or_prompt "python3" "python" "$PYTHON_BIN" \
  "$(command -v python3 2>/dev/null || true)" \
  "/usr/bin/python3" "/usr/local/bin/python3")"

BUN_BIN="$(detect_or_prompt "bun" "bun" "$BUN_BIN" \
  "$HOME/.bun/bin/bun" \
  "$(command -v bun 2>/dev/null || true)" \
  "/usr/local/bin/bun" "/usr/bin/bun" "/opt/homebrew/bin/bun")"

OPENCODE_BIN="$(detect_or_prompt "opencode" "opencode" "$OPENCODE_BIN" \
  "$HOME/.opencode/bin/opencode" \
  "$(command -v opencode 2>/dev/null || true)" \
  "/usr/local/bin/opencode" "/usr/bin/opencode" "/opt/homebrew/bin/opencode")"

mkdir -p "$DEST" "$UNIT_DIR"

for f in \
  bridge_config.py \
  bridge_protocol.py \
  bridge_signature.py \
  bridge_upstream.py \
  opencode_openai_bridge.py \
  bun_upstream.js; do
  install -m 755 "$REPO_DIR/$f" "$DEST/$f"
done

cat > "$UNIT_FILE" <<EOF
[Unit]
Description=OpenCode OpenAI-Compatible HTTP Bridge
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=$PYTHON_BIN $DEST/opencode_openai_bridge.py
Environment=BRIDGE_HOST=$BRIDGE_HOST
Environment=BRIDGE_PORT=$BRIDGE_PORT
Environment=UPSTREAM_PORT=$UPSTREAM_PORT
Environment=BRIDGE_STATE_DIR=$DEST
Environment=BUN_BIN=$BUN_BIN
Environment=OPENCODE_BIN=$OPENCODE_BIN
Environment=PYTHONUNBUFFERED=1
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable opencode-openai-bridge.service
systemctl --user restart opencode-openai-bridge.service

echo "Installed opencode-openai-bridge to $DEST"
echo "  python3 : $PYTHON_BIN"
echo "  bun     : $BUN_BIN"
echo "  opencode: $OPENCODE_BIN"
echo "  api_key : $DEST/api_key"
systemctl --user --no-pager status opencode-openai-bridge.service || true
