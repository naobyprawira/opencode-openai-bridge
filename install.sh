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
SIGNATURE_MODE="${BRIDGE_SIGNATURE_MODE:-auto}"
NON_INTERACTIVE="${NON_INTERACTIVE:-0}"

usage() {
  cat <<EOF
Usage: bash install.sh [options]

Options:
  --dest DIR              Install/state directory (default: $DEST)
  --python PATH           Python 3 binary path (auto-detected)
  --bun PATH              Bun binary path (auto-detected)
  --opencode PATH         Optional OpenCode CLI binary path (auto-detected)
  --signature-mode MODE   auto | github | cli | static (default: $SIGNATURE_MODE)
  --host ADDR             Bridge listen host (default: $BRIDGE_HOST)
  --port PORT             Bridge listen port (default: $BRIDGE_PORT)
  --upstream-port PORT    Local Bun forwarder port (default: $UPSTREAM_PORT)
  --non-interactive       Fail instead of prompting if a required path is missing
  -h, --help              Show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dest) DEST="$2"; shift 2 ;;
    --python) PYTHON_BIN="$2"; shift 2 ;;
    --bun) BUN_BIN="$2"; shift 2 ;;
    --opencode) OPENCODE_BIN="$2"; shift 2 ;;
    --signature-mode) SIGNATURE_MODE="$2"; shift 2 ;;
    --host) BRIDGE_HOST="$2"; shift 2 ;;
    --port) BRIDGE_PORT="$2"; shift 2 ;;
    --upstream-port) UPSTREAM_PORT="$2"; shift 2 ;;
    --non-interactive) NON_INTERACTIVE=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 1 ;;
  esac
done

detect_optional() {
  local current="$1"
  shift
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
  return 1
}

detect_or_prompt() {
  local label="$1"
  local env_hint="$2"
  local current="$3"
  shift 3

  local found=""
  if found="$(detect_optional "$current" "$@")"; then
    echo "$found"
    return 0
  fi

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

if [[ "$SIGNATURE_MODE" == "cli" ]]; then
  OPENCODE_BIN="$(detect_or_prompt "opencode" "opencode" "$OPENCODE_BIN" \
    "$HOME/.opencode/bin/opencode" \
    "$(command -v opencode 2>/dev/null || true)" \
    "/usr/local/bin/opencode" "/usr/bin/opencode" "/opt/homebrew/bin/opencode")"
else
  OPENCODE_BIN="$(detect_optional "$OPENCODE_BIN" \
    "$HOME/.opencode/bin/opencode" \
    "$(command -v opencode 2>/dev/null || true)" \
    "/usr/local/bin/opencode" "/usr/bin/opencode" "/opt/homebrew/bin/opencode" || true)"
fi

mkdir -p "$DEST/src/opencode_bridge/runtime" "$UNIT_DIR"

rm -f \
  "$DEST/bridge_config.py" \
  "$DEST/bridge_defaults.py" \
  "$DEST/bridge_protocol.py" \
  "$DEST/bridge_signature.py" \
  "$DEST/bridge_upstream.py" \
  "$DEST/opencode_openai_bridge.py" \
  "$DEST/bun_upstream.js"

for f in \
  __init__.py \
  __main__.py \
  config.py \
  defaults.py \
  protocol.py \
  server.py \
  signature.py \
  upstream.py; do
  install -m 644 "$REPO_DIR/src/opencode_bridge/$f" "$DEST/src/opencode_bridge/$f"
done
install -m 755 "$REPO_DIR/src/opencode_bridge/runtime/bun_upstream.js" \
  "$DEST/src/opencode_bridge/runtime/bun_upstream.js"

{
  cat <<EOF
[Unit]
Description=OpenCode OpenAI-Compatible HTTP Bridge
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=$PYTHON_BIN -m opencode_bridge
Environment=PYTHONPATH=$DEST/src
Environment=BRIDGE_HOST=$BRIDGE_HOST
Environment=BRIDGE_PORT=$BRIDGE_PORT
Environment=UPSTREAM_PORT=$UPSTREAM_PORT
Environment=BRIDGE_STATE_DIR=$DEST
Environment=BRIDGE_SIGNATURE_MODE=$SIGNATURE_MODE
Environment=BUN_BIN=$BUN_BIN
EOF
  if [[ -n "$OPENCODE_BIN" ]]; then
    echo "Environment=OPENCODE_BIN=$OPENCODE_BIN"
  fi
  cat <<EOF
Environment=PYTHONUNBUFFERED=1
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
EOF
} > "$UNIT_FILE"

systemctl --user daemon-reload
systemctl --user enable opencode-openai-bridge.service
systemctl --user restart opencode-openai-bridge.service

echo "Installed opencode-openai-bridge to $DEST"
echo "  python3 : $PYTHON_BIN"
echo "  bun     : $BUN_BIN"
echo "  opencode: ${OPENCODE_BIN:-(optional, using GitHub tracker + bundled defaults)}"
echo "  sig_mode: $SIGNATURE_MODE"
echo "  api_key : $DEST/api_key"
systemctl --user --no-pager status opencode-openai-bridge.service || true
