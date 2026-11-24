"""Unit tests for OpenAI-to-OpenCode protocol translation and SSE assembly."""

from __future__ import annotations

import io
import json
import re
import unittest

import bridge_protocol as proto


class FakeSSEResponse:
    def __init__(self, lines: list[str]) -> None:
        self._buf = io.BytesIO("".join(lines).encode("utf-8"))

    def readline(self) -> bytes:
        return self._buf.readline()


class TestProtocol(unittest.TestCase):
    def test_session_id_matches_opencode_descending_format(self) -> None:
        sid = proto.make_opencode_session_id()
        self.assertRegex(sid, r"^ses_[0-9a-f]{12}[0-9A-Za-z]{14}$")

    def test_normalize_model_id_strips_prefix(self) -> None:
        self.assertEqual(proto.normalize_model_id("opencode/deepseek-v4.1-flash-free"), "deepseek-v4.1-flash-free")
        self.assertEqual(proto.normalize_model_id("big-pickle"), "big-pickle")
        self.assertEqual(proto.normalize_model_id(""), "big-pickle")

    def test_normalize_messages_prepends_opencode_prompt_when_missing(self) -> None:
        msgs = [
            {"role": "developer", "content": "Be terse."},
            {"role": "user", "content": "Hi"},
        ]
        out = proto.normalize_messages(msgs, "You are an AI agent running in OpenCode.")
        self.assertEqual(out[0]["role"], "system")
        self.assertIn("OpenCode", out[0]["content"])
        self.assertEqual(out[1], {"role": "system", "content": "Be terse."})
        self.assertEqual(out[2], {"role": "user", "content": "Hi"})

    def test_normalize_messages_does_not_duplicate_opencode_prompt(self) -> None:
        msgs = [
            {"role": "system", "content": "You are an AI agent running in OpenCode, custom."},
            {"role": "user", "content": "Hi"},
        ]
        out = proto.normalize_messages(msgs, "DEFAULT")
        self.assertEqual(len(out), 2)

    def test_normalize_tools_without_client_tools_forces_none(self) -> None:
        base = [{"type": "function", "function": {"name": "read"}}]
        tools, choice = proto.normalize_tools({"messages": []}, base)
        self.assertEqual(len(tools), 1)
        self.assertEqual(choice, "none")

    def test_normalize_tools_preserves_client_tools_and_dedupes(self) -> None:
        base = [
            {"type": "function", "function": {"name": "read", "description": "base"}},
            {"type": "function", "function": {"name": "glob", "description": "base"}},
        ]
        client = [
            {"type": "function", "function": {"name": "read", "description": "client"}},
            {"type": "function", "function": {"name": "ping_custom", "description": "custom"}},
        ]
        tools, choice = proto.normalize_tools({"tools": client}, base)
        names = [t["function"]["name"] for t in tools]
        self.assertEqual(names, ["read", "ping_custom", "glob"])
        self.assertEqual(tools[0]["function"]["description"], "client")
        self.assertIsNone(choice)

    def test_rewrite_sse_line_replaces_model(self) -> None:
        line = 'data: {"id":"1","model":"deepseek-v4.1-flash-free","choices":[]}'
        rewritten = proto.rewrite_sse_line(line, "oc-bridge/deepseek-v4.1-flash-free")
        self.assertEqual(json.loads(rewritten[6:])["model"], "oc-bridge/deepseek-v4.1-flash-free")
        self.assertEqual(proto.rewrite_sse_line("data: [DONE]", "x"), "data: [DONE]")

    def test_assemble_non_stream_response_tool_calls_and_reasoning(self) -> None:
        chunks = [
            'data: {"id":"c1","created":100,"choices":[{"index":0,"delta":{"role":"assistant","content":"","reasoning_content":"Think "}}]}\n\n',
            'data: {"id":"c1","created":100,"choices":[{"index":0,"delta":{"reasoning_content":"step"}}]}\n\n',
            'data: {"id":"c1","created":100,"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"call_1","type":"function","function":{"name":"ping_custom","arguments":"{\\"msg\\":"}}]}}]}\n\n',
            'data: {"id":"c1","created":100,"choices":[{"index":0,"finish_reason":"tool_calls","delta":{"tool_calls":[{"index":0,"function":{"arguments":"\\"hi\\"}"}}]}}]}\n\n',
            'data: {"id":"c1","created":100,"choices":[],"usage":{"prompt_tokens":10,"completion_tokens":5,"total_tokens":15}}\n\n',
            'data: [DONE]\n\n',
        ]
        out = proto.assemble_non_stream_response(FakeSSEResponse(chunks), "oc-bridge/deepseek")
        self.assertEqual(out["model"], "oc-bridge/deepseek")
        ch = out["choices"][0]
        self.assertEqual(ch["finish_reason"], "tool_calls")
        self.assertIsNone(ch["message"]["content"])
        self.assertEqual(ch["message"]["reasoning_content"], "Think step")
        self.assertEqual(
            ch["message"]["tool_calls"],
            [{"id": "call_1", "type": "function", "function": {"name": "ping_custom", "arguments": '{"msg":"hi"}'}}],
        )
        self.assertEqual(out["usage"]["total_tokens"], 15)

    def test_is_rate_limit_error(self) -> None:
        self.assertTrue(proto.is_rate_limit_error(429, {}))
        self.assertTrue(proto.is_rate_limit_error(502, {"error": {"type": "FreeUsageLimitError"}}))
        self.assertFalse(proto.is_rate_limit_error(500, {"error": {"message": "Internal error"}}))

    def test_extract_free_chat_models_filters_paid_and_non_chat(self) -> None:
        raw = {
            "data": [
                {
                    "id": "deepseek-v4.1-flash-free",
                    "type": "chat",
                    "created": 1700000000,
                    "owned_by": "opencode",
                    "modalities": {"input": ["text", "image/png"], "output": ["text"]},
                    "capabilities": {"tools": {"supported": True}, "reasoning": {"supported": True}},
                    "limit": {"context": 200000, "output": 65536},
                    "cost": {"text": {"input": {"price": 0}, "output": {"price": 0}}},
                },
                {
                    "id": "jev-1.13-free",
                    "type": "decision",
                    "cost": {"text": {"input": {"price": 0}, "output": {"price": 0}}},
                },
                {
                    "id": "paid-pro",
                    "type": "chat",
                    "cost": {"text": {"input": {"price": 1.5}, "output": {"price": 6.0}}},
                },
            ]
        }
        models = proto.extract_free_chat_models(raw)
        self.assertEqual([m["id"] for m in models], ["deepseek-v4.1-flash-free"])
        self.assertEqual(models[0]["modalities"]["input"], ["text", "image/png"])
        self.assertTrue(models[0]["capabilities"]["tools"]["supported"])
        self.assertNotIn("limit", models[0])


if __name__ == "__main__":
    unittest.main()
