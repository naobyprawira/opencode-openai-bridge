"""Unit tests for upstream forwarding and retry behavior."""

from __future__ import annotations

import http.client
import json
import unittest
from unittest.mock import MagicMock, patch

from opencode_bridge import upstream


class TestUpstream(unittest.TestCase):
    def test_open_upstream_stream_omits_proxy_when_unset(self) -> None:
        mgr = upstream.UpstreamManager()
        mock_conn = MagicMock(spec=http.client.HTTPConnection)
        mock_resp = MagicMock(spec=http.client.HTTPResponse)
        mock_conn.getresponse.return_value = mock_resp

        with patch.object(mgr, "ensure_running"), patch(
            "http.client.HTTPConnection", return_value=mock_conn
        ):
            conn, resp = mgr.open_upstream_stream(
                {"Content-Type": "application/json"},
                {"model": "big-pickle"},
                timeout=30.0,
                proxy_url="",
            )

        self.assertIs(conn, mock_conn)
        self.assertIs(resp, mock_resp)
        sent = json.loads(mock_conn.request.call_args.kwargs["body"].decode("utf-8"))
        self.assertNotIn("proxyUrl", sent)

    def test_open_upstream_stream_includes_proxy_when_configured(self) -> None:
        mgr = upstream.UpstreamManager()
        mock_conn = MagicMock(spec=http.client.HTTPConnection)
        mock_resp = MagicMock(spec=http.client.HTTPResponse)
        mock_conn.getresponse.return_value = mock_resp

        with patch.object(mgr, "ensure_running"), patch(
            "http.client.HTTPConnection", return_value=mock_conn
        ):
            mgr.open_upstream_stream(
                {"Content-Type": "application/json"},
                {"model": "big-pickle"},
                timeout=30.0,
                proxy_url="http://127.0.0.1:8080",
            )

        sent = json.loads(mock_conn.request.call_args.kwargs["body"].decode("utf-8"))
        self.assertEqual(sent["proxyUrl"], "http://127.0.0.1:8080")

    def test_open_inference_returns_429_without_retrying(self) -> None:
        mock_conn = MagicMock(spec=http.client.HTTPConnection)
        mock_resp = MagicMock(spec=http.client.HTTPResponse)
        mock_resp.status = 429
        mock_resp.read.return_value = b'{"error":{"message":"Rate limit"}}'

        fake_sig = {
            "user_agent": "opencode/test",
            "system_prompt": "You are an AI agent running in OpenCode.",
            "base_tools": [],
        }
        with patch.object(upstream.SIGNATURE, "get", return_value=fake_sig), patch.object(
            upstream.UPSTREAM, "open_upstream_stream", return_value=(mock_conn, mock_resp)
        ) as mock_open:
            st, conn, resp, body = upstream.open_inference_with_retry(
                {"messages": [{"role": "user", "content": "hi"}]},
                "deepseek-v4.1-flash-free",
            )

        self.assertEqual(mock_open.call_count, 1)
        self.assertEqual(st, 429)
        self.assertIsNone(conn)
        self.assertIsNone(resp)
        self.assertEqual(body["error"]["message"], "Rate limit")

    def test_open_inference_retries_transient_connection_error(self) -> None:
        mock_conn = MagicMock(spec=http.client.HTTPConnection)
        mock_resp = MagicMock(spec=http.client.HTTPResponse)
        mock_resp.status = 200

        fake_sig = {
            "user_agent": "opencode/test",
            "system_prompt": "You are an AI agent running in OpenCode.",
            "base_tools": [],
        }
        with patch.object(upstream.SIGNATURE, "get", return_value=fake_sig), patch(
            "time.sleep"
        ), patch.object(
            upstream.UPSTREAM,
            "open_upstream_stream",
            side_effect=[ConnectionResetError("reset"), (mock_conn, mock_resp)],
        ) as mock_open:
            st, conn, resp, body = upstream.open_inference_with_retry(
                {"messages": [{"role": "user", "content": "hi"}]},
                "deepseek-v4.1-flash-free",
            )

        self.assertEqual(mock_open.call_count, 2)
        self.assertEqual(st, 200)
        self.assertIs(conn, mock_conn)
        self.assertIs(resp, mock_resp)
        self.assertIsNone(body)

    def test_model_catalog_caches_and_refreshes_on_force(self) -> None:
        mgr = upstream.ModelCatalogManager()
        payload = {
            "data": [
                {
                    "id": "new-free-model",
                    "type": "chat",
                    "cost": {"text": {"input": {"price": 0}, "output": {"price": 0}}},
                }
            ]
        }
        with patch.object(upstream.SIGNATURE, "get", return_value={"user_agent": "opencode/test", "models": ["fallback"]}), patch.object(
            upstream.UPSTREAM, "fetch_json", return_value=(200, payload)
        ) as mock_fetch:
            first = mgr.get()
            second = mgr.get()
            forced = mgr.get(force=True)

        self.assertEqual([m["id"] for m in first], ["new-free-model"])
        self.assertEqual([m["id"] for m in second], ["new-free-model"])
        self.assertEqual([m["id"] for m in forced], ["new-free-model"])
        self.assertEqual(mock_fetch.call_count, 2)

    def test_model_catalog_falls_back_when_fetch_fails(self) -> None:
        mgr = upstream.ModelCatalogManager()
        with patch.object(upstream.SIGNATURE, "get", return_value={"user_agent": "opencode/test", "models": ["big-pickle"]}), patch.object(
            upstream.UPSTREAM, "fetch_json", side_effect=OSError("offline")
        ):
            models = mgr.get()

        self.assertEqual([m["id"] for m in models], ["big-pickle"])

    def test_open_inference_calls_rotate_webhook_on_429_and_retries(self) -> None:
        conn_429 = MagicMock(spec=http.client.HTTPConnection)
        resp_429 = MagicMock(spec=http.client.HTTPResponse)
        resp_429.status = 429
        resp_429.read.return_value = b'{"error":{"message":"Rate limit"}}'

        conn_ok = MagicMock(spec=http.client.HTTPConnection)
        resp_ok = MagicMock(spec=http.client.HTTPResponse)
        resp_ok.status = 200

        fake_sig = {
            "user_agent": "opencode/test",
            "system_prompt": "You are an AI agent running in OpenCode.",
            "base_tools": [],
        }
        with patch.object(upstream, "UPSTREAM_ROTATE_URL", "http://127.0.0.1:40001/_rotate"), patch.object(
            upstream.SIGNATURE, "get", return_value=fake_sig
        ), patch.object(
            upstream.UPSTREAM,
            "open_upstream_stream",
            side_effect=[(conn_429, resp_429), (conn_ok, resp_ok)],
        ) as mock_open, patch.object(
            upstream, "_trigger_rotate_webhook", return_value=True
        ) as mock_rot:
            st, conn, resp, body = upstream.open_inference_with_retry(
                {"messages": [{"role": "user", "content": "hi"}]},
                "deepseek-v4.1-flash-free",
            )

        mock_rot.assert_called_once_with(force=False)
        self.assertEqual(mock_open.call_count, 2)
        self.assertEqual(st, 200)
        self.assertIs(conn, conn_ok)
        self.assertIs(resp, resp_ok)
        self.assertIsNone(body)

    def test_trigger_rotate_webhook_escalates_on_cooldown(self) -> None:
        calls: list[str] = []

        class FakeResp:
            def __init__(self, status: int, body: bytes) -> None:
                self.status = status
                self._body = body

            def read(self) -> bytes:
                return self._body

            def __enter__(self) -> "FakeResp":
                return self

            def __exit__(self, *args: object) -> None:
                return None

        def fake_urlopen(req: object, timeout: float = 60) -> FakeResp:
            full_url = getattr(req, "full_url")
            calls.append(full_url)
            if "force=1" not in full_url:
                return FakeResp(200, b'{"ok":true,"rotated":false,"cooldown":true}')
            return FakeResp(200, b'{"ok":true,"rotated":true,"cooldown":false}')

        with patch.object(upstream, "UPSTREAM_ROTATE_URL", "http://127.0.0.1:40001/_rotate"), patch(
            "urllib.request.urlopen", side_effect=fake_urlopen
        ):
            ok = upstream._trigger_rotate_webhook(force=False)

        self.assertTrue(ok)
        self.assertEqual(
            calls,
            [
                "http://127.0.0.1:40001/_rotate",
                "http://127.0.0.1:40001/_rotate?force=1",
            ],
        )

    def test_open_inference_retries_multiple_rotations_and_transient_502(self) -> None:
        conn_429_1 = MagicMock(spec=http.client.HTTPConnection)
        resp_429_1 = MagicMock(spec=http.client.HTTPResponse)
        resp_429_1.status = 429
        resp_429_1.read.return_value = b'{"error":{"message":"Rate limit"}}'

        conn_502 = MagicMock(spec=http.client.HTTPConnection)
        resp_502 = MagicMock(spec=http.client.HTTPResponse)
        resp_502.status = 502
        resp_502.read.return_value = b'{"error":{"message":"ECONNRESET"}}'

        conn_429_2 = MagicMock(spec=http.client.HTTPConnection)
        resp_429_2 = MagicMock(spec=http.client.HTTPResponse)
        resp_429_2.status = 429
        resp_429_2.read.return_value = b'{"error":{"message":"Rate limit"}}'

        conn_ok = MagicMock(spec=http.client.HTTPConnection)
        resp_ok = MagicMock(spec=http.client.HTTPResponse)
        resp_ok.status = 200

        fake_sig = {
            "user_agent": "opencode/test",
            "system_prompt": "You are an AI agent running in OpenCode.",
            "base_tools": [],
        }
        with patch.object(upstream, "UPSTREAM_ROTATE_URL", "http://127.0.0.1:40001/_rotate"), patch.object(
            upstream.SIGNATURE, "get", return_value=fake_sig
        ), patch("time.sleep"), patch.object(
            upstream.UPSTREAM,
            "open_upstream_stream",
            side_effect=[
                (conn_429_1, resp_429_1),
                (conn_502, resp_502),
                (conn_429_2, resp_429_2),
                (conn_ok, resp_ok),
            ],
        ) as mock_open, patch.object(
            upstream, "_trigger_rotate_webhook", return_value=True
        ) as mock_rot:
            st, conn, resp, body = upstream.open_inference_with_retry(
                {"messages": [{"role": "user", "content": "hi"}]},
                "deepseek-v4.1-flash-free",
            )

        self.assertEqual(mock_rot.call_count, 2)
        self.assertEqual(mock_open.call_count, 4)
        self.assertEqual(st, 200)
        self.assertIs(conn, conn_ok)
        self.assertIs(resp, resp_ok)
        self.assertIsNone(body)

    def test_rotate_for_generation_deduplicates_concurrent_callers(self) -> None:
        upstream._ROTATE_GEN = 10
        with patch.object(upstream, "_trigger_rotate_webhook", return_value=True) as mock_rot:
            self.assertTrue(upstream._rotate_for_generation(10))
            self.assertEqual(upstream._ROTATE_GEN, 11)
            # Stale generation caller should skip redundant webhook call
            self.assertTrue(upstream._rotate_for_generation(10))
            self.assertEqual(mock_rot.call_count, 1)

    def test_open_inference_stops_at_retry_budget_deadline(self) -> None:
        conn_429 = MagicMock(spec=http.client.HTTPConnection)
        resp_429 = MagicMock(spec=http.client.HTTPResponse)
        resp_429.status = 429
        resp_429.read.return_value = b'{"error":{"message":"Rate limit"}}'

        fake_sig = {
            "user_agent": "opencode/test",
            "system_prompt": "You are an AI agent running in OpenCode.",
            "base_tools": [],
        }
        with patch.object(upstream, "RETRY_BUDGET_SEC", 0.0), patch.object(
            upstream, "UPSTREAM_ROTATE_URL", "http://127.0.0.1:40001/_rotate"
        ), patch.object(upstream.SIGNATURE, "get", return_value=fake_sig), patch.object(
            upstream.UPSTREAM,
            "open_upstream_stream",
            return_value=(conn_429, resp_429),
        ) as mock_open, patch.object(
            upstream, "_trigger_rotate_webhook", return_value=True
        ) as mock_rot:
            st, conn, resp, body = upstream.open_inference_with_retry(
                {"messages": [{"role": "user", "content": "hi"}]},
                "deepseek-v4.1-flash-free",
            )

        self.assertEqual(st, 429)
        self.assertIsNone(conn)
        self.assertIsNone(resp)
        self.assertEqual(mock_open.call_count, 1)
        self.assertEqual(mock_rot.call_count, 0)

    def test_open_inference_retries_502_on_concurrent_rotation_without_burning_transient(self) -> None:
        conn_502 = MagicMock(spec=http.client.HTTPConnection)
        resp_502 = MagicMock(spec=http.client.HTTPResponse)
        resp_502.status = 502
        resp_502.read.return_value = b'{"error":{"message":"ECONNRESET"}}'

        conn_ok = MagicMock(spec=http.client.HTTPConnection)
        resp_ok = MagicMock(spec=http.client.HTTPResponse)
        resp_ok.status = 200

        fake_sig = {
            "user_agent": "opencode/test",
            "system_prompt": "You are an AI agent running in OpenCode.",
            "base_tools": [],
        }

        def fake_open(*args: object, **kwargs: object) -> tuple[object, object]:
            if mock_open.call_count == 1:
                upstream._ROTATE_GEN += 1
                return conn_502, resp_502
            return conn_ok, resp_ok

        with patch.object(upstream, "UPSTREAM_RETRY_COUNT", 0), patch.object(
            upstream.SIGNATURE, "get", return_value=fake_sig
        ), patch.object(
            upstream.UPSTREAM, "open_upstream_stream", side_effect=fake_open
        ) as mock_open:
            st, conn, resp, body = upstream.open_inference_with_retry(
                {"messages": [{"role": "user", "content": "hi"}]},
                "deepseek-v4.1-flash-free",
            )

        self.assertEqual(st, 200)
        self.assertIs(conn, conn_ok)
        self.assertIs(resp, resp_ok)
        self.assertIsNone(body)
        self.assertEqual(mock_open.call_count, 2)

    def test_open_inference_waits_for_in_progress_rotate_lock_on_502(self) -> None:
        import threading

        conn_502 = MagicMock(spec=http.client.HTTPConnection)
        resp_502 = MagicMock(spec=http.client.HTTPResponse)
        resp_502.status = 502
        resp_502.read.return_value = b'{"error":{"message":"ECONNRESET"}}'

        conn_ok = MagicMock(spec=http.client.HTTPConnection)
        resp_ok = MagicMock(spec=http.client.HTTPResponse)
        resp_ok.status = 200

        fake_sig = {
            "user_agent": "opencode/test",
            "system_prompt": "You are an AI agent running in OpenCode.",
            "base_tools": [],
        }

        rot_started = threading.Event()
        release_rot = threading.Event()
        call_num = 0

        def fake_rotate() -> None:
            with upstream._ROTATE_LOCK:
                rot_started.set()
                release_rot.wait(timeout=2.0)
                upstream._ROTATE_GEN += 1

        def fake_open(*args: object, **kwargs: object) -> tuple[object, object]:
            nonlocal call_num
            call_num += 1
            if call_num == 1:
                t = threading.Thread(target=fake_rotate, daemon=True)
                t.start()
                rot_started.wait(timeout=2.0)
                # Release rotation shortly after 502 is returned
                threading.Timer(0.05, release_rot.set).start()
                return conn_502, resp_502
            return conn_ok, resp_ok

        with patch.object(upstream, "UPSTREAM_RETRY_COUNT", 0), patch.object(
            upstream.SIGNATURE, "get", return_value=fake_sig
        ), patch.object(
            upstream.UPSTREAM, "open_upstream_stream", side_effect=fake_open
        ):
            st, conn, resp, body = upstream.open_inference_with_retry(
                {"messages": [{"role": "user", "content": "hi"}]},
                "deepseek-v4.1-flash-free",
            )

        self.assertEqual(st, 200)
        self.assertIs(conn, conn_ok)
        self.assertIs(resp, resp_ok)
        self.assertIsNone(body)
        self.assertEqual(call_num, 2)


if __name__ == "__main__":
    unittest.main()
