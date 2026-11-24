#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="${BRIDGE_STATE_DIR:-$HOME/.local/share/opencode-bridge}"
UNIT_DIR="$HOME/.config/systemd/user"
UNIT_FILE="$UNIT_DIR/opencode-openai-bridge.service"

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

sed "s|%h/.local/share/opencode-bridge|$DEST|g" \
  "$REPO_DIR/systemd/opencode-openai-bridge.service" > "$UNIT_FILE"

systemctl --user daemon-reload
systemctl --user enable opencode-openai-bridge.service
systemctl --user restart opencode-openai-bridge.service

echo "Installed opencode-openai-bridge to $DEST"
echo "API key file: $DEST/api_key"
systemctl --user --no-pager status opencode-openai-bridge.service || true
