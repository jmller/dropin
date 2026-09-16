"""Contract tests for the standalone release installer."""

from pathlib import Path
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "install-release.sh"


class ReleaseInstallerTest(unittest.TestCase):
    def test_script_is_valid_posix_shell_and_has_verified_atomic_flow(self):
        syntax = subprocess.run(["sh", "-n", str(SCRIPT)], capture_output=True,
                                text=True)
        self.assertEqual(syntax.returncode, 0, syntax.stderr)
        text = SCRIPT.read_text()
        self.assertIn("SHA256SUMS", text)
        self.assertIn("shasum -a 256 -c SHA256SUMS", text)
        self.assertIn("install -m 0755", text)
        self.assertIn("mv -f", text)
        self.assertIn("DROPIN_TARGET", text)
        self.assertIn("DROPIN_VERSION", text)
        self.assertNotIn("curl |", text)

    def test_help_is_available_without_network(self):
        result = subprocess.run(["sh", str(SCRIPT), "--help"], capture_output=True,
                                text=True)
        self.assertEqual(result.returncode, 0)
        self.assertIn("Downloads and verifies", result.stdout)


if __name__ == "__main__":
    unittest.main()
