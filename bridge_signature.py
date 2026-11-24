"""Captures and caches OpenCode's canonical request signature and model list."""

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
    SIGNATURE_FILE,
    SIGNATURE_TTL_SEC,
    STATE_DIR,
    log,
)


class SignatureManager:
    """Captures and caches OpenCode's canonical system prompt, tools, UA, and models."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: dict[str, Any] | None = None

    def get(self) -> dict[str, Any]:
        with self._lock:
            now = time.time()
            if (
                self._data
                and (now - float(self._data.get("captured_at", 0))) < SIGNATURE_TTL_SEC
            ):
                return self._data

            if os.path.exists(SIGNATURE_FILE):
                try:
                    with open(SIGNATURE_FILE, "r", encoding="utf-8") as f:
                        cached = json.load(f)
                    if (
                        isinstance(cached, dict)
                        and cached.get("system_prompt")
                        and isinstance(cached.get("base_tools"), list)
                        and cached["base_tools"]
                        and (now - float(cached.get("captured_at", 0))) < SIGNATURE_TTL_SEC
                    ):
                        self._data = cached
                        return cached
                except Exception:
                    pass

            fresh = self._capture()
            self._data = fresh
            try:
                os.makedirs(STATE_DIR, exist_ok=True)
                with open(SIGNATURE_FILE, "w", encoding="utf-8") as f:
                    json.dump(fresh, f)
            except OSError as exc:
                log(f"Warning: failed to cache signature: {exc}")
            return fresh

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

    def _capture(self) -> dict[str, Any]:
        log("Capturing canonical OpenCode request signature...")
        captured: list[dict[str, Any]] = []

        class CaptureHandler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length).decode("utf-8", errors="replace")
                captured.append({"headers": dict(self.headers), "body": raw})
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
                    req_entry = (entry["headers"], body_obj)
                    break
            except Exception:
                pass

        if not req_entry:
            raise RuntimeError(
                f"Failed to capture OpenCode signature (captured={len(captured)})"
            )

        hdrs, body_obj = req_entry
        ua = hdrs.get("User-Agent") or hdrs.get("user-agent") or "opencode/latest/2.2.7/cli"
        sys_msg = ""
        for m in body_obj.get("messages", []):
            if isinstance(m, dict) and m.get("role") == "system":
                sys_msg = str(m.get("content", ""))
                break

        models = self._capture_models()
        log(
            f"Captured OpenCode signature (ua={ua}, sys_len={len(sys_msg)}, "
            f"base_tools={len(body_obj['tools'])}, models={len(models)})"
        )
        return {
            "captured_at": time.time(),
            "user_agent": ua,
            "system_prompt": sys_msg,
            "base_tools": body_obj["tools"],
            "models": models,
        }


SIGNATURE = SignatureManager()
