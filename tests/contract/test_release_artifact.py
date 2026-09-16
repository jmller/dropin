"""Canonical release artifact reproducibility and content contract."""

from hashlib import sha256
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile


ROOT = Path(__file__).resolve().parents[2]
BUILD = ROOT / "scripts" / "build-release.py"
MANIFEST = ROOT / "packaging" / "release-files.txt"


class ReleaseArtifactTest(unittest.TestCase):
    def build(self, output: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(BUILD), "--output-dir", str(output)],
            cwd=ROOT, text=True, capture_output=True,
            env={**os.environ, "SOURCE_DATE_EPOCH": "1704067200"},
        )

    def test_manifest_explicitly_covers_runtime_sources(self):
        listed = {
            line.strip() for line in MANIFEST.read_text().splitlines()
            if line.strip() and not line.startswith("#")
        }
        runtime = {
            path.relative_to(ROOT).as_posix()
            for path in (ROOT / "dropin").rglob("*")
            if path.is_file() and path.suffix in {".py", ".sql"}
        }
        self.assertEqual(listed, {"LICENSE", "NOTICE", *runtime})
        self.assertTrue(all("*" not in item for item in listed))

    def test_two_clean_builds_are_byte_identical_and_checksums_match(self):
        with tempfile.TemporaryDirectory(prefix="dropin-release-") as raw:
            root = Path(raw)
            first, second = root / "first", root / "second"
            one, two = self.build(first), self.build(second)
            self.assertEqual(one.returncode, 0, one.stderr)
            self.assertEqual(two.returncode, 0, two.stderr)
            artifact = "dropin-0.1.0.pyz"
            first_bytes = (first / artifact).read_bytes()
            self.assertEqual(first_bytes, (second / artifact).read_bytes())
            self.assertTrue(first_bytes.startswith(b"#!/usr/bin/env python3\n"))
            expected = f"{sha256(first_bytes).hexdigest()}  {artifact}\n"
            self.assertEqual((first / "SHA256SUMS").read_text(), expected)
            self.assertEqual((second / "SHA256SUMS").read_text(), expected)

    def test_artifact_contains_only_manifest_files_and_launcher(self):
        with tempfile.TemporaryDirectory(prefix="dropin-release-") as raw:
            output = Path(raw)
            built = self.build(output)
            self.assertEqual(built.returncode, 0, built.stderr)
            artifact = output / "dropin-0.1.0.pyz"
            with zipfile.ZipFile(artifact) as archive:
                names = set(archive.namelist())
            listed = {
                line.strip() for line in MANIFEST.read_text().splitlines()
                if line.strip() and not line.startswith("#")
            }
            self.assertEqual(names, {"__main__.py", *listed})
            forbidden = ("tests/", "experiments/", "specs/", ".git/", "__pycache__")
            self.assertFalse(any(any(part in name for part in forbidden) for name in names))

            version = subprocess.run(
                [sys.executable, str(artifact), "--version"], cwd="/",
                text=True, capture_output=True,
            )
            self.assertEqual(version.returncode, 0, version.stderr)
            self.assertEqual(version.stdout, "dropin 0.1.0\n")


if __name__ == "__main__":
    unittest.main()
