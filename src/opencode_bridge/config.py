"""Configuration, paths, logging, and local bridge API key management."""

from __future__ import annotations

import json
import os
import secrets
import shutil
import sys
import time
from urllib.parse import urlsplit


def derive_rotate_url(proxy_url: str) -> str:
    """Derive default /_rotate webhook URL from a warp-rotating-proxy URL."""
    parsed = urlsplit(proxy_url.strip())
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}/_rotate"
    return ""


def parse_upstream_workers(
    raw_workers: str,
    fallback_proxy: str = "",
    fallback_rotate: str = "",
    fallback_token: str = "",
    *,
    worker_prefix: str = "",
    worker_base_port: int = 0,
    standby_workers: int = 0,
    max_workers: int = 0,
) -> list[dict[str, object]]:
    """Parse BRIDGE_UPSTREAM_WORKERS (CSV or JSON) with single-proxy or fleet fallback."""
    spec = raw_workers.strip()
    workers: list[dict[str, object]] = []
    if spec:
        if spec.startswith("["):
            try:
                items = json.loads(spec)
            except Exception:
                items = []
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, str) and item.strip():
                        p = item.strip()
                        workers.append(
                            {
                                "proxy": p,
                                "rotate": derive_rotate_url(p),
                                "token": fallback_token,
                            }
                        )
                    elif isinstance(item, dict):
                        p = str(item.get("proxy") or item.get("proxy_url") or "").strip()
                        r = str(item.get("rotate") or item.get("rotate_url") or "").strip()
                        t = str(item.get("token") or item.get("rotate_token") or fallback_token).strip()
                        if p or r:
                            entry: dict[str, object] = {
                                "proxy": p,
                                "rotate": r or (derive_rotate_url(p) if p else ""),
                                "token": t,
                            }
                            if item.get("container"):
                                entry["container"] = str(item["container"]).strip()
                            if "on_demand" in item:
                                entry["on_demand"] = bool(item["on_demand"])
                            workers.append(entry)
        else:
            for part in spec.split(","):
                p = part.strip()
                if not p:
                    continue
                workers.append(
                    {
                        "proxy": p,
                        "rotate": derive_rotate_url(p),
                        "token": fallback_token,
                    }
                )
        if len(workers) == 1 and fallback_rotate and not spec.startswith("["):
            workers[0]["rotate"] = fallback_rotate
    elif fallback_proxy or fallback_rotate:
        workers.append(
            {
                "proxy": fallback_proxy,
                "rotate": fallback_rotate,
                "token": fallback_token,
            }
        )
    elif max_workers > 0 and (worker_base_port > 0 or worker_prefix):
        base_port = worker_base_port if worker_base_port > 0 else 40011
        prefix = worker_prefix or "warp-worker"
        for idx in range(max_workers):
            port = base_port + idx
            p = f"http://127.0.0.1:{port}"
            workers.append(
                {
                    "proxy": p,
                    "rotate": derive_rotate_url(p),
                    "token": fallback_token,
                    "container": f"{prefix}-{idx + 1}",
                    "on_demand": (standby_workers > 0 and idx >= standby_workers),
                }
            )
    if max_workers > 0 and len(workers) > max_workers:
        workers = workers[:max_workers]
    return workers


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
PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
BUN_UPSTREAM_JS = os.path.join(PACKAGE_DIR, "runtime", "bun_upstream.js")
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
WORKERS_STANDBY = max(0, int(os.environ.get("BRIDGE_WORKERS_STANDBY", "0") or "0"))
WORKERS_MAX = max(0, int(os.environ.get("BRIDGE_WORKERS_MAX", "0") or "0"))
if WORKERS_MAX > 0 and WORKERS_STANDBY > WORKERS_MAX:
    WORKERS_STANDBY = WORKERS_MAX
WORKER_IDLE_SEC = max(0.0, float(os.environ.get("BRIDGE_WORKER_IDLE_SEC", "600") or "600"))
WORKER_SUMMON_TIMEOUT_SEC = max(
    2.0, float(os.environ.get("BRIDGE_WORKER_SUMMON_TIMEOUT_SEC", "25") or "25")
)
WORKER_REAPER_INTERVAL_SEC = max(
    5.0, float(os.environ.get("BRIDGE_WORKER_REAPER_INTERVAL_SEC", "30") or "30")
)
WORKER_PREFIX = os.environ.get("BRIDGE_WORKER_PREFIX", "").strip()
WORKER_BASE_PORT = max(0, int(os.environ.get("BRIDGE_WORKER_BASE_PORT", "0") or "0"))
WORKER_DOCKER_BIN = resolve_binary(
    "BRIDGE_WORKER_DOCKER_BIN",
    "docker",
    ["/usr/bin/docker", "/usr/local/bin/docker"],
)
WORKER_DOCKER_SUDO = os.environ.get("BRIDGE_WORKER_DOCKER_SUDO", "auto").strip().lower()

UPSTREAM_WORKERS = parse_upstream_workers(
    os.environ.get("BRIDGE_UPSTREAM_WORKERS", ""),
    fallback_proxy=UPSTREAM_PROXY_URL,
    fallback_rotate=UPSTREAM_ROTATE_URL,
    fallback_token=UPSTREAM_ROTATE_TOKEN,
    worker_prefix=WORKER_PREFIX,
    worker_base_port=WORKER_BASE_PORT,
    standby_workers=WORKERS_STANDBY,
    max_workers=WORKERS_MAX,
)

REQUEST_TIMEOUT_SEC = float(os.environ.get("BRIDGE_REQUEST_TIMEOUT", "240"))
SIGNATURE_TTL_SEC = float(os.environ.get("BRIDGE_SIGNATURE_TTL", "86400"))
SIGNATURE_CHECK_INTERVAL_SEC = float(
    os.environ.get("BRIDGE_SIGNATURE_CHECK_INTERVAL", "86400")
)
MODELS_TTL_SEC = float(os.environ.get("BRIDGE_MODELS_TTL", "300"))
RATE_LIMIT_RETRIES = max(0, int(os.environ.get("BRIDGE_RATE_LIMIT_RETRIES", "2")))
UPSTREAM_RETRY_COUNT = max(0, int(os.environ.get("BRIDGE_UPSTREAM_RETRIES", "2")))
RETRY_BUDGET_SEC = max(5.0, float(os.environ.get("BRIDGE_RETRY_BUDGET_SEC", "35")))
MAX_CONCURRENT_UPSTREAM = max(
    1, int(os.environ.get("BRIDGE_MAX_CONCURRENT_UPSTREAM", "3"))
)
STREAM_GRACE_SEC = max(
    0.0, float(os.environ.get("BRIDGE_ROTATE_STREAM_GRACE_SEC", "10"))
)
WORKER_PACING_MS = max(0, int(os.environ.get("BRIDGE_WORKER_PACING_MS", "200")))
RECENT_SUCCESS_GRACE_SEC = max(
    0.0, float(os.environ.get("BRIDGE_RECENT_SUCCESS_GRACE_SEC", "12"))
)
MIN_ROTATE_BUDGET_SEC = max(
    0.0, float(os.environ.get("BRIDGE_MIN_ROTATE_BUDGET_SEC", "8"))
)

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
