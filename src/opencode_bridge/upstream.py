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
from urllib.parse import urlsplit

from opencode_bridge.config import (
    BUN_BIN,
    BUN_UPSTREAM_JS,
    DEFAULT_MODELS,
    INFERENCE_URL,
    MAX_CONCURRENT_UPSTREAM,
    MIN_ROTATE_BUDGET_SEC,
    MODELS_TTL_SEC,
    MODELS_URL,
    RATE_LIMIT_RETRIES,
    RECENT_SUCCESS_GRACE_SEC,
    REQUEST_TIMEOUT_SEC,
    RETRY_BUDGET_SEC,
    STATE_DIR,
    STREAM_GRACE_SEC,
    UPSTREAM_HOST,
    UPSTREAM_PORT,
    UPSTREAM_PROXY_URL,
    UPSTREAM_RETRY_COUNT,
    UPSTREAM_ROTATE_TOKEN,
    UPSTREAM_ROTATE_URL,
    UPSTREAM_WORKERS,
    WORKER_DOCKER_BIN,
    WORKER_DOCKER_SUDO,
    WORKER_IDLE_SEC,
    WORKER_PACING_MS,
    WORKER_REAPER_INTERVAL_SEC,
    WORKER_SUMMON_TIMEOUT_SEC,
    WORKERS_MAX,
    WORKERS_STANDBY,
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
        proxy_url: str | None = None,
    ) -> tuple[http.client.HTTPConnection, http.client.HTTPResponse]:
        self.ensure_running()
        effective_proxy = UPSTREAM_PROXY_URL if proxy_url is None else proxy_url
        spec: dict[str, Any] = {
            "url": INFERENCE_URL,
            "method": "POST",
            "headers": headers,
            "body": payload,
            "timeoutMs": int(timeout * 1000),
        }
        if effective_proxy:
            spec["proxyUrl"] = effective_proxy
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
_ACTIVE_COND = threading.Condition()
_ACTIVE_STREAMS = 0
_UPSTREAM_SEM = threading.Semaphore(MAX_CONCURRENT_UPSTREAM)
_CONN_LOCK = threading.Lock()
_CONN_WORKERS: dict[int, "UpstreamWorker"] = {}


def _trigger_rotate_webhook(
    force: bool = False,
    url: str | None = None,
    token: str | None = None,
    allow_force_escalation: bool = True,
) -> bool:
    target_url = UPSTREAM_ROTATE_URL if url is None else url
    if not target_url:
        return False
    req_url = target_url
    if force:
        sep = "&" if "?" in req_url else "?"
        req_url = f"{req_url}{sep}force=1"
    headers = {"Content-Type": "application/json"}
    auth_token = UPSTREAM_ROTATE_TOKEN if token is None else token
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"
    req = urllib.request.Request(req_url, data=b"{}", headers=headers, method="POST")
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
                    if (
                        not force
                        and allow_force_escalation
                        and (data.get("cooldown") or data.get("rotated") is False)
                    ):
                        log("Rotate webhook returned cooldown/unchanged IP; escalating with force=1...")
                        if url is None and token is None:
                            return _trigger_rotate_webhook(force=True)
                        return _trigger_rotate_webhook(
                            force=True, url=url, token=token
                        )
                    if data.get("cooldown") and not data.get("rotated"):
                        return False
                    return bool(data.get("ok", True))
            return True
    except Exception as exc:
        log(f"Warning: upstream rotate webhook failed: {exc}")
        return False


class UpstreamWorker:
    """State and concurrency controls for a single upstream egress proxy (or direct egress)."""

    def __init__(
        self,
        name: str,
        proxy_url: str = "",
        rotate_url: str = "",
        rotate_token: str = "",
        is_primary: bool = False,
        container: str = "",
        on_demand: bool = False,
        enabled: bool | None = None,
    ) -> None:
        self.name = name
        self.proxy_url = proxy_url
        self.rotate_url = rotate_url
        self.rotate_token = rotate_token
        self.is_primary = is_primary
        self.container = container
        self.on_demand = on_demand
        self.enabled: bool = (not on_demand) if enabled is None else enabled
        self.starting: bool = False
        self.stopping: bool = False
        self.last_used_at: float = time.monotonic()
        self.last_summon_failed_at: float = 0.0
        self._state_cond = threading.Condition()
        self._lock = _ROTATE_LOCK if is_primary else threading.Lock()
        self._gen = 0
        self._active_cond = _ACTIVE_COND if is_primary else threading.Condition()
        self._active_streams = 0
        self._sem = _UPSTREAM_SEM if is_primary else threading.Semaphore(MAX_CONCURRENT_UPSTREAM)
        self._pace_lock = threading.Lock()
        self._last_handshake_at: float | None = None
        self.last_success_at: float | None = None
        self.last_success_gen: int | None = None
        self.last_rotate_at: float | None = None
        self.cooldown_until: float = 0.0
        self.rotating: bool = False
        self.pending_rotate: bool = False
        self._pending_rotate_gen: int | None = None
        self._burst_retry_gen: int | None = None

    def effective_proxy_url(self) -> str:
        if self.is_primary and not self.proxy_url and UPSTREAM_PROXY_URL:
            return UPSTREAM_PROXY_URL
        return self.proxy_url

    def effective_rotate_url(self) -> str:
        if self.is_primary and not self.rotate_url and UPSTREAM_ROTATE_URL:
            return UPSTREAM_ROTATE_URL
        return self.rotate_url

    def effective_rotate_token(self) -> str:
        if self.is_primary and not self.rotate_token and UPSTREAM_ROTATE_TOKEN:
            return UPSTREAM_ROTATE_TOKEN
        return self.rotate_token

    def health_url(self) -> str:
        p = self.effective_proxy_url().strip()
        if not p:
            return ""
        parsed = urlsplit(p)
        if not parsed.scheme or not parsed.hostname:
            return ""
        host_port = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
        if parsed.port is not None:
            host_port = f"{host_port}:{parsed.port}"
        return f"{parsed.scheme}://{host_port}/health"

    def touch_used(self) -> None:
        self.last_used_at = time.monotonic()

    def get_gen(self) -> int:
        if self.is_primary:
            return _ROTATE_GEN
        return self._gen

    def set_gen(self, val: int) -> None:
        global _ROTATE_GEN
        if self.is_primary:
            _ROTATE_GEN = val
        else:
            self._gen = val

    def get_active_streams(self) -> int:
        with self._active_cond:
            return _ACTIVE_STREAMS if self.is_primary else self._active_streams

    def begin_stream(self) -> None:
        global _ACTIVE_STREAMS
        self.touch_used()
        with self._active_cond:
            if self.is_primary:
                _ACTIVE_STREAMS += 1
            else:
                self._active_streams += 1

    def end_stream(self) -> None:
        global _ACTIVE_STREAMS
        deferred_gen: int | None = None
        self.touch_used()
        with self._active_cond:
            if self.is_primary:
                if _ACTIVE_STREAMS > 0:
                    _ACTIVE_STREAMS -= 1
                remaining_streams = _ACTIVE_STREAMS
            else:
                if self._active_streams > 0:
                    self._active_streams -= 1
                remaining_streams = self._active_streams
            if remaining_streams == 0:
                self._active_cond.notify_all()
                if self.pending_rotate:
                    self.pending_rotate = False
                    deferred_gen = self._pending_rotate_gen
                    self._pending_rotate_gen = None
        if (
            deferred_gen is not None
            and deferred_gen == self.get_gen()
            and self.effective_rotate_url()
            and not self.rotating
            and self.enabled
            and not self.stopping
        ):
            log(f"Active stream(s) completed on {self.name}; running deferred WARP rotation...")
            threading.Thread(
                target=self.rotate_for_generation,
                args=(deferred_gen,),
                kwargs={"grace_sec": 0.0, "allow_force": False},
                daemon=True,
            ).start()

    def wait_for_active_streams(self, timeout: float) -> bool:
        if timeout <= 0:
            return self.get_active_streams() == 0
        deadline = time.monotonic() + timeout
        with self._active_cond:
            while (_ACTIVE_STREAMS if self.is_primary else self._active_streams) > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._active_cond.wait(remaining)
        return True

    def pace_handshake(self) -> None:
        if WORKER_PACING_MS <= 0:
            return
        min_gap = WORKER_PACING_MS / 1000.0
        with self._pace_lock:
            now = time.monotonic()
            if self._last_handshake_at is not None:
                wait = min_gap - (now - self._last_handshake_at)
                if wait > 0:
                    time.sleep(wait)
            self._last_handshake_at = time.monotonic()

    def record_success(self) -> None:
        now = time.monotonic()
        self.last_success_at = now
        self.last_used_at = now
        self.last_success_gen = self.get_gen()
        self.cooldown_until = 0.0
        self._burst_retry_gen = None

    def has_recent_success(self, window_sec: float = RECENT_SUCCESS_GRACE_SEC) -> bool:
        return (
            self.last_success_at is not None
            and self.last_success_gen == self.get_gen()
            and (time.monotonic() - self.last_success_at) < window_sec
        )

    def can_burst_retry(self, gen: int) -> bool:
        return self._burst_retry_gen != gen

    def mark_burst_retry(self, gen: int) -> None:
        self._burst_retry_gen = gen

    def load_score(self) -> tuple[int, int, int]:
        now = time.monotonic()
        return (
            1 if self.rotating else 0,
            1 if self.cooldown_until > now else 0,
            self.get_active_streams(),
        )

    def rotate_for_generation(
        self,
        expected_gen: int,
        grace_sec: float = STREAM_GRACE_SEC,
        allow_force: bool = True,
        remaining_budget: float | None = None,
    ) -> bool:
        rot_url = self.effective_rotate_url()
        with self._lock:
            if self.get_gen() != expected_gen:
                return True
            active_now = self.get_active_streams()
            if (
                remaining_budget is not None
                and active_now > 0
                and remaining_budget < MIN_ROTATE_BUDGET_SEC
            ):
                with self._active_cond:
                    self.pending_rotate = True
                    self._pending_rotate_gen = expected_gen
                log(
                    f"Skipping WARP rotation on {self.name}: {active_now} active stream(s) "
                    f"and remaining budget ({remaining_budget:.1f}s) < {MIN_ROTATE_BUDGET_SEC:.0f}s."
                )
                return False
            self.rotating = True
            try:
                if active_now > 0:
                    drained = self.wait_for_active_streams(grace_sec)
                    if not drained:
                        with self._active_cond:
                            self.pending_rotate = True
                            self._pending_rotate_gen = expected_gen
                        log(
                            f"Active stream(s) still running on {self.name} after "
                            f"{grace_sec:.1f}s grace; protecting stream and deferring rotation."
                        )
                        self.cooldown_until = time.monotonic() + 2.0
                        return False

                if self.is_primary and rot_url == UPSTREAM_ROTATE_URL:
                    ok = (
                        _trigger_rotate_webhook(force=False)
                        if allow_force
                        else _trigger_rotate_webhook(
                            force=False, allow_force_escalation=False
                        )
                    )
                else:
                    if not rot_url:
                        return False
                    ok = _trigger_rotate_webhook(
                        force=False,
                        url=rot_url,
                        token=self.effective_rotate_token(),
                        allow_force_escalation=allow_force,
                    )
                if ok:
                    self.set_gen(self.get_gen() + 1)
                    self.last_rotate_at = time.monotonic()
                    self.cooldown_until = 0.0
                    with self._active_cond:
                        self.pending_rotate = False
                        self._pending_rotate_gen = None
                    return True
                return False
            finally:
                self.rotating = False

    def snapshot(self) -> dict[str, Any]:
        now = time.monotonic()
        return {
            "name": self.name,
            "container": self.container or None,
            "on_demand": self.on_demand,
            "enabled": self.enabled,
            "starting": self.starting,
            "proxy_configured": bool(self.effective_proxy_url()),
            "rotate_configured": bool(self.effective_rotate_url()),
            "active_streams": self.get_active_streams(),
            "rotating": self.rotating,
            "pending_rotate": self.pending_rotate,
            "in_cooldown": self.cooldown_until > now,
            "generation": self.get_gen(),
        }


class WorkerPool:
    """Manages standby and summon-on-event WARP proxy workers for concurrent requests."""

    def __init__(
        self,
        worker_specs: list[dict[str, Any]],
        *,
        standby: int = 0,
        max_workers: int = 0,
        idle_sec: float = 600.0,
        summon_timeout_sec: float = 25.0,
        reaper_interval_sec: float = 30.0,
    ) -> None:
        self.workers: list[UpstreamWorker] = []
        specs = list(worker_specs)
        if max_workers > 0 and len(specs) > max_workers:
            specs = specs[:max_workers]

        if specs:
            eff_standby = max(1, min(standby, len(specs))) if standby > 0 else 0
            for idx, spec in enumerate(specs):
                proxy = str(spec.get("proxy") or "")
                rotate = str(spec.get("rotate") or "")
                token = str(spec.get("token") or "")
                container = str(spec.get("container") or "")
                if eff_standby > 0:
                    on_demand = idx >= eff_standby
                else:
                    on_demand = bool(spec.get("on_demand", False))
                label = container or proxy or rotate or f"worker-{idx}"
                self.workers.append(
                    UpstreamWorker(
                        name=label,
                        proxy_url=proxy,
                        rotate_url=rotate,
                        rotate_token=token,
                        is_primary=(idx == 0),
                        container=container,
                        on_demand=on_demand,
                        enabled=(not on_demand),
                    )
                )
        else:
            self.workers.append(
                UpstreamWorker(
                    name="default",
                    proxy_url=UPSTREAM_PROXY_URL,
                    rotate_url=UPSTREAM_ROTATE_URL,
                    rotate_token=UPSTREAM_ROTATE_TOKEN,
                    is_primary=True,
                )
            )
        self.idle_sec = max(0.0, float(idle_sec))
        self.summon_timeout_sec = max(2.0, float(summon_timeout_sec))
        self.reaper_interval_sec = max(1.0, float(reaper_interval_sec))
        self._rr_lock = threading.Lock()
        self._rr_idx = 0
        self._summon_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._manager_thread: threading.Thread | None = None

    def summary(self) -> dict[str, Any]:
        active = sum(1 for w in self.workers if w.enabled and not w.stopping)
        standby = sum(1 for w in self.workers if not w.on_demand)
        starting = sum(1 for w in self.workers if w.starting)
        return {
            "total": len(self.workers),
            "standby": standby,
            "max": len(self.workers),
            "active": active,
            "starting": starting,
            "idle_sec": self.idle_sec,
        }

    def enabled_workers(self) -> list[UpstreamWorker]:
        return [w for w in self.workers if w.enabled and not w.stopping]

    def dormant_workers(self, include_cooling: bool = False) -> list[UpstreamWorker]:
        now = time.monotonic()
        return [
            w
            for w in self.workers
            if w.on_demand
            and not w.enabled
            and not w.starting
            and not w.stopping
            and (include_cooling or (now - w.last_summon_failed_at) >= 15.0)
        ]

    def has_unburdened_enabled_worker(
        self, exclude: set[UpstreamWorker] | None = None
    ) -> bool:
        now = time.monotonic()
        for w in self.enabled_workers():
            if exclude and w in exclude:
                continue
            if not w.rotating and w.cooldown_until <= now and w.get_active_streams() == 0:
                return True
        return False

    def all_enabled_unusable(
        self, exclude: set[UpstreamWorker] | None = None
    ) -> bool:
        now = time.monotonic()
        candidates = [
            w for w in self.enabled_workers() if not exclude or w not in exclude
        ]
        if not candidates:
            return True
        return all(w.rotating or w.cooldown_until > now for w in candidates)

    def pick_worker(self, exclude: set[UpstreamWorker] | None = None) -> UpstreamWorker:
        pool = self.enabled_workers() or self.workers
        candidates = [w for w in pool if not exclude or w not in exclude]
        if not candidates:
            candidates = pool
        if len(candidates) == 1:
            return candidates[0]
        with self._rr_lock:
            n = len(candidates)
            start = self._rr_idx % n
            rotated_order = candidates[start:] + candidates[:start]
            chosen = min(rotated_order, key=lambda w: w.load_score())
            self._rr_idx = (self._rr_idx + 1) % n
            return chosen

    @staticmethod
    def _use_sudo_for_docker() -> bool:
        mode = WORKER_DOCKER_SUDO
        if mode in ("1", "true", "yes"):
            return True
        if mode in ("0", "false", "no"):
            return False
        return not os.access("/var/run/docker.sock", os.R_OK | os.W_OK)

    def _run_docker(self, args: list[str], timeout: float = 15.0) -> tuple[bool, str]:
        cmd = [WORKER_DOCKER_BIN, *args]
        if self._use_sudo_for_docker():
            cmd = ["sudo", "-n", *cmd]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            out = (res.stderr or res.stdout or "").strip()
            return res.returncode == 0, out
        except Exception as exc:
            return False, str(exc)

    def _check_worker_health(self, worker: UpstreamWorker, timeout: float = 2.0) -> bool:
        url = worker.health_url()
        if not url:
            return True
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                if resp.status != 200:
                    return False
                raw = resp.read().decode("utf-8", errors="replace")
                if not raw:
                    return True
                data = json.loads(raw)
                if isinstance(data, dict) and "warp_connected" in data:
                    return bool(data.get("warp_connected"))
                return True
        except Exception:
            return False

    def _boot_claimed_worker(
        self, worker: UpstreamWorker, timeout: float
    ) -> UpstreamWorker | None:
        started_at = time.monotonic()
        deadline = started_at + max(2.0, timeout)
        with worker._state_cond:
            while worker.stopping:
                rem = deadline - time.monotonic()
                if rem <= 0:
                    worker.starting = False
                    worker.last_summon_failed_at = time.monotonic()
                    worker._state_cond.notify_all()
                    return None
                worker._state_cond.wait(rem)

        healthy = self._check_worker_health(worker, timeout=1.5)
        if not healthy and worker.container:
            log(f"Summoning on-demand worker {worker.name} ({worker.container})...")
            ok, err = self._run_docker(["start", worker.container], timeout=15.0)
            if not ok:
                log(f"Warning: failed to start container {worker.container}: {err}")
                with worker._state_cond:
                    worker.starting = False
                    worker.last_summon_failed_at = time.monotonic()
                    worker._state_cond.notify_all()
                return None

        while not healthy and time.monotonic() < deadline:
            healthy = self._check_worker_health(worker, timeout=2.0)
            if healthy:
                break
            time.sleep(min(0.5, max(0.05, deadline - time.monotonic())))

        with worker._state_cond:
            worker.starting = False
            if healthy:
                now = time.monotonic()
                worker.enabled = True
                worker.cooldown_until = 0.0
                worker.last_used_at = now
                worker.last_summon_failed_at = 0.0
                worker._state_cond.notify_all()
                log(
                    f"Summoned worker {worker.name} ready in "
                    f"{now - started_at:.1f}s."
                )
                return worker
            worker.last_summon_failed_at = time.monotonic()
            worker._state_cond.notify_all()
        log(f"Warning: summoned worker {worker.name} timed out before becoming healthy.")
        return None

    def _wait_for_starting_worker(
        self, worker: UpstreamWorker, timeout: float
    ) -> UpstreamWorker | None:
        deadline = time.monotonic() + max(0.5, timeout)
        with worker._state_cond:
            while worker.starting:
                rem = deadline - time.monotonic()
                if rem <= 0:
                    return None
                worker._state_cond.wait(rem)
            return worker if worker.enabled and not worker.stopping else None

    def summon_next_sync(
        self,
        timeout: float | None = None,
        exclude: set[UpstreamWorker] | None = None,
    ) -> UpstreamWorker | None:
        eff_timeout = self.summon_timeout_sec if timeout is None else max(1.0, timeout)
        target: UpstreamWorker | None = None
        claimed = False
        with self._summon_lock:
            starting = [
                w for w in self.workers if w.starting and (not exclude or w not in exclude)
            ]
            if starting:
                target = starting[0]
                claimed = False
            else:
                dormant = [
                    w for w in self.dormant_workers() if not exclude or w not in exclude
                ]
                if dormant:
                    target = dormant[0]
                    target.starting = True
                    claimed = True
        if target is None:
            return None
        if not claimed:
            res = self._wait_for_starting_worker(target, eff_timeout)
            if res is not None:
                return res
            # If the in-flight boot failed, fall through to try claiming next dormant worker
            return self.summon_next_sync(
                timeout=eff_timeout,
                exclude=(exclude or set()) | {target},
            )
        return self._boot_claimed_worker(target, eff_timeout)

    def summon_next_async(self) -> UpstreamWorker | None:
        target: UpstreamWorker | None = None
        with self._summon_lock:
            if any(w.starting for w in self.workers):
                return None
            dormant = self.dormant_workers()
            if not dormant:
                return None
            target = dormant[0]
            target.starting = True
        threading.Thread(
            target=self._boot_claimed_worker,
            args=(target, self.summon_timeout_sec),
            daemon=True,
        ).start()
        return target

    def reconcile_startup(self) -> None:
        for w in self.workers:
            if w.on_demand:
                if self._check_worker_health(w, timeout=1.0):
                    with w._state_cond:
                        w.enabled = True
                        w.last_used_at = time.monotonic()
                    log(f"Adopted already-running on-demand worker {w.name}.")
            else:
                with w._state_cond:
                    w.enabled = True
                if w.container and not self._check_worker_health(w, timeout=1.0):
                    ok, err = self._run_docker(["start", w.container], timeout=15.0)
                    if not ok:
                        log(f"Warning: standby container {w.container} start failed: {err}")

    def reap_idle_workers(self, now: float | None = None) -> int:
        if self.idle_sec <= 0:
            return 0
        cur_now = time.monotonic() if now is None else now
        stopped = 0
        for w in self.workers:
            if not w.on_demand:
                continue
            should_stop = False
            with self._summon_lock:
                if (
                    w.enabled
                    and not w.starting
                    and not w.stopping
                    and not w.rotating
                    and w.get_active_streams() == 0
                    and (cur_now - w.last_used_at) >= self.idle_sec
                ):
                    w.enabled = False
                    w.stopping = True
                    should_stop = True
            if not should_stop:
                continue
            log(
                f"Worker {w.name} idle for {int(cur_now - w.last_used_at)}s "
                f"(>= {int(self.idle_sec)}s); despawning..."
            )
            if w.container:
                ok, err = self._run_docker(["stop", w.container], timeout=25.0)
                if not ok:
                    log(f"Warning: failed stopping idle container {w.container}: {err}")
            with w._state_cond:
                w.stopping = False
                w._state_cond.notify_all()
            stopped += 1
        return stopped

    def start_manager(self) -> None:
        if not any(w.on_demand or w.container for w in self.workers):
            return
        if self._manager_thread is not None and self._manager_thread.is_alive():
            return
        self._stop_event.clear()

        def _loop() -> None:
            try:
                self.reconcile_startup()
            except Exception as exc:
                log(f"Warning: worker pool reconcile_startup failed: {exc}")
            while not self._stop_event.wait(self.reaper_interval_sec):
                try:
                    self.reap_idle_workers()
                except Exception as exc:
                    log(f"Warning: worker pool idle reaper failed: {exc}")

        self._manager_thread = threading.Thread(
            target=_loop, name="worker-pool-manager", daemon=True
        )
        self._manager_thread.start()

    def stop_manager(self) -> None:
        self._stop_event.set()
        t = self._manager_thread
        if t is not None and t.is_alive():
            t.join(timeout=2.0)

    def snapshot(self) -> list[dict[str, Any]]:
        return [w.snapshot() for w in self.workers]


WORKER_POOL = WorkerPool(
    UPSTREAM_WORKERS,
    standby=WORKERS_STANDBY,
    max_workers=WORKERS_MAX,
    idle_sec=WORKER_IDLE_SEC,
    summon_timeout_sec=WORKER_SUMMON_TIMEOUT_SEC,
    reaper_interval_sec=WORKER_REAPER_INTERVAL_SEC,
)


def begin_active_stream(conn: http.client.HTTPConnection | None = None) -> None:
    worker: UpstreamWorker | None = None
    if conn is not None:
        with _CONN_LOCK:
            worker = _CONN_WORKERS.get(id(conn))
    if worker is None:
        worker = WORKER_POOL.workers[0]
    worker.begin_stream()


def end_active_stream(conn: http.client.HTTPConnection | None = None) -> None:
    worker: UpstreamWorker | None = None
    if conn is not None:
        with _CONN_LOCK:
            worker = _CONN_WORKERS.pop(id(conn), None)
    if worker is None:
        worker = WORKER_POOL.workers[0]
    worker.end_stream()


def _wait_for_active_streams(timeout: float) -> None:
    WORKER_POOL.workers[0].wait_for_active_streams(timeout)


def _rotate_for_generation(expected_gen: int, grace_sec: float = STREAM_GRACE_SEC) -> bool:
    return WORKER_POOL.workers[0].rotate_for_generation(
        expected_gen, grace_sec=grace_sec
    )


def _register_conn_worker(
    conn: http.client.HTTPConnection, worker: UpstreamWorker
) -> None:
    with _CONN_LOCK:
        _CONN_WORKERS[id(conn)] = worker


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
    inflight_rot_retries = 0
    failovers_used = 0
    burst_waits = 0
    max_inflight_rot_retries = max(1, RATE_LIMIT_RETRIES + 1)
    saw_rate_limit = False
    rate_limit_body: Any = None
    tried_workers: set[UpstreamWorker] = set()
    multi_worker = len(WORKER_POOL.workers) > 1
    deadline = time.monotonic() + RETRY_BUDGET_SEC

    def _finish_error(st: int, body: Any) -> tuple[int, None, None, Any]:
        if saw_rate_limit and st in (502, 503, 504):
            return 429, None, None, rate_limit_body or {
                "error": {
                    "message": "Free usage limit reached; please retry shortly.",
                    "type": "rate_limit_error",
                    "code": "429",
                }
            }
        return st, None, None, body

    while True:
        remaining_before = deadline - time.monotonic()
        if len(WORKER_POOL.workers) > len(WORKER_POOL.enabled_workers()):
            if (
                WORKER_POOL.all_enabled_unusable(exclude=tried_workers)
                and remaining_before > 3.0
            ):
                WORKER_POOL.summon_next_sync(
                    timeout=min(WORKER_POOL.summon_timeout_sec, remaining_before - 1.0),
                    exclude=tried_workers,
                )
            elif not WORKER_POOL.has_unburdened_enabled_worker(exclude=tried_workers):
                WORKER_POOL.summon_next_async()

        worker = WORKER_POOL.pick_worker(exclude=tried_workers)
        with worker._lock:
            gen_before = worker.get_gen()

        headers = make_opencode_headers(
            sig["user_agent"], sig.get("static_headers")
        )
        conn: http.client.HTTPConnection | None = None
        try:
            with worker._sem:
                worker.pace_handshake()
                if len(WORKER_POOL.workers) == 1 and worker.is_primary:
                    conn, resp = UPSTREAM.open_upstream_stream(
                        headers, payload, timeout=REQUEST_TIMEOUT_SEC
                    )
                else:
                    conn, resp = UPSTREAM.open_upstream_stream(
                        headers,
                        payload,
                        timeout=REQUEST_TIMEOUT_SEC,
                        proxy_url=worker.effective_proxy_url(),
                    )
            if resp.status == 200:
                worker.record_success()
                _register_conn_worker(conn, worker)
                return 200, conn, resp, None

            raw = resp.read().decode("utf-8", errors="replace")
            st = resp.status
            conn.close()
            try:
                body = json.loads(raw) if raw else {"error": {"message": f"HTTP {st}"}}
            except Exception:
                body = {"error": {"message": raw or f"HTTP {st}"}}
            last_st, last_body = st, body
            if is_rate_limit_error(st, body):
                saw_rate_limit = True
                rate_limit_body = body

            remaining_budget = deadline - time.monotonic()
            if remaining_budget <= 0:
                return _finish_error(st, body)

            enabled_untried = [
                w
                for w in WORKER_POOL.enabled_workers()
                if w not in tried_workers and w != worker
            ]
            has_summonable = bool(
                WORKER_POOL.dormant_workers()
                or any(w.starting for w in WORKER_POOL.workers)
            )

            if is_rate_limit_error(st, body):
                # Multi-worker failover first: move immediately to another warm enabled worker
                if multi_worker and enabled_untried:
                    tried_workers.add(worker)
                    worker.cooldown_until = time.monotonic() + 5.0
                    failovers_used += 1
                    if (
                        worker.effective_rotate_url()
                        and worker.get_active_streams() == 0
                        and not worker.rotating
                    ):
                        threading.Thread(
                            target=worker.rotate_for_generation,
                            args=(gen_before,),
                            kwargs={"grace_sec": 0.0, "allow_force": False},
                            daemon=True,
                        ).start()
                    log(
                        f"Worker {worker.name} hit 429; failing over to next worker "
                        f"({failovers_used}/{max(1, len(WORKER_POOL.enabled_workers()) - 1)})..."
                    )
                    continue

                # Summon an on-demand worker when enabled pool is exhausted or stream-protected
                if multi_worker and has_summonable and remaining_budget > 2.0:
                    tried_workers.add(worker)
                    worker.cooldown_until = time.monotonic() + 5.0
                    if (
                        worker.effective_rotate_url()
                        and worker.get_active_streams() == 0
                        and not worker.rotating
                    ):
                        threading.Thread(
                            target=worker.rotate_for_generation,
                            args=(gen_before,),
                            kwargs={"grace_sec": 0.0, "allow_force": False},
                            daemon=True,
                        ).start()
                    summoned = WORKER_POOL.summon_next_sync(
                        timeout=min(WORKER_POOL.summon_timeout_sec, remaining_budget - 1.0),
                        exclude=tried_workers,
                    )
                    if summoned is not None:
                        failovers_used += 1
                        log(
                            f"Summoned on-demand worker {summoned.name} after 429; "
                            "retrying on fresh egress..."
                        )
                        continue

                # Burst-429 backoff on a recently healthy IP before tearing down WARP
                if (
                    worker.effective_rotate_url()
                    and worker.has_recent_success()
                    and worker.can_burst_retry(gen_before)
                    and remaining_budget > 3.0
                ):
                    worker.mark_burst_retry(gen_before)
                    log(
                        f"Burst 429 on recently healthy worker {worker.name}; "
                        "backing off 1s before rotating..."
                    )
                    time.sleep(min(1.0, remaining_budget - 1.0))
                    continue

                rot_url = worker.effective_rotate_url()
                if rot_url and rotations_used < RATE_LIMIT_RETRIES:
                    allow_force = not worker.has_recent_success()
                    rotated = worker.rotate_for_generation(
                        gen_before,
                        grace_sec=STREAM_GRACE_SEC,
                        allow_force=allow_force,
                        remaining_budget=remaining_budget,
                    )
                    if rotated:
                        rotations_used += 1
                        tried_workers.discard(worker)
                        continue
                    if (
                        worker.get_active_streams() > 0
                        and remaining_budget > 2.5
                        and burst_waits < 1
                    ):
                        burst_waits += 1
                        time.sleep(min(1.5, remaining_budget - 1.0))
                        continue

            if st in (502, 503, 504):
                with worker._lock:
                    rotated_while_inflight = worker.get_gen() != gen_before
                if (
                    rotated_while_inflight
                    and inflight_rot_retries < max_inflight_rot_retries
                ):
                    inflight_rot_retries += 1
                    log(f"Upstream HTTP {st} during concurrent rotation; retrying on fresh IP...")
                    continue
                if multi_worker and enabled_untried:
                    tried_workers.add(worker)
                    worker.cooldown_until = time.monotonic() + 3.0
                    log(f"Worker {worker.name} returned HTTP {st}; failing over to next worker...")
                    continue
                if multi_worker and has_summonable and remaining_budget > 2.0:
                    tried_workers.add(worker)
                    worker.cooldown_until = time.monotonic() + 3.0
                    summoned = WORKER_POOL.summon_next_sync(
                        timeout=min(WORKER_POOL.summon_timeout_sec, remaining_budget - 1.0),
                        exclude=tried_workers,
                    )
                    if summoned is not None:
                        log(f"Summoned on-demand worker {summoned.name} after HTTP {st}; retrying...")
                        continue
                if transient_used < UPSTREAM_RETRY_COUNT:
                    transient_used += 1
                    log(f"Transient upstream HTTP {st}; retrying ({transient_used}/{UPSTREAM_RETRY_COUNT})...")
                    time.sleep(0.5)
                    continue

            return _finish_error(st, body)
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
            remaining_budget = deadline - time.monotonic()
            if remaining_budget <= 0:
                return _finish_error(last_st, last_body)
            with worker._lock:
                rotated_while_inflight = worker.get_gen() != gen_before
            if rotated_while_inflight and inflight_rot_retries < max_inflight_rot_retries:
                inflight_rot_retries += 1
                continue
            enabled_untried = [
                w
                for w in WORKER_POOL.enabled_workers()
                if w not in tried_workers and w != worker
            ]
            if multi_worker and enabled_untried:
                tried_workers.add(worker)
                worker.cooldown_until = time.monotonic() + 3.0
                continue
            if (
                multi_worker
                and (
                    WORKER_POOL.dormant_workers()
                    or any(w.starting for w in WORKER_POOL.workers)
                )
                and remaining_budget > 2.0
            ):
                tried_workers.add(worker)
                worker.cooldown_until = time.monotonic() + 3.0
                summoned = WORKER_POOL.summon_next_sync(
                    timeout=min(WORKER_POOL.summon_timeout_sec, remaining_budget - 1.0)
                )
                if summoned is not None:
                    continue
            if transient_used < UPSTREAM_RETRY_COUNT:
                transient_used += 1
                time.sleep(0.5)
                continue
            return _finish_error(last_st, last_body)

    return _finish_error(last_st, last_body)
