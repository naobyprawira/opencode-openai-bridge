"""Unit tests for CLI flag overrides."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from opencode_bridge import cli


class TestCli(unittest.TestCase):
    def test_apply_cli_env_sets_worker_fleet_options(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            cli.apply_cli_env(
                [
                    "--standby-workers",
                    "3",
                    "--max-workers",
                    "15",
                    "--worker-idle-sec",
                    "600",
                    "--worker-prefix",
                    "warp-worker",
                    "--worker-base-port",
                    "40011",
                    "--docker-sudo",
                    "1",
                ]
            )
            self.assertEqual(os.environ["BRIDGE_WORKERS_STANDBY"], "3")
            self.assertEqual(os.environ["BRIDGE_WORKERS_MAX"], "15")
            self.assertEqual(os.environ["BRIDGE_WORKER_IDLE_SEC"], "600.0")
            self.assertEqual(os.environ["BRIDGE_WORKER_PREFIX"], "warp-worker")
            self.assertEqual(os.environ["BRIDGE_WORKER_BASE_PORT"], "40011")
            self.assertEqual(os.environ["BRIDGE_WORKER_DOCKER_SUDO"], "1")

    def test_apply_cli_env_rejects_max_below_standby(self) -> None:
        with self.assertRaises(SystemExit):
            cli.apply_cli_env(["--standby-workers", "5", "--max-workers", "3"])


if __name__ == "__main__":
    unittest.main()
