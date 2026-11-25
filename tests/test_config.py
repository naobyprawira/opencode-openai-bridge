"""Unit tests for binary path auto-detection and configuration overrides."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest.mock import patch

from opencode_bridge import config as cfg


class TestConfig(unittest.TestCase):
    def test_resolve_binary_prefers_env_override(self) -> None:
        with patch.dict(os.environ, {"BUN_BIN": "/custom/path/bun"}):
            self.assertEqual(
                cfg.resolve_binary("BUN_BIN", "bun", ["/usr/bin/bun"]),
                "/custom/path/bun",
            )

    def test_resolve_binary_detects_executable_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            fake_bin = os.path.join(td, "opencode")
            with open(fake_bin, "w", encoding="utf-8") as f:
                f.write("#!/bin/sh\n")
            os.chmod(fake_bin, 0o755)
            with patch.dict(os.environ, {}, clear=True):
                self.assertEqual(
                    cfg.resolve_binary("OPENCODE_BIN", "opencode", ["/nonexistent/opencode", fake_bin]),
                    fake_bin,
                )

    def test_resolve_binary_falls_back_to_which(self) -> None:
        with patch.dict(os.environ, {}, clear=True), patch(
            "shutil.which", return_value="/opt/bin/bun"
        ):
            self.assertEqual(
                cfg.resolve_binary("BUN_BIN", "bun", ["/nonexistent/bun"]),
                "/opt/bin/bun",
            )

    def test_resolve_port_prefers_bridge_port_then_paas_port_then_default(self) -> None:
        with patch.dict(os.environ, {"BRIDGE_PORT": "9000", "PORT": "10000"}, clear=True):
            self.assertEqual(cfg.resolve_port(("BRIDGE_PORT", "PORT"), 29471), 9000)
        with patch.dict(os.environ, {"PORT": "10000"}, clear=True):
            self.assertEqual(cfg.resolve_port(("BRIDGE_PORT", "PORT"), 29471), 10000)
        with patch.dict(os.environ, {"BRIDGE_PORT": "", "PORT": ""}, clear=True):
            self.assertEqual(cfg.resolve_port(("BRIDGE_PORT", "PORT"), 29471), 29471)

    def test_default_signature_check_interval_is_24h(self) -> None:
        if not os.environ.get("BRIDGE_SIGNATURE_CHECK_INTERVAL"):
            self.assertEqual(cfg.SIGNATURE_CHECK_INTERVAL_SEC, 86400.0)

    def test_parse_upstream_workers_csv_json_and_fallback(self) -> None:
        csv_workers = cfg.parse_upstream_workers(
            "http://127.0.0.1:40001, http://127.0.0.1:40011",
            fallback_token="tok",
        )
        self.assertEqual(
            csv_workers,
            [
                {
                    "proxy": "http://127.0.0.1:40001",
                    "rotate": "http://127.0.0.1:40001/_rotate",
                    "token": "tok",
                },
                {
                    "proxy": "http://127.0.0.1:40011",
                    "rotate": "http://127.0.0.1:40011/_rotate",
                    "token": "tok",
                },
            ],
        )

        json_workers = cfg.parse_upstream_workers(
            '[{"proxy":"http://127.0.0.1:40001","rotate":"http://127.0.0.1:40001/_rotate","token":"a"}]'
        )
        self.assertEqual(
            json_workers,
            [
                {
                    "proxy": "http://127.0.0.1:40001",
                    "rotate": "http://127.0.0.1:40001/_rotate",
                    "token": "a",
                }
            ],
        )

        fb = cfg.parse_upstream_workers(
            "",
            fallback_proxy="http://127.0.0.1:40001",
            fallback_rotate="http://127.0.0.1:40001/_rotate",
            fallback_token="sec",
        )
        self.assertEqual(
            fb,
            [
                {
                    "proxy": "http://127.0.0.1:40001",
                    "rotate": "http://127.0.0.1:40001/_rotate",
                    "token": "sec",
                }
            ],
        )

    def test_parse_upstream_workers_fleet_metadata_and_generation(self) -> None:
        raw = json.dumps(
            [
                {
                    "proxy": "http://127.0.0.1:40011",
                    "container": "warp-worker-1",
                    "on_demand": False,
                },
                {
                    "proxy": "http://127.0.0.1:40012",
                    "container": "warp-worker-2",
                    "on_demand": True,
                },
                {
                    "proxy": "http://127.0.0.1:40013",
                    "container": "warp-worker-3",
                    "on_demand": True,
                },
            ]
        )
        capped = cfg.parse_upstream_workers(raw, max_workers=2)
        self.assertEqual(len(capped), 2)
        self.assertEqual(capped[1]["container"], "warp-worker-2")
        self.assertTrue(capped[1]["on_demand"])

        generated = cfg.parse_upstream_workers(
            "",
            worker_prefix="warp-worker",
            worker_base_port=40011,
            standby_workers=3,
            max_workers=5,
        )
        self.assertEqual(len(generated), 5)
        self.assertEqual(generated[0]["container"], "warp-worker-1")
        self.assertFalse(generated[2]["on_demand"])
        self.assertTrue(generated[3]["on_demand"])
        self.assertEqual(generated[4]["proxy"], "http://127.0.0.1:40015")


if __name__ == "__main__":
    unittest.main()
