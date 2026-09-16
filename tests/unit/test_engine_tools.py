"""The startup tool gate: presence and minimum versions."""

from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from dropin.config import load
from dropin.engine.tools import ToolGateError, check_tools, parse_version

CONFIG = """
[paths]
drop_dir  = "{drop}"
state_dir = "{state}"
[repository]
repo          = "rclone:archive:/dropin"
password_file = "{password}"
[tools]
restic = "{restic}"
rclone = "{rclone}"
restic_min = "0.19.1"
rclone_min = "1.75.1"
rclone_connections = 2
timeout_seconds = 3600
pack_size_mb = 16
cache_max_mb = 2048
[drain]
settle_seconds = 5
sample_gap_seconds = 2
max_attempts = 3
retry_backoff_seconds = 300
[ownership]
lsof = "lsof"
[launchd]
label = "dev.dropin.drain"
interval = 900
"""

RESTIC_OUT = "restic 0.19.1 compiled with go1.26.4 on linux/arm64\n"
RCLONE_OUT = ("rclone v1.75.1\n- os/version: debian 12.15 (64 bit)\n"
              "- os/kernel: 6.8.0 (aarch64)\n")


class VersionParsingTest(unittest.TestCase):
    def test_restic_version_line(self):
        self.assertEqual(parse_version("restic", RESTIC_OUT), (0, 19, 1))

    def test_rclone_version_line(self):
        self.assertEqual(parse_version("rclone", RCLONE_OUT), (1, 75, 1))

    def test_two_component_version(self):
        self.assertEqual(parse_version("rclone", "rclone v1.75\n"), (1, 75, 0))

    def test_unparseable_output_raises(self):
        for text in ("", "not a version\n", "restic\n"):
            with self.subTest(text=text):
                with self.assertRaises(ToolGateError):
                    parse_version("restic", text)


class ToolGateTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dropin-tools-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in ("drop", "state"):
            (self.root / name).mkdir()
        self.password = self.root / "pw"
        self.password.write_text("x")
        self.password.chmod(0o600)
        self.restic = self.root / "restic"
        self.rclone = self.root / "rclone"
        for path in (self.restic, self.rclone):
            path.write_text("#!/bin/sh\n")
            path.chmod(0o755)

    def config(self, restic=None, rclone=None):
        path = self.root / "config.toml"
        path.write_text(CONFIG.format(
            drop=self.root / "drop", state=self.root / "state",
            password=self.password, restic=restic or self.restic,
            rclone=rclone or self.rclone))
        return load(path)

    def patched(self, restic_out=RESTIC_OUT, rclone_out=RCLONE_OUT):
        def run(argv, **kwargs):
            text = restic_out if "restic" in str(argv[0]) else rclone_out
            return subprocess.CompletedProcess(argv, 0, text.encode(), b"")

        return mock.patch("subprocess.run", side_effect=run)


class ToolGateTest(ToolGateTestCase):
    def test_pinned_versions_pass(self):
        with self.patched():
            versions = check_tools(self.config())
        self.assertEqual(versions.restic, (0, 19, 1))
        self.assertEqual(versions.rclone, (1, 75, 1))

    def test_newer_versions_pass(self):
        with self.patched(restic_out="restic 0.20.0 compiled\n",
                          rclone_out="rclone v1.76.0\n"):
            check_tools(self.config())

    def test_below_pin_lists_found_and_required(self):
        with self.patched(restic_out="restic 0.19.0 compiled\n"):
            with self.assertRaises(ToolGateError) as caught:
                check_tools(self.config())
        message = str(caught.exception)
        self.assertIn("0.19.0", message)
        self.assertIn("0.19.1", message)
        self.assertIn("restic", message)

    def test_below_pin_on_rclone_is_reported_too(self):
        with self.patched(rclone_out="rclone v1.74.0\n"):
            with self.assertRaises(ToolGateError) as caught:
                check_tools(self.config())
        self.assertIn("rclone", str(caught.exception))

    def test_missing_binary_names_it(self):
        config = self.config(restic=self.root / "absent-restic")
        with mock.patch("subprocess.run", side_effect=FileNotFoundError()):
            with self.assertRaises(ToolGateError) as caught:
                check_tools(config)
        self.assertIn("restic", str(caught.exception))

    def test_bare_name_is_resolved_on_path(self):
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        for name in ("restic", "rclone"):
            target = bin_dir / name
            target.write_text("#!/bin/sh\n")
            target.chmod(0o755)
        config = self.config(restic="restic", rclone="rclone")
        with mock.patch.dict("os.environ", {"PATH": str(bin_dir)}):
            with self.patched():
                versions = check_tools(config)
        self.assertEqual(versions.restic_path, str(bin_dir / "restic"))

    def test_configured_absolute_path_wins_over_path_lookup(self):
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        decoy = bin_dir / "restic"
        decoy.write_text("#!/bin/sh\n")
        decoy.chmod(0o755)
        with mock.patch.dict("os.environ", {"PATH": str(bin_dir)}):
            with self.patched():
                versions = check_tools(self.config())
        self.assertEqual(versions.restic_path, str(self.restic))

    def test_nonzero_exit_from_version_is_a_gate_error(self):
        def run(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 1, b"", b"broken")

        with mock.patch("subprocess.run", side_effect=run):
            with self.assertRaises(ToolGateError):
                check_tools(self.config())
