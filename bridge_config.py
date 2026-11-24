"""Configuration, paths, logging, and local bridge API key management."""

from __future__ import annotations

import os
import secrets
import shutil
import sys
import time


def resolve_binary(env_var: str, binary_name: str, extra_candidates: list[str]) -> str:
    """Resolve binary path from env override, common install locations, or PATH."""
    override = os.environ.get(env_var, "").strip()
    if override:
        return os.path.expanduser(override)
    for raw in extra_candidates:
        cand = os.path.expanduser(raw)
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    found = shutil.which(binary_name)
    if found:
        return found
    return os.path.expanduser(extra_candidates[0]) if extra_candidates else binary_name


def resolve_port(env_vars: tuple[str, ...], default: int) -> int:
    """Resolve port from the first non-empty env var in priority order."""
    for name in env_vars:
        raw = os.environ.get(name, "").strip()
        if raw:
            return int(raw)
    return default


BRIDGE_HOST = os.environ.get("BRIDGE_HOST", "0.0.0.0")
BRIDGE_PORT = resolve_port(("BRIDGE_PORT", "PORT"), 29471)
UPSTREAM_HOST = "127.0.0.1"
UPSTREAM_PORT = resolve_port(("UPSTREAM_PORT",), 29472)

STATE_DIR = os.path.expanduser(
    os.environ.get("BRIDGE_STATE_DIR", "~/.local/share/opencode-bridge")
)
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BUN_BIN = resolve_binary(
    "BUN_BIN",
    "bun",
    [
        "~/.bun/bin/bun",
        "/usr/local/bin/bun",
        "/usr/bin/bun",
        "/opt/homebrew/bin/bun",
        "/home/linuxbrew/.linuxbrew/bin/bun",
    ],
)
OPENCODE_BIN = resolve_binary(
    "OPENCODE_BIN",
    "opencode",
    [
        "~/.opencode/bin/opencode",
        "/usr/local/bin/opencode",
        "/usr/bin/opencode",
        "/opt/homebrew/bin/opencode",
        "/home/linuxbrew/.linuxbrew/bin/opencode",
    ],
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
MODELS_URL = os.environ.get(
    "OPENCODE_MODELS_URL",
    "https://opencode.ai/inference/openai/v1/models",
)
OPENCODE_UPDATE_URL = os.environ.get(
    "OPENCODE_UPDATE_URL",
    "https://opencode.ai/update/api/latest/cli/npm",
).strip()
OPENCODE_GITHUB_REPO = os.environ.get(
    "OPENCODE_GITHUB_REPO",
    "anomalyco/opencode",
).strip()
SIGNATURE_MODE = os.environ.get("BRIDGE_SIGNATURE_MODE", "auto").strip().lower()

UPSTREAM_PROXY_URL = os.environ.get("BRIDGE_UPSTREAM_PROXY", "").strip()
UPSTREAM_ROTATE_URL = os.environ.get("BRIDGE_UPSTREAM_ROTATE_URL", "").strip()
UPSTREAM_ROTATE_TOKEN = os.environ.get("BRIDGE_UPSTREAM_ROTATE_TOKEN", "").strip()

REQUEST_TIMEOUT_SEC = float(os.environ.get("BRIDGE_REQUEST_TIMEOUT", "240"))
SIGNATURE_TTL_SEC = float(os.environ.get("BRIDGE_SIGNATURE_TTL", "86400"))
SIGNATURE_CHECK_INTERVAL_SEC = float(
    os.environ.get("BRIDGE_SIGNATURE_CHECK_INTERVAL", "86400")
)
MODELS_TTL_SEC = float(os.environ.get("BRIDGE_MODELS_TTL", "300"))

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
