"""Unit tests for binary path auto-detection and configuration overrides."""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

import bridge_config as cfg


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


if __name__ == "__main__":
    unittest.main()
