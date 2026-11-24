#!/usr/bin/env bun
/**
 * Tiny local HTTP forwarder using Bun's native TLS/HTTP2 stack.
 * Exists solely so Python's bridge gets Bun's TLS fingerprint and
 * zero-copy SSE streaming when calling OpenCode's inference endpoint.
 */

const HOST = process.env.BUN_UPSTREAM_HOST || "127.0.0.1";
const PORT = Number(process.env.BUN_UPSTREAM_PORT || "29472");
const DEFAULT_TIMEOUT_MS = Number(process.env.BRIDGE_REQUEST_TIMEOUT || "240") * 1000;

const server = Bun.serve({
  hostname: HOST,
  port: PORT,
  idleTimeout: 255,
  async fetch(req) {
    const url = new URL(req.url);
    if (req.method === "GET" && url.pathname === "/health") {
      return Response.json({ status: "ok", port: server.port });
    }
    if (req.method !== "POST" || url.pathname !== "/forward") {
      return Response.json({ error: "Not found" }, { status: 404 });
    }

    let spec;
    try {
      spec = await req.json();
    } catch (err) {
      return Response.json({ error: `Invalid JSON: ${String(err)}` }, { status: 400 });
    }

    const targetUrl = String(spec.url || "https://opencode.ai/inference/openai/v1/chat/completions");
    const headers = spec.headers || {};
    const body = typeof spec.body === "string" ? spec.body : JSON.stringify(spec.body || {});
    const proxyUrl = spec.proxyUrl ? String(spec.proxyUrl) : undefined;
    const timeoutMs = Number(spec.timeoutMs || DEFAULT_TIMEOUT_MS);

    const ac = new AbortController();
    const timer = setTimeout(() => ac.abort(new Error(`Upstream timed out after ${timeoutMs}ms`)), timeoutMs);
    if (req.signal) {
      req.signal.addEventListener("abort", () => ac.abort(req.signal.reason), { once: true });
    }

    try {
      const fetchOpts = {
        method: "POST",
        headers,
        body,
        signal: ac.signal,
      };
      if (proxyUrl) {
        fetchOpts.proxy = proxyUrl;
      }

      const upstream = await fetch(targetUrl, fetchOpts);
      const outHeaders = new Headers();
      const ct = upstream.headers.get("content-type");
      if (ct) outHeaders.set("content-type", ct);
      outHeaders.set("cache-control", "no-cache");

      if (!upstream.body) {
        clearTimeout(timer);
        return new Response("", { status: upstream.status, headers: outHeaders });
      }

      // Clear overall request timeout only after upstream stream closes/cancels
      const reader = upstream.body.getReader();
      const stream = new ReadableStream({
        async pull(controller) {
          try {
            const { done, value } = await reader.read();
            if (done) {
              clearTimeout(timer);
              controller.close();
              return;
            }
            controller.enqueue(value);
          } catch (err) {
            clearTimeout(timer);
            controller.error(err);
          }
        },
        cancel(reason) {
          clearTimeout(timer);
          reader.cancel(reason).catch(() => {});
        },
      });

      return new Response(stream, {
        status: upstream.status,
        headers: outHeaders,
      });
    } catch (err) {
      clearTimeout(timer);
      const msg = err && err.message ? err.message : String(err);
      const status = /timed out|abort/i.test(msg) ? 504 : 502;
      return Response.json(
        { error: { message: `Bun upstream fetch error: ${msg}`, type: "upstream_error" } },
        { status }
      );
    }
  },
});

console.error(`[bun-upstream] listening on http://${HOST}:${server.port}`);
