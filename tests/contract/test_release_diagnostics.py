"""Release diagnostics never disclose configured credentials."""

from __future__ import annotations

import contextlib
import io
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock

from dropin.cli import emit
from dropin.mcp.server import response
from dropin.report import Outcome, Report


class ReleaseDiagnosticRedactionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dropin-diagnostics-")
        self.addCleanup(self.temp.cleanup)
        self.password = "release-password-sentinel"
        self.repository_secret = "repository-token-sentinel"
        password_file = Path(self.temp.name) / "password"
        password_file.write_text(self.password + "\n")
        password_file.chmod(0o600)
        self.config = SimpleNamespace(
            password_file=password_file,
            repo=f"https://release-user:{self.repository_secret}@archive.example/repo",
        )
        self.raw = (
            f"restic exited 1 for {self.config.repo}; backend repeated "
            f"{self.password}"
        )

    def assert_redacted(self, text: str):
        self.assertNotIn(self.password, text)
        self.assertNotIn(self.repository_secret, text)
        self.assertIn("[REDACTED]", text)
        self.assertIn("restic exited 1", text)

    def render(self, json_output: bool) -> tuple[str, str, int]:
        report = Report("drain", "release-diagnostics")
        report.run_refusal = self.raw
        report.item(Outcome.RETAINED, "safe-name.txt", reason=self.raw)
        context = SimpleNamespace(config=self.config, json_output=json_output)
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = emit(context, report)
        return stdout.getvalue(), stderr.getvalue(), code

    def test_human_diagnostics_redact_password_and_repository_credentials(self):
        stdout, stderr, code = self.render(False)
        self.assertEqual(code, 3)
        self.assertEqual(stdout, "")
        self.assert_redacted(stderr)
        self.assertIn("safe-name.txt", stderr)

    def test_ndjson_and_refusal_diagnostics_share_the_redaction_boundary(self):
        stdout, stderr, code = self.render(True)
        self.assertEqual(code, 3)
        self.assert_redacted(stdout)
        self.assert_redacted(stderr)
        self.assertTrue(stdout.rstrip().endswith("}"))

    def test_mcp_tool_payload_is_redacted_without_breaking_json_rpc(self):
        with mock.patch(
            "dropin.mcp.server.call",
            return_value=({"error": "store", "reason": self.raw}, True),
        ):
            result = response(self.config, {
                "jsonrpc": "2.0", "id": 7, "method": "tools/call",
                "params": {"name": "find", "arguments": {}},
            })
        self.assertEqual(result["jsonrpc"], "2.0")
        self.assertEqual(result["id"], 7)
        text = result["result"]["content"][0]["text"]
        self.assert_redacted(text)


if __name__ == "__main__":
    unittest.main()
