"""Unit tests for upstream forwarding and retry behavior."""

from __future__ import annotations

import http.client
import json
import unittest
from unittest.mock import MagicMock, patch

import bridge_upstream as upstream


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


if __name__ == "__main__":
    unittest.main()
