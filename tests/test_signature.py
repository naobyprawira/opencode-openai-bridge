"""Unit tests for OpenCode signature monitoring, drift warnings, and auto-adaptation."""

from __future__ import annotations

import unittest
from unittest.mock import patch

import bridge_protocol as proto
import bridge_signature as sigmod


class TestSignatureMonitor(unittest.TestCase):
    def test_extract_static_headers_skips_dynamic_and_transport(self) -> None:
        raw = {
            "Host": "127.0.0.1:9999",
            "Authorization": "Bearer public",
            "User-Agent": "opencode/latest/2.3.0/cli",
            "b3": "abc",
            "traceparent": "def",
            "x-opencode-session": "ses_123",
            "x-opencode-client": "cli",
            "x-opencode-extra": "v2",
        }
        self.assertEqual(
            sigmod.extract_static_headers(raw),
            {"x-opencode-client": "cli", "x-opencode-extra": "v2"},
        )

    def test_make_opencode_headers_merges_adapted_static_headers(self) -> None:
        hdrs = proto.make_opencode_headers(
            "opencode/latest/2.3.0/cli",
            {"x-opencode-client": "cli-v2", "x-opencode-extra": "enabled"},
        )
        self.assertEqual(hdrs["User-Agent"], "opencode/latest/2.3.0/cli")
        self.assertEqual(hdrs["x-opencode-client"], "cli-v2")
        self.assertEqual(hdrs["x-opencode-extra"], "enabled")

    def test_detect_signature_drift_flags_adapted_and_format_warnings(self) -> None:
        prev = {
            "cli_version": "opencode v2.2.7",
            "user_agent": "opencode/latest/2.2.7/cli",
            "request_path": "/v1/chat/completions",
            "header_names": sorted(sigmod._EXPECTED_HEADERS),
            "static_headers": {"x-opencode-client": "cli"},
            "body_keys": sorted(sigmod._EXPECTED_BODY_KEYS),
            "system_role": "system",
            "system_content_type": "str",
            "tool_types": ["function"],
            "system_prompt": "old prompt",
            "base_tools": [{"type": "function", "function": {"name": "read"}}],
        }
        fresh = {
            "cli_version": "opencode v2.3.0",
            "user_agent": "opencode/latest/2.3.0/cli",
            "request_path": "/v2/chat/completions",
            "header_names": sorted(sigmod._EXPECTED_HEADERS | {"x-opencode-new"}),
            "static_headers": {"x-opencode-client": "cli", "x-opencode-new": "1"},
            "body_keys": sorted(sigmod._EXPECTED_BODY_KEYS | {"metadata"}),
            "system_role": "developer",
            "system_content_type": "list",
            "tool_types": ["custom_tool"],
            "system_prompt": "new prompt",
            "base_tools": [
                {"type": "function", "function": {"name": "read"}},
                {"type": "function", "function": {"name": "bash"}},
            ],
        }

        adapted, warnings = sigmod.detect_signature_drift(prev, fresh)
        adapted_txt = " | ".join(adapted)
        warnings_txt = " | ".join(warnings)
        self.assertIn("cli_version", adapted_txt)
        self.assertIn("user_agent", adapted_txt)
        self.assertIn("static_headers", adapted_txt)
        self.assertIn("system_prompt", adapted_txt)
        self.assertIn("base_tools", adapted_txt)
        self.assertIn("endpoint path changed", warnings_txt)
        self.assertIn("request headers drifted", warnings_txt)
        self.assertIn("request body keys drifted", warnings_txt)
        self.assertIn("system message format drifted", warnings_txt)
        self.assertIn("tool schema type drifted", warnings_txt)

    def test_check_and_refresh_triggers_immediately_on_cli_version_change(self) -> None:
        mgr = sigmod.SignatureManager()
        mgr._data = {
            "captured_at": 9e18,
            "cli_version": "opencode v2.2.7",
            "user_agent": "opencode/latest/2.2.7/cli",
            "request_path": "/v1/chat/completions",
            "header_names": sorted(sigmod._EXPECTED_HEADERS),
            "static_headers": {"x-opencode-client": "cli"},
            "body_keys": sorted(sigmod._EXPECTED_BODY_KEYS),
            "system_role": "system",
            "system_content_type": "str",
            "tool_types": ["function"],
            "system_prompt": "sys",
            "base_tools": [{"type": "function", "function": {"name": "read"}}],
        }
        fresh = dict(mgr._data)
        fresh["cli_version"] = "opencode v2.3.0"
        fresh["user_agent"] = "opencode/latest/2.3.0/cli"

        with patch.object(sigmod, "SIGNATURE_MODE", "cli"), patch.object(
            mgr, "_get_cli_version", return_value="opencode v2.3.0"
        ), patch.object(mgr, "_capture", return_value=fresh) as mock_cap, patch.object(
            mgr, "_save_disk_cache"
        ), patch.object(sigmod, "log") as mock_log:
            out = mgr.check_and_refresh()

        self.assertEqual(mock_cap.call_count, 1)
        self.assertEqual(out["user_agent"], "opencode/latest/2.3.0/cli")
        logged = "\n".join(call.args[0] for call in mock_log.call_args_list)
        self.assertIn("Auto-adapted OpenCode signature", logged)

    def test_render_system_prompt_and_extract_model_request_headers(self) -> None:
        sys_txt = "Head\n${OPENCODE_TOOL_GUIDANCE}\nTail"
        sys_ts = '''
          if (tools.includes("shell")) {
            instructions.push("- Shell line 1", "- Shell line 2")
          }
          if (tools.includes("write")) {
            instructions.push("- Write line")
          }
          if (tools.includes("edit")) {
            instructions.push("- Edit line")
          }
        '''
        rendered, warnings = sigmod.render_system_prompt_from_sources(sys_txt, sys_ts)
        self.assertEqual(warnings, [])
        self.assertIn("- Shell line 1\n- Shell line 2\n- Write line\n- Edit line", rendered)

        mr_ts = '''
          const base = ai.request({
            model: model.model,
            headers: {
              "x-opencode-session-id": session.id,
              "x-session-affinity": affinity,
              "X-Session-Id": affinity,
              "User-Agent": App.useragent(app),
              "x-opencode-project": session.projectID,
              "x-opencode-session": affinity,
              "x-opencode-client": app.name,
            },
          })
        '''
        hdrs = sigmod.extract_model_request_headers(mr_ts)
        self.assertEqual(hdrs, set(sigmod._CORE_MODEL_REQUEST_HEADERS))

    def test_auto_mode_syncs_from_github_and_falls_back_to_bundled(self) -> None:
        mgr = sigmod.SignatureManager()
        with patch.object(sigmod, "SIGNATURE_MODE", "auto"), patch.object(
            mgr, "_load_disk_cache", return_value=None
        ), patch.object(mgr, "_save_disk_cache"), patch.object(
            mgr, "_fetch_latest_v2_release", return_value=("latest", "2.4.9")
        ), patch.object(
            mgr,
            "_http_get_text",
            side_effect=[
                "OpenCode\n${OPENCODE_TOOL_GUIDANCE}",
                'if (tools.includes("shell")) { instructions.push("- s") } '
                'if (tools.includes("write")) { instructions.push("- w") } '
                'if (tools.includes("edit")) { instructions.push("- e") }',
                'ai.request({ headers: { "x-opencode-client": app.name, "User-Agent": ua, "x-opencode-project": p, "x-opencode-session": s, "x-opencode-session-id": s, "x-session-affinity": s, "X-Session-Id": s } })',
            ],
        ), patch.object(sigmod, "log"):
            out = mgr.check_and_refresh()

        self.assertEqual(out["user_agent"], "opencode/latest/2.4.9/cli")
        self.assertIn("- s\n- w\n- e", out["system_prompt"])
        self.assertGreater(len(out["base_tools"]), 0)

        mgr_offline = sigmod.SignatureManager()
        with patch.object(sigmod, "SIGNATURE_MODE", "auto"), patch.object(
            mgr_offline, "_load_disk_cache", return_value=None
        ), patch.object(mgr_offline, "_save_disk_cache"), patch.object(
            mgr_offline, "_capture_github", side_effect=RuntimeError("offline")
        ), patch.object(sigmod, "log"):
            fb = mgr_offline.check_and_refresh()

        self.assertEqual(fb["source"], "bundled")
        self.assertTrue(fb["user_agent"].startswith("opencode/"))
        self.assertGreater(len(fb["base_tools"]), 0)


if __name__ == "__main__":
    unittest.main()
