"""Contracts for the side-effect-free shell prerequisite checker."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "install-prerequisites.sh"


class PrerequisiteInstallerTest(unittest.TestCase):
    def write_tool(self, directory: Path, name: str, output: str,
                   exit_code: int = 0) -> None:
        path = directory / name
        path.write_text(
            f"#!/bin/sh\nprintf '%s\\n' '{output}'\nexit {exit_code}\n"
        )
        path.chmod(0o755)

    def environment(self, directory: Path) -> dict[str, str]:
        (directory / "python3").symlink_to(sys.executable)
        return {
            **os.environ,
            "PATH": f"{directory}:/usr/bin:/bin",
            "HOME": str(directory.parent),
        }

    def run_check(self, tools: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(SCRIPT)], text=True, capture_output=True,
            env=self.environment(tools), cwd=tools.parent,
        )

    def test_script_is_executable_posix_shell(self):
        self.assertTrue(os.access(SCRIPT, os.X_OK))
        result = subprocess.run(["/bin/sh", "-n", str(SCRIPT)],
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_check_accepts_compatible_tools_without_writing(self):
        with tempfile.TemporaryDirectory(prefix="dropin-prerequisites-") as raw:
            root = Path(raw)
            tools = root / "path"
            tools.mkdir()
            self.write_tool(tools, "restic", "restic 0.19.1 compiled with go")
            self.write_tool(tools, "rclone", "rclone v1.75.1")
            before = sorted(path.name for path in root.iterdir())
            result = self.run_check(tools)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("OK       python3", result.stdout)
            self.assertIn("OK       restic 0.19.1", result.stdout)
            self.assertIn("OK       rclone 1.75.1", result.stdout)
            self.assertEqual(sorted(path.name for path in root.iterdir()), before)

    def test_check_reports_outdated_and_missing_tools(self):
        with tempfile.TemporaryDirectory(prefix="dropin-prerequisites-") as raw:
            tools = Path(raw) / "path"
            tools.mkdir()
            self.write_tool(tools, "restic", "restic 0.18.0 compiled with go")
            self.write_tool(tools, "rclone", "not rclone", exit_code=127)
            result = self.run_check(tools)
            self.assertEqual(result.returncode, 1)
            self.assertIn("OUTDATED restic 0.18.0", result.stderr)
            self.assertIn("INVALID  rclone", result.stderr)
            self.assertIn("brew install python@3.11 restic rclone", result.stderr)

    def test_malformed_version_and_nonzero_process_are_rejected(self):
        cases = (
            ("restic", "restic 999", 0),
            ("restic", "restic 99.0.0", 1),
            ("rclone", "rclone v999", 0),
            ("rclone", "rclone v99.0.0", 1),
        )
        for broken, output, exit_code in cases:
            with self.subTest(tool=broken, output=output, exit_code=exit_code), \
                 tempfile.TemporaryDirectory(prefix="dropin-prerequisites-") as raw:
                tools = Path(raw) / "path"
                tools.mkdir()
                self.write_tool(tools, "restic", "restic 0.19.1 compiled with go")
                self.write_tool(tools, "rclone", "rclone v1.75.1")
                self.write_tool(tools, broken, output, exit_code)
                result = self.run_check(tools)
                self.assertEqual(result.returncode, 1)
                self.assertIn(f"INVALID  {broken}", result.stderr)

    def test_unknown_argument_is_usage_error(self):
        result = subprocess.run([str(SCRIPT), "--install"], text=True,
                                capture_output=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("Unknown argument", result.stderr)


if __name__ == "__main__":
    unittest.main()
