"""Captures, monitors, and auto-adapts OpenCode's canonical request signature."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

from bridge_config import (
    DEFAULT_MODELS,
    OPENCODE_BIN,
    SIGNATURE_CHECK_INTERVAL_SEC,
    SIGNATURE_FILE,
    SIGNATURE_TTL_SEC,
    STATE_DIR,
    log,
)


_TRANSPORT_HEADERS = frozenset(
    {
        "host",
        "connection",
        "accept-encoding",
        "content-length",
        "content-type",
        "accept",
        "authorization",
        "user-agent",
    }
)
_DYNAMIC_HEADERS = frozenset(
    {
        "b3",
        "traceparent",
        "x-opencode-project",
        "x-opencode-session",
        "x-opencode-session-id",
        "x-session-affinity",
        "x-session-id",
    }
)
_EXPECTED_PATH = "/v1/chat/completions"
_EXPECTED_HEADERS = frozenset(
    {
        "authorization",
        "b3",
        "traceparent",
        "user-agent",
        "x-opencode-client",
        "x-opencode-project",
        "x-opencode-session",
        "x-opencode-session-id",
        "x-session-affinity",
        "x-session-id",
    }
)
_EXPECTED_BODY_KEYS = frozenset(
    {
        "max_completion_tokens",
        "messages",
        "model",
        "stream",
        "stream_options",
        "tools",
    }
)


def _tool_names(tools: list[dict[str, Any]] | None) -> list[str]:
    names: list[str] = []
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        fn = t.get("function") if isinstance(t.get("function"), dict) else {}
        name = fn.get("name") or t.get("name")
        if isinstance(name, str) and name:
            names.append(name)
    return names


def extract_static_headers(raw_headers: dict[str, str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in raw_headers.items():
        lk = k.lower()
        if lk in _TRANSPORT_HEADERS or lk in _DYNAMIC_HEADERS:
            continue
        out[lk] = str(v)
    return dict(sorted(out.items()))


def extract_header_names(raw_headers: dict[str, str]) -> list[str]:
    ignored = _TRANSPORT_HEADERS - {"authorization", "user-agent"}
    return sorted({k.lower() for k in raw_headers if k.lower() not in ignored})


def detect_signature_drift(
    prev: dict[str, Any] | None, fresh: dict[str, Any]
) -> tuple[list[str], list[str]]:
    """Compare a fresh capture against previous/expected format.

    Returns (auto_adapted_notices, call_format_warnings).
    """
    adapted: list[str] = []
    warnings: list[str] = []

    if prev:
        if prev.get("cli_version") and prev.get("cli_version") != fresh.get("cli_version"):
            adapted.append(
                f"cli_version {prev.get('cli_version')} -> {fresh.get('cli_version')}"
            )
        if prev.get("user_agent") and prev.get("user_agent") != fresh.get("user_agent"):
            adapted.append(
                f"user_agent {prev.get('user_agent')} -> {fresh.get('user_agent')}"
            )
        if prev.get("static_headers") != fresh.get("static_headers"):
            adapted.append(
                f"static_headers {prev.get('static_headers')} -> {fresh.get('static_headers')}"
            )
        if prev.get("system_prompt") and prev.get("system_prompt") != fresh.get("system_prompt"):
            adapted.append(
                f"system_prompt updated (len {len(prev.get('system_prompt', ''))} -> {len(fresh.get('system_prompt', ''))})"
            )
        prev_tools = set(_tool_names(prev.get("base_tools")))
        fresh_tools = set(_tool_names(fresh.get("base_tools")))
        if prev_tools and prev_tools != fresh_tools:
            added = sorted(fresh_tools - prev_tools)
            removed = sorted(prev_tools - fresh_tools)
            adapted.append(f"base_tools changed (added={added}, removed={removed})")

    fresh_path = fresh.get("request_path") or _EXPECTED_PATH
    if fresh_path != _EXPECTED_PATH:
        warnings.append(f"endpoint path changed: expected {_EXPECTED_PATH}, got {fresh_path}")
    elif prev and prev.get("request_path") and prev.get("request_path") != fresh_path:
        warnings.append(f"endpoint path changed: {prev.get('request_path')} -> {fresh_path}")

    base_headers = set(prev.get("header_names") or _EXPECTED_HEADERS) if prev else set(_EXPECTED_HEADERS)
    fresh_headers = set(fresh.get("header_names") or [])
    if fresh_headers:
        added_h = sorted(fresh_headers - base_headers)
        removed_h = sorted(base_headers - fresh_headers)
        if added_h or removed_h:
            warnings.append(f"request headers drifted (added={added_h}, removed={removed_h})")

    base_keys = set(prev.get("body_keys") or _EXPECTED_BODY_KEYS) if prev else set(_EXPECTED_BODY_KEYS)
    fresh_keys = set(fresh.get("body_keys") or [])
    if fresh_keys:
        added_k = sorted(fresh_keys - base_keys)
        removed_k = sorted(base_keys - fresh_keys)
        if added_k or removed_k:
            warnings.append(f"request body keys drifted (added={added_k}, removed={removed_k})")

    sys_role = fresh.get("system_role") or "system"
    sys_type = fresh.get("system_content_type") or "str"
    if sys_role != "system" or sys_type != "str":
        warnings.append(
            f"system message format drifted (role={sys_role}, content_type={sys_type})"
        )

    tool_types = set(fresh.get("tool_types") or ["function"])
    if tool_types != {"function"}:
        warnings.append(f"tool schema type drifted (types={sorted(tool_types)})")

    return adapted, warnings


class SignatureManager:
    """Captures, monitors, and caches OpenCode's canonical call signature."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: dict[str, Any] | None = None
        self._stop_evt = threading.Event()
        self._monitor_thread: threading.Thread | None = None

    def _get_cli_version(self) -> str:
        try:
            r = subprocess.run(
                [OPENCODE_BIN, "--version"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            ver = r.stdout.strip()
            if ver:
                return ver
        except Exception:
            pass
        return ""

    @staticmethod
    def _is_cache_complete(cached: Any) -> bool:
        return (
            isinstance(cached, dict)
            and bool(cached.get("system_prompt"))
            and isinstance(cached.get("base_tools"), list)
            and bool(cached["base_tools"])
            and bool(cached.get("body_keys"))
            and bool(cached.get("header_names"))
        )

    def _load_disk_cache(self) -> dict[str, Any] | None:
        if not os.path.exists(SIGNATURE_FILE):
            return None
        try:
            with open(SIGNATURE_FILE, "r", encoding="utf-8") as f:
                cached = json.load(f)
            if isinstance(cached, dict):
                return cached
        except Exception:
            pass
        return None

    def _save_disk_cache(self, data: dict[str, Any]) -> None:
        try:
            os.makedirs(STATE_DIR, exist_ok=True)
            with open(SIGNATURE_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f)
        except OSError as exc:
            log(f"Warning: failed to cache signature: {exc}")

    def _log_drift(self, prev: dict[str, Any] | None, fresh: dict[str, Any]) -> None:
        adapted, warnings = detect_signature_drift(prev, fresh)
        if adapted:
            log(f"Auto-adapted OpenCode signature: {'; '.join(adapted)}")
        for w in warnings:
            log(f"WARNING: OpenCode call format changed — {w}")

    def get(self) -> dict[str, Any]:
        return self.check_and_refresh(force=False)

    def check_and_refresh(self, force: bool = False) -> dict[str, Any]:
        with self._lock:
            now = time.time()
            current_ver = self._get_cli_version()
            prev = self._data or self._load_disk_cache()

            if not force and prev and self._is_cache_complete(prev):
                fresh_ttl = (now - float(prev.get("captured_at", 0))) < SIGNATURE_TTL_SEC
                same_ver = not current_ver or prev.get("cli_version") == current_ver
                if fresh_ttl and same_ver:
                    self._data = prev
                    return prev
                if current_ver and prev.get("cli_version") != current_ver:
                    log(
                        f"OpenCode CLI version changed ({prev.get('cli_version') or '?'} -> {current_ver}); re-checking call format..."
                    )

            fresh = self._capture(cli_version=current_ver)
            self._log_drift(prev, fresh)
            self._data = fresh
            self._save_disk_cache(fresh)
            return fresh

    def start_periodic_check(
        self, interval_sec: float = SIGNATURE_CHECK_INTERVAL_SEC
    ) -> None:
        if interval_sec <= 0:
            return
        if self._monitor_thread and self._monitor_thread.is_alive():
            return
        self._stop_evt.clear()

        def _loop() -> None:
            while not self._stop_evt.wait(interval_sec):
                try:
                    self.check_and_refresh(force=False)
                except Exception as exc:
                    log(f"WARNING: Periodic OpenCode format check failed: {exc}")

        self._monitor_thread = threading.Thread(
            target=_loop, name="opencode-format-monitor", daemon=True
        )
        self._monitor_thread.start()

    def stop_periodic_check(self) -> None:
        self._stop_evt.set()

    def _capture_models(self) -> list[str]:
        try:
            r = subprocess.run(
                [OPENCODE_BIN, "--standalone", "models"],
                capture_output=True,
                text=True,
                timeout=15,
            )
            models = []
            for line in r.stdout.splitlines():
                line = line.strip()
                if line.startswith("opencode/"):
                    models.append(line[len("opencode/") :])
            if models:
                return models
        except Exception as exc:
            log(f"Warning: `opencode --standalone models` failed: {exc}")
        return list(DEFAULT_MODELS)

    def _capture(self, cli_version: str = "") -> dict[str, Any]:
        log("Capturing canonical OpenCode request signature...")
        captured: list[dict[str, Any]] = []

        class CaptureHandler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length).decode("utf-8", errors="replace")
                captured.append(
                    {
                        "path": self.path,
                        "headers": dict(self.headers),
                        "body": raw,
                    }
                )
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"error":"capture_complete"}')

            def log_message(self, *args: Any) -> None:
                pass

        srv = HTTPServer(("127.0.0.1", 0), CaptureHandler)
        cap_port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            with tempfile.TemporaryDirectory() as td:
                env = os.environ.copy()
                for k in (
                    "XDG_CONFIG_HOME",
                    "XDG_DATA_HOME",
                    "XDG_STATE_HOME",
                    "XDG_CACHE_HOME",
                ):
                    p = os.path.join(td, k)
                    os.makedirs(p, exist_ok=True)
                    env[k] = p
                env["OPENCODE_CONFIG_CONTENT"] = json.dumps(
                    {
                        "mcp": {},
                        "plugin": [],
                        "provider": {
                            "opencode": {
                                "options": {"baseURL": f"http://127.0.0.1:{cap_port}/v1"}
                            }
                        },
                    }
                )
                subprocess.run(
                    [
                        OPENCODE_BIN,
                        "--standalone",
                        "run",
                        "--model",
                        "opencode/deepseek-v4.1-flash-free",
                        "Say OK",
                    ],
                    cwd=td,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
        finally:
            srv.shutdown()
            srv.server_close()

        req_entry = None
        for entry in captured:
            try:
                body_obj = json.loads(entry["body"])
                if isinstance(body_obj.get("tools"), list) and body_obj["tools"]:
                    req_entry = (entry["path"], entry["headers"], body_obj)
                    break
            except Exception:
                pass

        if not req_entry:
            raise RuntimeError(
                f"Failed to capture OpenCode signature (captured={len(captured)})"
            )

        req_path, hdrs, body_obj = req_entry
        ua = hdrs.get("User-Agent") or hdrs.get("user-agent") or "opencode/latest/2.2.7/cli"
        sys_msg = ""
        sys_role = "system"
        sys_type = "str"
        for m in body_obj.get("messages", []):
            if isinstance(m, dict) and m.get("role") in ("system", "developer"):
                sys_role = str(m.get("role"))
                raw_content = m.get("content", "")
                sys_type = type(raw_content).__name__
                sys_msg = raw_content if isinstance(raw_content, str) else json.dumps(raw_content)
                break

        tool_types = sorted(
            {
                str(t.get("type", ""))
                for t in body_obj["tools"]
                if isinstance(t, dict)
            }
        )
        models = self._capture_models()
        cli_ver = cli_version or self._get_cli_version()
        static_headers = extract_static_headers(hdrs)
        header_names = extract_header_names(hdrs)
        body_keys = sorted(body_obj.keys())

        log(
            f"Captured OpenCode signature (ver={cli_ver or '?'}, ua={ua}, "
            f"sys_len={len(sys_msg)}, base_tools={len(body_obj['tools'])}, models={len(models)})"
        )
        return {
            "captured_at": time.time(),
            "cli_version": cli_ver,
            "user_agent": ua,
            "request_path": req_path,
            "header_names": header_names,
            "static_headers": static_headers,
            "body_keys": body_keys,
            "system_role": sys_role,
            "system_content_type": sys_type,
            "tool_types": tool_types,
            "system_prompt": sys_msg,
            "base_tools": body_obj["tools"],
            "models": models,
        }


SIGNATURE = SignatureManager()
