"""CLI entrypoint and runtime flag overrides for opencode-openai-bridge."""

from __future__ import annotations

import argparse
import os
import sys
from typing import Sequence


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="opencode-openai-bridge",
        description="OpenAI-compatible HTTP bridge to OpenCode free inference API.",
    )
    parser.add_argument("--host", default=None, help="Bridge listen host (env: BRIDGE_HOST)")
    parser.add_argument("--port", type=int, default=None, help="Bridge listen port (env: BRIDGE_PORT)")
    parser.add_argument(
        "--upstream-port",
        type=int,
        default=None,
        help="Local Bun forwarder port (env: UPSTREAM_PORT)",
    )
    parser.add_argument(
        "--signature-mode",
        choices=("auto", "github", "cli", "static"),
        default=None,
        help="Signature tracking mode (env: BRIDGE_SIGNATURE_MODE)",
    )
    parser.add_argument(
        "--workers",
        default=None,
        help="Upstream worker pool CSV or JSON (env: BRIDGE_UPSTREAM_WORKERS)",
    )
    parser.add_argument(
        "--standby-workers",
        type=int,
        default=None,
        help="Always-on standby WARP workers (env: BRIDGE_WORKERS_STANDBY)",
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        default=None,
        help="Max WARP workers including summon-on-event (env: BRIDGE_WORKERS_MAX)",
    )
    parser.add_argument(
        "--worker-idle-sec",
        type=float,
        default=None,
        help="Seconds before idle on-demand workers are stopped; 0 disables (env: BRIDGE_WORKER_IDLE_SEC)",
    )
    parser.add_argument(
        "--summon-timeout-sec",
        type=float,
        default=None,
        help="Max seconds to wait for an on-demand worker boot (env: BRIDGE_WORKER_SUMMON_TIMEOUT_SEC)",
    )
    parser.add_argument(
        "--worker-prefix",
        default=None,
        help="Docker container name prefix for auto-generated workers (env: BRIDGE_WORKER_PREFIX)",
    )
    parser.add_argument(
        "--worker-base-port",
        type=int,
        default=None,
        help="Base localhost port for auto-generated workers (env: BRIDGE_WORKER_BASE_PORT)",
    )
    parser.add_argument(
        "--docker-sudo",
        choices=("auto", "1", "0"),
        default=None,
        help="Use sudo -n for docker commands (env: BRIDGE_WORKER_DOCKER_SUDO)",
    )
    return parser


def apply_cli_env(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else sys.argv[1:])
    if args.standby_workers is not None and args.standby_workers < 1:
        parser.error("--standby-workers must be >= 1")
    if args.max_workers is not None and args.max_workers < 1:
        parser.error("--max-workers must be >= 1")
    if (
        args.standby_workers is not None
        and args.max_workers is not None
        and args.max_workers < args.standby_workers
    ):
        parser.error("--max-workers must be >= --standby-workers")

    mapping = {
        "host": "BRIDGE_HOST",
        "port": "BRIDGE_PORT",
        "upstream_port": "UPSTREAM_PORT",
        "signature_mode": "BRIDGE_SIGNATURE_MODE",
        "workers": "BRIDGE_UPSTREAM_WORKERS",
        "standby_workers": "BRIDGE_WORKERS_STANDBY",
        "max_workers": "BRIDGE_WORKERS_MAX",
        "worker_idle_sec": "BRIDGE_WORKER_IDLE_SEC",
        "summon_timeout_sec": "BRIDGE_WORKER_SUMMON_TIMEOUT_SEC",
        "worker_prefix": "BRIDGE_WORKER_PREFIX",
        "worker_base_port": "BRIDGE_WORKER_BASE_PORT",
        "docker_sudo": "BRIDGE_WORKER_DOCKER_SUDO",
    }
    for attr, env_name in mapping.items():
        val = getattr(args, attr)
        if val is not None:
            os.environ[env_name] = str(val)
    return args


def main(argv: Sequence[str] | None = None) -> None:
    apply_cli_env(argv)
    from opencode_bridge.server import main as server_main

    server_main()
