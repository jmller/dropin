"""Executable upgrade, rollback, and uninstall preserve archive state and keys."""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]


class ReleaseLifecycleTest(unittest.TestCase):
    def test_program_replacement_and_removal_touch_only_the_executable(self):
        with tempfile.TemporaryDirectory(prefix="dropin-lifecycle-") as raw:
            root = Path(raw)
            target = root / "prefix/bin/dropin"
            persistent = {
                root / "config.toml": b"config sentinel",
                root / "state/store.sqlite": b"catalog sentinel",
                root / "drop/queued.txt": b"queued sentinel",
                root / "repo.password": b"password sentinel",
            }
            for path, content in persistent.items():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
            first, second = root / "dropin-a.pyz", root / "dropin-b.pyz"
            first.write_bytes(b"candidate-a")
            second.write_bytes(b"candidate-b")

            for artifact, expected in ((first, b"candidate-a"),
                                       (second, b"candidate-b"),
                                       (first, b"candidate-a")):
                result = subprocess.run([
                    sys.executable, str(ROOT / "scripts/install-user.py"),
                    "--artifact", str(artifact), "--target", str(target),
                ], cwd="/", text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(target.read_bytes(), expected)
                self.assertEqual(
                    {path: path.read_bytes() for path in persistent}, persistent)

            target.unlink()
            self.assertFalse(target.exists())
            self.assertEqual({path: path.read_bytes() for path in persistent},
                             persistent)


if __name__ == "__main__":
    unittest.main()
