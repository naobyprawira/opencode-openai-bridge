"""Process supervision for the local Bun HTTP upstream forwarder."""

from __future__ import annotations

import http.client
import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from typing import Any

from opencode_bridge.config import (
    BUN_BIN,
    BUN_UPSTREAM_JS,
    DEFAULT_MODELS,
    INFERENCE_URL,
    MODELS_TTL_SEC,
    MODELS_URL,
    RATE_LIMIT_RETRIES,
    REQUEST_TIMEOUT_SEC,
    RETRY_BUDGET_SEC,
    STATE_DIR,
    UPSTREAM_HOST,
    UPSTREAM_PORT,
    UPSTREAM_PROXY_URL,
    UPSTREAM_RETRY_COUNT,
    UPSTREAM_ROTATE_TOKEN,
    UPSTREAM_ROTATE_URL,
    log,
)
from opencode_bridge.protocol import (
    build_upstream_payload,
    extract_free_chat_models,
    is_rate_limit_error,
    make_opencode_headers,
)
from opencode_bridge.signature import SIGNATURE


class UpstreamManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._bun_proc: subprocess.Popen[bytes] | None = None

    @staticmethod
    def _terminate_proc(proc: subprocess.Popen[bytes] | None) -> None:
        if proc is None:
            return
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except Exception:
            pass
        try:
            proc.wait(timeout=4)
        except Exception:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except Exception:
                pass

    def _is_bun_healthy_unlocked(self) -> bool:
        if self._bun_proc is None or self._bun_proc.poll() is not None:
            return False
        try:
            req = urllib.request.Request(
                f"http://{UPSTREAM_HOST}:{UPSTREAM_PORT}/health"
            )
            with urllib.request.urlopen(req, timeout=2) as r:
                return r.status == 200
        except Exception:
            return False

    def _start_bun_unlocked(self) -> None:
        if not os.path.isfile(BUN_BIN):
            raise RuntimeError(f"bun binary not found at {BUN_BIN}")
        if not os.path.isfile(BUN_UPSTREAM_JS):
            raise RuntimeError(f"bun_upstream.js not found at {BUN_UPSTREAM_JS}")

        os.makedirs(STATE_DIR, exist_ok=True)
        env = os.environ.copy()
        env["BUN_UPSTREAM_HOST"] = UPSTREAM_HOST
        env["BUN_UPSTREAM_PORT"] = str(UPSTREAM_PORT)
        env["BRIDGE_REQUEST_TIMEOUT"] = str(int(REQUEST_TIMEOUT_SEC))

        log(f"Starting Bun upstream forwarder on {UPSTREAM_HOST}:{UPSTREAM_PORT}")
        self._bun_proc = subprocess.Popen(
            [BUN_BIN, "run", BUN_UPSTREAM_JS],
            cwd=STATE_DIR,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=sys.stderr,
            start_new_session=True,
        )
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if self._bun_proc.poll() is not None:
                raise RuntimeError(
                    f"bun_upstream.js exited prematurely with code {self._bun_proc.returncode}"
                )
            if self._is_bun_healthy_unlocked():
                log("Bun upstream forwarder is ready")
                return
            time.sleep(0.1)
        raise RuntimeError("Timed out waiting for bun_upstream.js readiness")

    def ensure_running(self) -> None:
        with self._lock:
            if not self._is_bun_healthy_unlocked():
                self._terminate_proc(self._bun_proc)
                self._bun_proc = None
                self._start_bun_unlocked()

    def stop(self) -> None:
        with self._lock:
            self._terminate_proc(self._bun_proc)
            self._bun_proc = None

    def open_upstream_stream(
        self,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout: float = REQUEST_TIMEOUT_SEC,
        proxy_url: str = UPSTREAM_PROXY_URL,
    ) -> tuple[http.client.HTTPConnection, http.client.HTTPResponse]:
        self.ensure_running()
        spec: dict[str, Any] = {
            "url": INFERENCE_URL,
            "method": "POST",
            "headers": headers,
            "body": payload,
            "timeoutMs": int(timeout * 1000),
        }
        if proxy_url:
            spec["proxyUrl"] = proxy_url
        body = json.dumps(spec).encode("utf-8")
        conn = http.client.HTTPConnection(UPSTREAM_HOST, UPSTREAM_PORT, timeout=timeout)
        conn.request(
            "POST",
            "/forward",
            body=body,
            headers={"Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        return conn, resp

    def fetch_json(
        self,
        url: str,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        timeout: float = 15.0,
        proxy_url: str = UPSTREAM_PROXY_URL,
    ) -> tuple[int, Any]:
        self.ensure_running()
        spec: dict[str, Any] = {
            "url": url,
            "method": method,
            "headers": headers or {},
            "timeoutMs": int(timeout * 1000),
        }
        if proxy_url:
            spec["proxyUrl"] = proxy_url
        body = json.dumps(spec).encode("utf-8")
        conn = http.client.HTTPConnection(UPSTREAM_HOST, UPSTREAM_PORT, timeout=timeout)
        try:
            conn.request(
                "POST",
                "/forward",
                body=body,
                headers={"Content-Type": "application/json"},
            )
            resp = conn.getresponse()
            raw = resp.read().decode("utf-8", errors="replace")
            return resp.status, json.loads(raw) if raw else {}
        finally:
            conn.close()


UPSTREAM = UpstreamManager()


class ModelCatalogManager:
    """Syncs active free chat models from OpenCode's live /v1/models endpoint."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: list[dict[str, Any]] = []
        self._fetched_at: float = 0.0

    def _fallback_items(self) -> list[dict[str, Any]]:
        try:
            sig = SIGNATURE.get()
            models = sig.get("models") or DEFAULT_MODELS
        except Exception:
            models = DEFAULT_MODELS
        now = int(time.time())
        return [
            {
                "id": mid,
                "object": "model",
                "created": now,
                "owned_by": "opencode",
            }
            for mid in models
        ]

    def get(self, force: bool = False) -> list[dict[str, Any]]:
        with self._lock:
            now = time.monotonic()
            if (
                not force
                and self._items
                and (now - self._fetched_at) < MODELS_TTL_SEC
            ):
                return list(self._items)

            try:
                ua = "opencode/latest"
                try:
                    ua = SIGNATURE.get().get("user_agent") or ua
                except Exception:
                    pass
                st, raw_payload = UPSTREAM.fetch_json(
                    MODELS_URL,
                    method="GET",
                    headers={
                        "Authorization": "Bearer public",
                        "Accept": "application/json",
                        "User-Agent": ua,
                    },
                    timeout=15.0,
                )
                if st == 200 and isinstance(raw_payload, dict):
                    items = extract_free_chat_models(raw_payload)
                    if items:
                        old_ids = [m["id"] for m in self._items]
                        new_ids = [m["id"] for m in items]
                        if new_ids != old_ids:
                            log(f"Synced {len(items)} live free models from OpenCode")
                        self._items = items
                        self._fetched_at = time.monotonic()
                        return list(self._items)
            except Exception as exc:
                log(f"Warning: failed to sync live models catalog: {exc}")

            if self._items:
                return list(self._items)
            return self._fallback_items()


MODELS_CATALOG = ModelCatalogManager()


_ROTATE_LOCK = threading.Lock()
_ROTATE_GEN = 0


def _trigger_rotate_webhook(force: bool = False) -> bool:
    if not UPSTREAM_ROTATE_URL:
        return False
    url = UPSTREAM_ROTATE_URL
    if force:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}force=1"
    headers = {"Content-Type": "application/json"}
    if UPSTREAM_ROTATE_TOKEN:
        headers["Authorization"] = f"Bearer {UPSTREAM_ROTATE_TOKEN}"
    req = urllib.request.Request(url, data=b"{}", headers=headers, method="POST")
    try:
        log(f"Rate limit hit; calling upstream rotate webhook (force={force})...")
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read().decode("utf-8", errors="replace").strip()
            if not (200 <= r.status < 300):
                return False
            if raw:
                try:
                    data = json.loads(raw)
                except Exception:
                    return True
                if isinstance(data, dict):
                    if not force and (data.get("cooldown") or data.get("rotated") is False):
                        log("Rotate webhook returned cooldown/unchanged IP; escalating with force=1...")
                        return _trigger_rotate_webhook(force=True)
                    return bool(data.get("ok", True))
            return True
    except Exception as exc:
        log(f"Warning: upstream rotate webhook failed: {exc}")
        return False


def _rotate_for_generation(expected_gen: int) -> bool:
    global _ROTATE_GEN
    with _ROTATE_LOCK:
        if _ROTATE_GEN != expected_gen:
            return True
        if _trigger_rotate_webhook(force=False):
            _ROTATE_GEN += 1
            return True
        return False


def open_inference_with_retry(
    req: dict[str, Any], model_id: str
) -> tuple[
    int,
    http.client.HTTPConnection | None,
    http.client.HTTPResponse | None,
    Any,
]:
    sig = SIGNATURE.get()
    payload = build_upstream_payload(req, model_id, sig)
    last_st = 502
    last_body: Any = {"error": {"message": "Upstream connection failed"}}
    rotations_used = 0
    transient_used = 0
    deadline = time.monotonic() + RETRY_BUDGET_SEC

    while True:
        with _ROTATE_LOCK:
            gen_before = _ROTATE_GEN

        headers = make_opencode_headers(
            sig["user_agent"], sig.get("static_headers")
        )
        conn: http.client.HTTPConnection | None = None
        try:
            conn, resp = UPSTREAM.open_upstream_stream(
                headers, payload, timeout=REQUEST_TIMEOUT_SEC
            )
            if resp.status == 200:
                return 200, conn, resp, None

            raw = resp.read().decode("utf-8", errors="replace")
            st = resp.status
            conn.close()
            try:
                body = json.loads(raw) if raw else {"error": {"message": f"HTTP {st}"}}
            except Exception:
                body = {"error": {"message": raw or f"HTTP {st}"}}
            last_st, last_body = st, body

            if time.monotonic() >= deadline:
                return st, None, None, body

            if (
                is_rate_limit_error(st, body)
                and UPSTREAM_ROTATE_URL
                and rotations_used < RATE_LIMIT_RETRIES
                and _rotate_for_generation(gen_before)
            ):
                rotations_used += 1
                continue

            if st in (502, 503, 504):
                if _ROTATE_GEN != gen_before:
                    log(f"Upstream HTTP {st} during concurrent rotation; retrying on fresh IP...")
                    continue
                if transient_used < UPSTREAM_RETRY_COUNT:
                    transient_used += 1
                    log(f"Transient upstream HTTP {st}; retrying ({transient_used}/{UPSTREAM_RETRY_COUNT})...")
                    time.sleep(0.5)
                    continue

            return st, None, None, body
        except TimeoutError:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
            return 504, None, None, {
                "error": {
                    "message": f"Upstream timed out after {int(REQUEST_TIMEOUT_SEC)}s",
                    "type": "timeout_error",
                }
            }
        except Exception as exc:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
            last_st = 502
            last_body = {"error": {"message": f"Bridge upstream error: {exc}"}}
            if time.monotonic() >= deadline:
                return last_st, None, None, last_body
            if _ROTATE_GEN != gen_before:
                continue
            if transient_used < UPSTREAM_RETRY_COUNT:
                transient_used += 1
                time.sleep(0.5)
                continue
            return last_st, None, None, last_body

    return last_st, None, None, last_body
