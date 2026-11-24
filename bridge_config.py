"""Configuration, paths, logging, and local bridge API key management."""

from __future__ import annotations

import os
import secrets
import sys
import time


BRIDGE_HOST = os.environ.get("BRIDGE_HOST", "0.0.0.0")
BRIDGE_PORT = int(os.environ.get("BRIDGE_PORT", "29471"))
UPSTREAM_HOST = "127.0.0.1"
UPSTREAM_PORT = int(os.environ.get("UPSTREAM_PORT", "29472"))

STATE_DIR = os.path.expanduser(
    os.environ.get("BRIDGE_STATE_DIR", "~/.local/share/opencode-bridge")
)
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BUN_BIN = os.path.expanduser(os.environ.get("BUN_BIN", "~/.bun/bin/bun"))
OPENCODE_BIN = os.path.expanduser(
    os.environ.get("OPENCODE_BIN", "~/.opencode/bin/opencode")
)
BUN_UPSTREAM_JS = os.path.join(SCRIPT_DIR, "bun_upstream.js")
if not os.path.isfile(BUN_UPSTREAM_JS):
    BUN_UPSTREAM_JS = os.path.join(STATE_DIR, "bun_upstream.js")

API_KEY_FILE = os.path.join(STATE_DIR, "api_key")
SIGNATURE_FILE = os.path.join(STATE_DIR, "opencode_signature.json")

INFERENCE_URL = os.environ.get(
    "OPENCODE_INFERENCE_URL",
    "https://opencode.ai/inference/openai/v1/chat/completions",
)
UPSTREAM_PROXY_URL = os.environ.get("BRIDGE_UPSTREAM_PROXY", "").strip()

REQUEST_TIMEOUT_SEC = float(os.environ.get("BRIDGE_REQUEST_TIMEOUT", "240"))
SIGNATURE_TTL_SEC = float(os.environ.get("BRIDGE_SIGNATURE_TTL", "86400"))

DEFAULT_MODELS = [
    "beam-free",
    "big-pickle",
    "deepseek-v4.1-flash-free",
    "flint-40b-free",
    "hy4-flash-free",
    "iceman-free",
    "mimo-v2.6-flash-free",
    "nemotron-3.5-lightning-free",
    "nemotron-3.5-super-vl-free",
    "tidepool-alpha-free",
]


def log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    sys.stderr.write(f"[{ts}] {msg}\n")
    sys.stderr.flush()


def load_or_create_api_key() -> str:
    env_key = os.environ.get("BRIDGE_API_KEY", "").strip()
    if env_key:
        return env_key
    os.makedirs(STATE_DIR, exist_ok=True)
    if os.path.exists(API_KEY_FILE):
        with open(API_KEY_FILE, "r", encoding="utf-8") as f:
            key = f.read().strip()
            if key:
                return key
    key = "sk-ocbridge-" + secrets.token_urlsafe(24)
    fd = os.open(API_KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(key + "\n")
    return key
