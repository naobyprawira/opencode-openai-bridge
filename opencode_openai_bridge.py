#!/usr/bin/env python3
"""OpenAI-compatible HTTP bridge to OpenCode's free inference API.

Exposes:
  - GET  /health
  - GET  /v1/models           (alias: /models)
  - POST /v1/chat/completions (alias: /chat/completions)
"""

from __future__ import annotations

import json
import os
import queue
import secrets
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from urllib.parse import parse_qs

from bridge_config import (
    API_KEY_FILE,
    BRIDGE_HOST,
    BRIDGE_PORT,
    UPSTREAM_PORT,
    UPSTREAM_PROXY_URL,
    UPSTREAM_ROTATE_URL,
    load_or_create_api_key,
    log,
)
from bridge_protocol import (
    assemble_non_stream_response,
    is_rate_limit_error,
    normalize_model_id,
    rewrite_sse_line,
)
from bridge_signature import SIGNATURE
from bridge_upstream import MODELS_CATALOG, UPSTREAM, open_inference_with_retry


API_KEY = load_or_create_api_key()


class BridgeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        log(f"{self.address_string()} - {format % args}")

    def _send_json(self, code: int, payload: Any) -> None:
        self.close_connection = True
        raw = json.dumps(payload).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(raw)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_openai_error(
        self, code: int, message: str, err_type: str = "api_error"
    ) -> None:
        self._send_json(
            code,
            {"error": {"message": message, "type": err_type, "code": str(code)}},
        )

    def _authorized(self) -> bool:
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            if secrets.compare_digest(auth[7:].strip(), API_KEY):
                return True
        xkey = self.headers.get("X-API-Key", "").strip()
        if xkey and secrets.compare_digest(xkey, API_KEY):
            return True
        return False

    def do_OPTIONS(self) -> None:
        self.close_connection = True
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header(
            "Access-Control-Allow-Headers", "Authorization, Content-Type, X-API-Key"
        )
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()

    def do_GET(self) -> None:
        path, _, query = self.path.partition("?")
        if path == "/health":
            sig = SIGNATURE.get()
            self._send_json(
                200,
                {
                    "status": "ok",
                    "bridge_port": BRIDGE_PORT,
                    "upstream_port": UPSTREAM_PORT,
                    "mode": "bun-inference-passthrough",
                    "signature_source": sig.get("source", "unknown"),
                    "proxy_configured": bool(UPSTREAM_PROXY_URL),
                    "rotate_webhook_configured": bool(UPSTREAM_ROTATE_URL),
                },
            )
            return

        if path in ("/v1/models", "/models"):
            if not self._authorized():
                self._send_openai_error(401, "Invalid API key", "authentication_error")
                return
            qs = parse_qs(query)
            force = qs.get("refresh", [""])[0].lower() in ("1", "true", "yes")
            items = MODELS_CATALOG.get(force=force)
            self._send_json(200, {"object": "list", "data": items})
            return

        self._send_openai_error(404, f"Unknown endpoint: {path}", "invalid_request_error")

    def do_POST(self) -> None:
        self.close_connection = True
        path = self.path.split("?", 1)[0]
        if path not in ("/v1/chat/completions", "/chat/completions"):
            self._send_openai_error(404, f"Unknown endpoint: {path}", "invalid_request_error")
            return

        if not self._authorized():
            self._send_openai_error(401, "Invalid API key", "authentication_error")
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send_openai_error(400, "Invalid Content-Length", "invalid_request_error")
            return

        raw_body = self.rfile.read(length) if length > 0 else b"{}"
        try:
            req = json.loads(raw_body.decode("utf-8"))
        except Exception as e:
            self._send_openai_error(400, f"Invalid JSON: {e}", "invalid_request_error")
            return

        messages = req.get("messages")
        if not isinstance(messages, list) or not messages:
            self._send_openai_error(
                400, "'messages' must be a non-empty list", "invalid_request_error"
            )
            return

        requested_model = str(req.get("model") or "big-pickle")
        model_id = normalize_model_id(requested_model)
        stream = bool(req.get("stream", False))

        st, conn, resp, err_body = open_inference_with_retry(req, model_id)
        if st != 200 or conn is None or resp is None:
            err_obj = (
                err_body.get("error", err_body)
                if isinstance(err_body, dict)
                else {"message": str(err_body)}
            )
            msg = (
                err_obj.get("message", str(err_body))
                if isinstance(err_obj, dict)
                else str(err_body)
            )
            if is_rate_limit_error(st, err_body):
                self._send_openai_error(429, msg, "rate_limit_error")
            elif st == 504:
                self._send_openai_error(504, msg, "timeout_error")
            else:
                self._send_openai_error(st if 400 <= st < 600 else 502, msg)
            return

        try:
            if not stream:
                completion = assemble_non_stream_response(resp, requested_model)
                self._send_json(200, completion)
                return

            # Streaming mode: forward live SSE lines and emit keepalive if idle >8s
            line_q: queue.Queue[bytes | None] = queue.Queue()

            def pump() -> None:
                try:
                    while True:
                        raw_line = resp.readline()
                        if not raw_line:
                            break
                        line_q.put(raw_line)
                except Exception:
                    pass
                finally:
                    line_q.put(None)

            threading.Thread(target=pump, daemon=True).start()

            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Connection", "close")
            self.end_headers()

            while True:
                try:
                    item = line_q.get(timeout=8.0)
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    continue

                if item is None:
                    break
                text_line = item.decode("utf-8", errors="replace").rstrip("\r\n")
                if not text_line:
                    self.wfile.write(b"\n")
                    self.wfile.flush()
                    continue
                rewritten = rewrite_sse_line(text_line, requested_model)
                self.wfile.write((rewritten + "\n").encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass


class ReusableThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def main() -> None:
    sig = SIGNATURE.get()
    SIGNATURE.start_periodic_check()
    UPSTREAM.ensure_running()
    server = ReusableThreadingHTTPServer((BRIDGE_HOST, BRIDGE_PORT), BridgeHandler)

    def handle_sig(signum: int, _frame: Any) -> None:
        log(f"Received signal {signum}, shutting down...")
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, handle_sig)
    signal.signal(signal.SIGINT, handle_sig)

    auth_source = (
        "BRIDGE_API_KEY env"
        if os.environ.get("BRIDGE_API_KEY", "").strip()
        else API_KEY_FILE
    )
    log(
        f"OpenCode OpenAI bridge listening on http://{BRIDGE_HOST}:{BRIDGE_PORT}/v1 "
        f"(auth: {auth_source}, sig: {sig.get('source', 'unknown')})"
    )
    try:
        server.serve_forever()
    finally:
        SIGNATURE.stop_periodic_check()
        UPSTREAM.stop()
        server.server_close()


if __name__ == "__main__":
    main()
