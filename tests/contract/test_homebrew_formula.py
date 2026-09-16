"""Project-owned Homebrew tap formula contract."""

from hashlib import sha256
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
FORMULA = ROOT / "packaging/homebrew/Formula/dropin.rb"
PUBLIC_URL = (
    "https://github.com/jmller/dropin/releases/download/"
    "v0.1.0/dropin-0.1.0.pyz"
)


class HomebrewFormulaTest(unittest.TestCase):
    def text(self) -> str:
        return FORMULA.read_text()

    def test_formula_uses_immutable_release_url_and_exact_candidate_checksum(self):
        text = self.text()
        self.assertIn(f'url "{PUBLIC_URL}", using: :nounzip', text)
        match = re.search(r'^\s*sha256 "([0-9a-f]{64})"$', text, re.MULTILINE)
        self.assertIsNotNone(match)
        with tempfile.TemporaryDirectory(prefix="dropin-formula-") as raw:
            output = Path(raw)
            built = subprocess.run(
                [sys.executable, str(ROOT / "scripts/build-release.py"),
                 "--output-dir", str(output)], cwd=ROOT, text=True,
                capture_output=True,
                env={**os.environ, "SOURCE_DATE_EPOCH": "1704067200"},
            )
            self.assertEqual(built.returncode, 0, built.stderr)
            digest = sha256((output / "dropin-0.1.0.pyz").read_bytes()).hexdigest()
        self.assertEqual(match.group(1), digest)

    def test_formula_is_arm_only_source_available_and_uses_brew_python(self):
        text = self.text()
        self.assertIn('license :cannot_represent', text)
        self.assertIn('depends_on arch: :arm64', text)
        self.assertIn('depends_on "python@3.13"', text)
        self.assertIn('Formula["python@3.13"].opt_libexec', text)
        self.assertIn('depends_on "restic"', text)
        self.assertIn('depends_on "rclone"', text)
        self.assertIn('cached_download', text)
        self.assertIn('(bin/"dropin").write_env_script', text)
        self.assertNotRegex(text, r"\b(curl|wget)\b")

    def test_formula_test_asserts_the_public_version_string(self):
        text = self.text()
        self.assertIn('assert_equal "dropin 0.1.0", shell_output("#{bin}/dropin --version").strip', text)

    def test_candidate_validation_changes_only_the_url(self):
        text = self.text()
        candidate = text.replace(PUBLIC_URL, "http://127.0.0.1:8765/dropin-0.1.0.pyz")
        self.assertEqual(candidate.count("http://127.0.0.1:8765/dropin-0.1.0.pyz"), 1)
        self.assertEqual(candidate.replace("http://127.0.0.1:8765/dropin-0.1.0.pyz", PUBLIC_URL), text)


if __name__ == "__main__":
    unittest.main()
