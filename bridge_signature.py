"""Tracks, monitors, and auto-adapts OpenCode's canonical request signature.

Default behavior (BRIDGE_SIGNATURE_MODE=auto) tracks upstream releases and
source files on GitHub (anomalyco/opencode) with a bundled v2.4.1 fallback
snapshot — requiring zero local `opencode` CLI binary. Setting
BRIDGE_SIGNATURE_MODE=cli opts back into local `opencode --standalone` capture.
"""

from __future__ import annotations

import copy
import json
import os
import re
import subprocess
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

from bridge_config import (
    DEFAULT_MODELS,
    OPENCODE_BIN,
    OPENCODE_GITHUB_REPO,
    OPENCODE_UPDATE_URL,
    SIGNATURE_CHECK_INTERVAL_SEC,
    SIGNATURE_FILE,
    SIGNATURE_MODE,
    SIGNATURE_TTL_SEC,
    STATE_DIR,
    log,
)
from bridge_defaults import (
    DEFAULT_BASE_TOOLS,
    DEFAULT_BODY_KEYS,
    DEFAULT_HEADER_NAMES,
    DEFAULT_REQUEST_PATH,
    DEFAULT_STATIC_HEADERS,
    DEFAULT_SYSTEM_PROMPT_SUFFIX,
    build_default_signature,
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
_EXPECTED_HEADERS = frozenset(DEFAULT_HEADER_NAMES)
_EXPECTED_BODY_KEYS = frozenset(DEFAULT_BODY_KEYS)
_CORE_MODEL_REQUEST_HEADERS = frozenset(
    {
        "user-agent",
        "x-opencode-client",
        "x-opencode-project",
        "x-opencode-session",
        "x-opencode-session-id",
        "x-session-affinity",
        "x-session-id",
    }
)
_OPTIONAL_CONDITIONAL_HEADERS = frozenset(
    {
        "x-opencode-parent-session-id",
        "x-parent-session-id",
    }
)
_DEFAULT_TOOL_GUIDANCE: dict[str, list[str]] = {
    "shell": [
        "- Prefer dedicated tools over shell commands; fall back to the shell when a tool cannot do what you need.",
        "- Do not chain shell commands with separators like `echo \"====\";` or `printf '---'`; the output becomes noisy in a way that makes the user's side of the conversation worse.",
    ],
    "write": [
        "- Use the write tool to create files or completely replace their content. Prefer using the edit tool for targeted changes.",
    ],
    "edit": [
        "- Use the edit tool for targeted changes to existing text files. It replaces the exact text in `oldString` with `newString`, and the values must differ. By default, `oldString` must occur exactly once. If it occurs multiple times, include more surrounding context to make it unique or set `replaceAll` to true to replace every occurrence.",
    ],
}


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


def _unescape_js_string(raw: str, quote: str) -> str:
    return (
        raw.replace(f"\\{quote}", quote)
        .replace("\\n", "\n")
        .replace("\\t", "\t")
        .replace("\\\\", "\\")
    )


def render_system_prompt_from_sources(
    system_txt: str, system_prompt_ts: str = ""
) -> tuple[str, list[str]]:
    """Render OpenCode's system.txt with tool guidance extracted from system-prompt.ts."""
    warnings: list[str] = []
    if "${OPENCODE_TOOL_GUIDANCE}" not in system_txt:
        warnings.append("system.txt missing ${OPENCODE_TOOL_GUIDANCE} placeholder")
        return system_txt, warnings

    guidance_lines: list[str] = []
    for tool_name in ("shell", "write", "edit"):
        block_match = re.search(
            rf'if\s*\(\s*tools\.includes\(\s*["\']{re.escape(tool_name)}["\']\s*\)\s*\)\s*\{{(.*?)\}}',
            system_prompt_ts,
            re.DOTALL,
        )
        parsed_lines: list[str] = []
        if block_match:
            block = block_match.group(1)
            for m in re.finditer(r'(["\'])((?:\\.|(?!\1).)*)\1', block):
                parsed_lines.append(_unescape_js_string(m.group(2), m.group(1)))
        if parsed_lines:
            guidance_lines.extend(parsed_lines)
        else:
            guidance_lines.extend(_DEFAULT_TOOL_GUIDANCE[tool_name])
            if system_prompt_ts:
                warnings.append(f"could not parse {tool_name} guidance in system-prompt.ts")

    rendered = system_txt.replace("${OPENCODE_TOOL_GUIDANCE}", "\n".join(guidance_lines))
    return rendered, warnings


def extract_model_request_headers(model_request_ts: str) -> set[str]:
    """Extract static/dynamic header keys from packages/core/src/session/model-request.ts."""
    m = re.search(
        r"ai\.request\(\s*\{.*?headers\s*:\s*\{(.*?)\n\s*\}",
        model_request_ts,
        re.DOTALL,
    )
    if not m:
        return set()
    block = m.group(1)
    found: set[str] = set()
    for km in re.finditer(r'["\']([A-Za-z0-9-]+)["\']\s*:', block):
        found.add(km.group(1).lower())
    return found


def detect_signature_drift(
    prev: dict[str, Any] | None, fresh: dict[str, Any]
) -> tuple[list[str], list[str]]:
    """Compare a fresh capture against previous/expected format.

    Returns (auto_adapted_notices, call_format_warnings).
    """
    adapted: list[str] = []
    warnings: list[str] = list(fresh.get("source_warnings") or [])

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
    """Tracks, monitors, and caches OpenCode's canonical call signature."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: dict[str, Any] | None = None
        self._stop_evt = threading.Event()
        self._monitor_thread: threading.Thread | None = None

    @staticmethod
    def _has_cli_binary() -> bool:
        return bool(OPENCODE_BIN) and os.path.isfile(OPENCODE_BIN) and os.access(OPENCODE_BIN, os.X_OK)

    def _get_cli_version(self) -> str:
        if not self._has_cli_binary():
            return ""
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
    def _http_get_text(url: str, timeout: float = 10.0) -> str:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "opencode-openai-bridge"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")

    def _fetch_latest_v2_release(self) -> tuple[str, str]:
        """Return (channel, version) without consuming GitHub REST API rate limits."""
        if OPENCODE_UPDATE_URL:
            try:
                raw = self._http_get_text(OPENCODE_UPDATE_URL, timeout=8.0)
                obj = json.loads(raw)
                ver = str(obj.get("version") or "").lstrip("v").strip()
                chan = str(obj.get("channel") or "latest").strip() or "latest"
                if re.match(r"^\d+\.\d+\.\d+", ver):
                    return chan, ver
            except Exception:
                pass

        releases_url = f"https://github.com/{OPENCODE_GITHUB_REPO}/releases"
        html = self._http_get_text(releases_url, timeout=10.0)
        escaped_repo = re.escape(OPENCODE_GITHUB_REPO)
        matches = re.findall(
            rf"/{escaped_repo}/releases/tag/v(2\.\d+\.\d+)", html
        )
        if matches:
            return "latest", matches[0]
        any_matches = re.findall(
            rf"/{escaped_repo}/releases/tag/v(\d+\.\d+\.\d+)", html
        )
        if any_matches:
            return "latest", any_matches[0]
        raise RuntimeError(f"Could not determine latest version from {releases_url}")

    def _capture_github(self, prev: dict[str, Any] | None = None) -> dict[str, Any]:
        channel, version = self._fetch_latest_v2_release()
        tag = f"v{version}"
        raw_base = f"https://raw.githubusercontent.com/{OPENCODE_GITHUB_REPO}/{tag}"

        system_txt = self._http_get_text(
            f"{raw_base}/packages/core/src/session/runner/prompt/system.txt"
        )
        system_prompt_ts = ""
        try:
            system_prompt_ts = self._http_get_text(
                f"{raw_base}/packages/core/src/session/system-prompt.ts"
            )
        except Exception:
            pass

        core_sys_msg, parse_warnings = render_system_prompt_from_sources(
            system_txt, system_prompt_ts
        )
        sys_msg = core_sys_msg.rstrip() + "\n\n" + DEFAULT_SYSTEM_PROMPT_SUFFIX

        # Inspect model-request.ts for any unexpected header drift.
        header_names = list(DEFAULT_HEADER_NAMES)
        try:
            mr_ts = self._http_get_text(
                f"{raw_base}/packages/core/src/session/model-request.ts"
            )
            parsed_headers = extract_model_request_headers(mr_ts)
            if parsed_headers:
                unexpected = parsed_headers - _CORE_MODEL_REQUEST_HEADERS - _OPTIONAL_CONDITIONAL_HEADERS
                missing = _CORE_MODEL_REQUEST_HEADERS - parsed_headers
                if unexpected or missing:
                    non_req_base = set(DEFAULT_HEADER_NAMES) - _CORE_MODEL_REQUEST_HEADERS
                    header_names = sorted(
                        non_req_base | (parsed_headers - _OPTIONAL_CONDITIONAL_HEADERS)
                    )
        except Exception:
            pass

        base_tools = (
            copy.deepcopy(prev["base_tools"])
            if prev and isinstance(prev.get("base_tools"), list) and prev["base_tools"]
            else copy.deepcopy(DEFAULT_BASE_TOOLS)
        )
        static_headers = (
            dict(prev["static_headers"])
            if prev and isinstance(prev.get("static_headers"), dict) and prev["static_headers"]
            else dict(DEFAULT_STATIC_HEADERS)
        )
        models = (
            list(prev["models"])
            if prev and isinstance(prev.get("models"), list) and prev["models"]
            else list(DEFAULT_MODELS)
        )

        ua = f"opencode/{channel}/{version}/cli"
        cli_ver = f"opencode {tag}"
        log(
            f"Synced OpenCode signature from GitHub {OPENCODE_GITHUB_REPO}@{tag} "
            f"(ua={ua}, sys_len={len(sys_msg)})"
        )
        out: dict[str, Any] = {
            "captured_at": time.time(),
            "source": f"github:{OPENCODE_GITHUB_REPO}@{tag}",
            "cli_version": cli_ver,
            "user_agent": ua,
            "request_path": DEFAULT_REQUEST_PATH,
            "header_names": header_names,
            "static_headers": static_headers,
            "body_keys": list(DEFAULT_BODY_KEYS),
            "system_role": "system",
            "system_content_type": "str",
            "tool_types": ["function"],
            "system_prompt": sys_msg,
            "base_tools": base_tools,
            "models": models,
        }
        if parse_warnings:
            out["source_warnings"] = parse_warnings
        return out

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
            to_write = {k: v for k, v in data.items() if k != "source_warnings"}
            with open(SIGNATURE_FILE, "w", encoding="utf-8") as f:
                json.dump(to_write, f)
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

    def _effective_mode(self) -> str:
        if SIGNATURE_MODE in ("cli", "github", "static"):
            return SIGNATURE_MODE
        return "github"

    def check_and_refresh(self, force: bool = False) -> dict[str, Any]:
        with self._lock:
            now = time.time()
            mode = self._effective_mode()
            prev = self._data or self._load_disk_cache()

            if mode == "static":
                if prev and self._is_cache_complete(prev):
                    self._data = prev
                    return prev
                fallback = build_default_signature()
                self._data = fallback
                self._save_disk_cache(fallback)
                return fallback

            if mode == "cli":
                current_ver = self._get_cli_version()
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

            # Default: GitHub tracker + bundled fallback (zero opencode CLI dependency).
            if not force and prev and self._is_cache_complete(prev):
                fresh_ttl = (now - float(prev.get("captured_at", 0))) < SIGNATURE_TTL_SEC
                if fresh_ttl:
                    self._data = prev
                    return prev

            try:
                fresh = self._capture_github(prev=prev)
                self._log_drift(prev, fresh)
                clean = {k: v for k, v in fresh.items() if k != "source_warnings"}
                self._data = clean
                self._save_disk_cache(clean)
                return clean
            except Exception as exc:
                log(f"Warning: GitHub OpenCode tracker failed ({exc}); using cached/bundled signature")
                if prev and self._is_cache_complete(prev):
                    self._data = prev
                    return prev
                fallback = build_default_signature()
                self._data = fallback
                self._save_disk_cache(fallback)
                return fallback

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
        if not self._has_cli_binary():
            return list(DEFAULT_MODELS)
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
        if not self._has_cli_binary():
            raise RuntimeError(f"opencode CLI binary not found at {OPENCODE_BIN}")
        log("Capturing canonical OpenCode request signature via local CLI...")
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
        ua = hdrs.get("User-Agent") or hdrs.get("user-agent") or "opencode/latest/2.4.1/cli"
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
            "source": "cli",
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
