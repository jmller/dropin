"""Clean-prefix installation smoke tests for the canonical release artifact."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]


class ReleaseInstallTest(unittest.TestCase):
    def test_exact_canonical_artifact_installs_and_runs_outside_checkout(self):
        with tempfile.TemporaryDirectory(prefix="dropin-release-install-") as raw:
            root = Path(raw)
            built = subprocess.run(
                [sys.executable, str(ROOT / "scripts/build-release.py"),
                 "--output-dir", str(root / "assets")],
                cwd=ROOT, text=True, capture_output=True,
            )
            self.assertEqual(built.returncode, 0, built.stderr)
            artifact = root / "assets/dropin-0.1.0.pyz"
            target = root / "prefix/bin/dropin"
            installed = subprocess.run(
                [sys.executable, str(ROOT / "scripts/install-user.py"),
                 "--artifact", str(artifact), "--target", str(target)],
                cwd="/", text=True, capture_output=True,
            )
            self.assertEqual(installed.returncode, 0, installed.stderr)
            self.assertEqual(target.read_bytes(), artifact.read_bytes())
            self.assertTrue(os.access(target, os.X_OK))
            version = subprocess.run(
                [str(target), "--version"], cwd="/", text=True, capture_output=True,
            )
            self.assertEqual(version.returncode, 0, version.stderr)
            self.assertEqual(version.stdout, "dropin 0.1.0\n")

    @unittest.skipUnless(
        os.environ.get("DROPIN_RESTIC_BIN") and os.environ.get("DROPIN_RCLONE_BIN"),
        "pinned tool environment not provided",
    )
    def test_installed_candidate_initialises_private_disposable_state(self):
        with tempfile.TemporaryDirectory(prefix="dropin-release-init-") as raw:
            root = Path(raw)
            artifact_dir = root / "assets"
            built = subprocess.run([
                sys.executable, str(ROOT / "scripts/build-release.py"),
                "--output-dir", str(artifact_dir),
            ], cwd=ROOT, text=True, capture_output=True)
            self.assertEqual(built.returncode, 0, built.stderr)
            config, drop, state, repo = (
                root / "config.toml", root / "drop", root / "state", root / "repo")
            rclone_config = root / "rclone.conf"
            rclone_config.write_text("[local]\ntype = local\n")
            result = subprocess.run([
                str(artifact_dir / "dropin-0.1.0.pyz"), "--config", str(config),
                "init", "--repo", f"rclone:local:{repo}",
                "--drop-dir", str(drop), "--state-dir", str(state),
            ], cwd="/", text=True, capture_output=True, env={
                **os.environ,
                "DROPIN_RESTIC_BIN": os.environ["DROPIN_RESTIC_BIN"],
                "DROPIN_RCLONE_BIN": os.environ["DROPIN_RCLONE_BIN"],
                "RCLONE_CONFIG": str(rclone_config),
            })
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(config.stat().st_mode & 0o777, 0o600)
            self.assertEqual(config.with_name("repo.password").stat().st_mode & 0o777,
                             0o600)
            self.assertEqual(state.stat().st_mode & 0o777, 0o700)
            for name in ("export", "cache", "tmp"):
                self.assertEqual((state / name).stat().st_mode & 0o777, 0o700)

    def test_installed_candidate_reports_an_incompatible_tool_actionably(self):
        with tempfile.TemporaryDirectory(prefix="dropin-release-install-") as raw:
            root = Path(raw)
            target = root / "prefix/bin/dropin"
            installed = subprocess.run(
                [sys.executable, str(ROOT / "scripts/install-user.py"),
                 "--target", str(target)], cwd="/", text=True, capture_output=True,
            )
            self.assertEqual(installed.returncode, 0, installed.stderr)
            tools = root / "tools"
            tools.mkdir()
            restic, rclone = tools / "restic", tools / "rclone"
            restic.write_text("#!/bin/sh\necho 'restic 0.18.0 compiled'\n")
            rclone.write_text("#!/bin/sh\necho 'rclone v1.75.1'\n")
            restic.chmod(0o755)
            rclone.chmod(0o755)
            result = subprocess.run(
                [sys.executable, str(target), "--config", str(root / "config.toml"),
                 "init", "--repo", "rclone:test:/repo", "--drop-dir",
                 str(root / "drop"), "--state-dir", str(root / "state")],
                cwd="/", text=True, capture_output=True,
                env={**os.environ, "PATH": str(tools)},
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("restic 0.18.0", result.stderr)
            self.assertIn("required 0.19.1", result.stderr)


if __name__ == "__main__":
    unittest.main()
