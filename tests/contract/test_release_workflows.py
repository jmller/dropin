"""Static contract for credential-minimal, pinned release workflows."""

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"


class ReleaseWorkflowTest(unittest.TestCase):
    def read(self, name: str) -> str:
        return (WORKFLOWS / name).read_text()

    def assert_actions_are_commit_pinned(self, text: str) -> None:
        actions = re.findall(r"uses:\s*([^\s#]+)", text)
        self.assertTrue(actions)
        for action in actions:
            with self.subTest(action=action):
                self.assertRegex(action, r"^[^@]+@[0-9a-f]{40}$")

    def test_ci_has_separate_core_and_real_tool_jobs(self):
        text = self.read("ci.yml")
        self.assertIn("pull_request:", text)
        self.assertIn("push:", text)
        self.assertRegex(text, r"(?m)^\s{2}core-tests:")
        self.assertRegex(text, r"(?m)^\s{2}real-tool-integration:")
        self.assertIn("make test", text)
        self.assertIn("make test-integration", text)
        self.assertIn("make probe", text)
        self.assertIn("PYTHONWARNINGS: error::ResourceWarning", text)
        self.assertIn("scripts/release_toolchain.py", text)
        self.assertIn("DROPIN_RESTIC_BIN", text)
        self.assertIn("DROPIN_RCLONE_BIN", text)
        self.assertIn("GITHUB_PATH", text)
        self.assertNotIn("cache: pip", text)
        self.assertNotIn("continue-on-error: true", text)
        self.assert_actions_are_commit_pinned(text)

    def test_ci_explicitly_reports_mac_only_tests_without_calling_them_passed(self):
        text = self.read("ci.yml")
        self.assertIn("macos-release-gate", text)
        self.assertIn("not executed on Linux", text)
        self.assertIn("does not satisfy the release gate", text)

    def test_release_workflow_builds_canonical_assets_but_cannot_publish(self):
        text = self.read("release.yml")
        self.assertRegex(text, r"(?s)tags:.*v\[0-9\]")
        self.assertIn("scripts/build-release.py", text)
        self.assertIn("dropin-0.1.0.pyz", text)
        self.assertIn("SHA256SUMS", text)
        self.assertIn("permissions:\n  contents: read", text)
        for forbidden in ("contents: write", "secrets.", "GH_TOKEN", "gh release", "softprops/action-gh-release"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, text)
        self.assert_actions_are_commit_pinned(text)


if __name__ == "__main__":
    unittest.main()
