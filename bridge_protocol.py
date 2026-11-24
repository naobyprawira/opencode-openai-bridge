"""OpenAI-to-OpenCode request normalization and SSE response assembly."""

from __future__ import annotations

import http.client
import json
import secrets
import threading
import time
from typing import Any


_SESSION_LOCK = threading.Lock()
_SESSION_LAST_MS = 0
_SESSION_SEQ = 0
_BASE62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"


def is_rate_limit_error(status: int, body: Any) -> bool:
    if status == 429:
        return True
    if status in (502, 503) and isinstance(body, dict):
        err = body.get("error") if isinstance(body.get("error"), dict) else body
        msg = str(err.get("message", "")).lower()
        etype = str(err.get("type", "")).lower()
        if (
            err.get("status") == 429
            or "freeusagelimit" in etype
            or "rate_limit" in etype
            or "rate_limit" in msg
            or "free usage limit" in msg
            or "too many requests" in msg
        ):
            return True
    return False


def normalize_model_id(raw_model: str | None) -> str:
    if not raw_model:
        return "big-pickle"
    m = raw_model.strip()
    if m.startswith("opencode/"):
        m = m[len("opencode/") :]
    return m or "big-pickle"


def _is_free_chat_model(entry: dict[str, Any]) -> bool:
    mid = entry.get("id")
    if not isinstance(mid, str) or not mid:
        return False
    mtype = entry.get("type")
    if mtype and mtype != "chat":
        return False
    cost = entry.get("cost")
    if not isinstance(cost, dict):
        return mid.endswith("-free")
    txt = cost.get("text")
    if not isinstance(txt, dict):
        return mid.endswith("-free")
    inp = txt.get("input") if isinstance(txt.get("input"), dict) else {}
    out = txt.get("output") if isinstance(txt.get("output"), dict) else {}
    return inp.get("price") == 0 and out.get("price") == 0


def extract_free_chat_models(raw_payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Filter live OpenCode /v1/models catalog to active free chat models."""
    items = raw_payload.get("data")
    if not isinstance(items, list):
        return []

    now = int(time.time())
    out: list[dict[str, Any]] = []
    for m in items:
        if not isinstance(m, dict) or not _is_free_chat_model(m):
            continue
        item: dict[str, Any] = {
            "id": m["id"],
            "object": "model",
            "created": m.get("created") if isinstance(m.get("created"), int) else now,
            "owned_by": m.get("owned_by") or "opencode",
        }
        for extra in ("name", "description", "modalities", "capabilities", "limit"):
            if m.get(extra) is not None:
                item[extra] = m[extra]
        out.append(item)
    return out


def make_opencode_session_id() -> str:
    """Generate a descending OpenCode session ID (48-bit ~ms + 14 base62)."""
    global _SESSION_LAST_MS, _SESSION_SEQ
    with _SESSION_LOCK:
        now_ms = int(time.time() * 1000)
        if now_ms != _SESSION_LAST_MS:
            _SESSION_LAST_MS = now_ms
            _SESSION_SEQ = 0
        _SESSION_SEQ += 1
        val = now_ms * 0x1000 + _SESSION_SEQ
    inv = (~val) & 0xFFFFFFFFFFFF
    hex12 = f"{inv:012x}"
    rand14 = "".join(secrets.choice(_BASE62) for _ in range(14))
    return f"ses_{hex12}{rand14}"


def make_opencode_headers(user_agent: str) -> dict[str, str]:
    trace_id = secrets.token_hex(16)
    span_id = secrets.token_hex(8)
    parent_id = secrets.token_hex(8)
    sid = make_opencode_session_id()
    proj = secrets.token_hex(20)
    return {
        "Authorization": "Bearer public",
        "Content-Type": "application/json",
        "User-Agent": user_agent,
        "Accept": "*/*",
        "b3": f"{trace_id}-{span_id}-1-{parent_id}",
        "traceparent": f"00-{trace_id}-{span_id}-01",
        "x-opencode-client": "cli",
        "x-opencode-project": proj,
        "x-opencode-session": sid,
        "x-opencode-session-id": sid,
        "x-session-affinity": sid,
        "x-session-id": sid,
    }


def normalize_messages(
    messages: list[dict[str, Any]], base_sys_prompt: str
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        item = dict(m)
        if item.get("role") == "developer":
            item["role"] = "system"
        out.append(item)

    already_opencode = (
        bool(out)
        and out[0].get("role") == "system"
        and isinstance(out[0].get("content"), str)
        and "You are an AI agent running in OpenCode" in out[0]["content"]
    )
    if not already_opencode and base_sys_prompt:
        out.insert(0, {"role": "system", "content": base_sys_prompt})
    return out


def normalize_tools(
    req: dict[str, Any], base_tools: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], Any]:
    client_tools = req.get("tools")
    has_client_tools = isinstance(client_tools, list) and len(client_tools) > 0

    merged: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    if has_client_tools:
        for t in client_tools:
            if not isinstance(t, dict):
                continue
            merged.append(t)
            fn = t.get("function") if isinstance(t.get("function"), dict) else {}
            name = fn.get("name") or t.get("name")
            if isinstance(name, str) and name:
                seen_names.add(name)

    for bt in base_tools:
        if not isinstance(bt, dict):
            continue
        fn = bt.get("function") if isinstance(bt.get("function"), dict) else {}
        name = fn.get("name") or bt.get("name")
        if isinstance(name, str) and name in seen_names:
            continue
        merged.append(bt)

    tool_choice = req.get("tool_choice")
    if not has_client_tools and tool_choice is None:
        tool_choice = "none"

    return merged, tool_choice


def build_upstream_payload(
    req: dict[str, Any], model_id: str, sig: dict[str, Any]
) -> dict[str, Any]:
    messages = normalize_messages(req["messages"], sig.get("system_prompt", ""))
    tools, tool_choice = normalize_tools(req, sig.get("base_tools", []))

    payload: dict[str, Any] = {
        "model": model_id,
        "messages": messages,
        "tools": tools,
        "stream": True,
        "stream_options": {"include_usage": True},
        "max_completion_tokens": req.get("max_completion_tokens")
        or req.get("max_tokens")
        or 32000,
    }
    if tool_choice is not None:
        payload["tool_choice"] = tool_choice

    for k in (
        "temperature",
        "top_p",
        "stop",
        "response_format",
        "reasoning_effort",
        "seed",
        "presence_penalty",
        "frequency_penalty",
        "parallel_tool_calls",
    ):
        if k in req and req[k] is not None:
            payload[k] = req[k]
    return payload


def rewrite_sse_line(raw_line: str, requested_model: str) -> str:
    if not raw_line.startswith("data: "):
        return raw_line
    data_str = raw_line[6:].strip()
    if data_str == "[DONE]" or not data_str:
        return raw_line
    try:
        obj = json.loads(data_str)
        if isinstance(obj, dict) and "model" in obj:
            obj["model"] = requested_model
        return "data: " + json.dumps(obj, separators=(",", ":"))
    except Exception:
        return raw_line


def assemble_non_stream_response(
    resp: http.client.HTTPResponse, requested_model: str
) -> dict[str, Any]:
    cmpl_id = f"chatcmpl-{secrets.token_hex(12)}"
    created = int(time.time())
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_calls_map: dict[int, dict[str, Any]] = {}
    finish_reason = "stop"
    usage: dict[str, Any] = {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
    }

    while True:
        raw = resp.readline()
        if not raw:
            break
        line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        if not line.startswith("data: "):
            continue
        data_str = line[6:].strip()
        if data_str == "[DONE]":
            break
        if not data_str:
            continue
        try:
            chunk = json.loads(data_str)
        except Exception:
            continue
        if not isinstance(chunk, dict):
            continue
        if isinstance(chunk.get("id"), str) and chunk["id"]:
            cmpl_id = chunk["id"]
        if isinstance(chunk.get("created"), int):
            created = chunk["created"]
        if isinstance(chunk.get("usage"), dict):
            usage = chunk["usage"]

        for ch in chunk.get("choices") or []:
            if not isinstance(ch, dict):
                continue
            if ch.get("finish_reason"):
                finish_reason = ch["finish_reason"]
            delta = ch.get("delta") or {}
            if not isinstance(delta, dict):
                continue
            if isinstance(delta.get("content"), str):
                content_parts.append(delta["content"])
            rc = delta.get("reasoning_content")
            if isinstance(rc, str):
                reasoning_parts.append(rc)
            elif isinstance(delta.get("reasoning"), str):
                reasoning_parts.append(delta["reasoning"])

            for tc in delta.get("tool_calls") or []:
                if not isinstance(tc, dict):
                    continue
                idx = int(tc.get("index", 0) or 0)
                entry = tool_calls_map.setdefault(
                    idx,
                    {
                        "id": tc.get("id") or f"call_{secrets.token_hex(8)}",
                        "type": tc.get("type") or "function",
                        "function": {"name": "", "arguments": ""},
                    },
                )
                if tc.get("id"):
                    entry["id"] = tc["id"]
                if tc.get("type"):
                    entry["type"] = tc["type"]
                fn = tc.get("function") or {}
                if isinstance(fn, dict):
                    if isinstance(fn.get("name"), str) and fn["name"]:
                        entry["function"]["name"] += fn["name"]
                    if isinstance(fn.get("arguments"), str):
                        entry["function"]["arguments"] += fn["arguments"]

    joined_content = "".join(content_parts)
    msg: dict[str, Any] = {
        "role": "assistant",
        "content": joined_content if joined_content else (None if tool_calls_map else ""),
    }
    if reasoning_parts:
        msg["reasoning_content"] = "".join(reasoning_parts)
    if tool_calls_map:
        msg["tool_calls"] = [tool_calls_map[k] for k in sorted(tool_calls_map)]

    return {
        "id": cmpl_id,
        "object": "chat.completion",
        "created": created,
        "model": requested_model,
        "choices": [
            {
                "index": 0,
                "message": msg,
                "finish_reason": finish_reason,
            }
        ],
        "usage": usage,
    }
