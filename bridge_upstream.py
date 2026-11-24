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

from bridge_config import (
    BUN_BIN,
    BUN_UPSTREAM_JS,
    INFERENCE_URL,
    REQUEST_TIMEOUT_SEC,
    STATE_DIR,
    UPSTREAM_HOST,
    UPSTREAM_PORT,
    UPSTREAM_PROXY_URL,
    log,
)
from bridge_protocol import (
    build_upstream_payload,
    make_opencode_headers,
)
from bridge_signature import SIGNATURE


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


UPSTREAM = UpstreamManager()


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

    for attempt in range(3):
        headers = make_opencode_headers(sig["user_agent"])
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
            if attempt < 2:
                time.sleep(0.5)
                continue
            return last_st, None, None, last_body

    return last_st, None, None, last_body
