"""Open-descriptor checks: fail closed, prove capability first.

Covers the real macOS `lsof` adapter (patched subprocess, fixture-recorded argv
and exit semantics) and the Linux `/proc` adapter, which runs for real here.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from dropin.macos.interface import Unsupported
from dropin.macos.real import RealMacOS
from dropin.ownership.linux import LinuxOwnership

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "synthetic" / "ownership"


def recording(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text())


class LsofArgvTest(unittest.TestCase):
    """The two argv forms are not interchangeable."""

    def setUp(self):
        self.adapter = RealMacOS(lsof="lsof", platform="darwin")

    def run_with(self, name: str):
        record = recording(name)
        completed = subprocess.CompletedProcess(
            record["argv"], record["returncode"],
            record["stdout"].encode(), record["stderr"].encode())
        return mock.patch("subprocess.run", return_value=completed)

    def test_file_form(self):
        with self.run_with("file_clear") as run:
            self.adapter.open_descriptors("/drop/quiet.txt", is_dir=False)
        self.assertEqual(run.call_args.args[0],
                         ["lsof", "-Fpn", "--", "/drop/quiet.txt"])

    def test_directory_form(self):
        with self.run_with("dir_clear") as run:
            self.adapter.open_descriptors("/drop/tree", is_dir=True)
        self.assertEqual(run.call_args.args[0],
                         ["lsof", "-Fpn", "+D", "/drop/tree"])

    def test_exit_one_with_no_output_is_clear(self):
        with self.run_with("file_clear"):
            self.assertEqual(
                self.adapter.open_descriptors("/drop/quiet.txt", is_dir=False), [])

    def test_exit_zero_with_a_p_record_reports_the_holder(self):
        with self.run_with("file_open"):
            self.assertEqual(
                self.adapter.open_descriptors("/drop/held.txt", is_dir=False),
                [4242])

    def test_directory_holder_is_reported(self):
        with self.run_with("dir_open"):
            self.assertEqual(
                self.adapter.open_descriptors("/drop/tree", is_dir=True), [4242])

    def test_any_stderr_is_unsupported_never_clear(self):
        # A warning on stderr means we cannot trust an empty result.
        with self.run_with("error_stderr"):
            with self.assertRaises(Unsupported):
                self.adapter.open_descriptors("/drop/x", is_dir=False)

    def test_unexpected_exit_status_is_unsupported(self):
        with self.run_with("unexpected_exit"):
            with self.assertRaises(Unsupported):
                self.adapter.open_descriptors("/drop/x", is_dir=False)

    def test_unparseable_output_is_unsupported(self):
        completed = subprocess.CompletedProcess([], 0, b"garbage without fields\n", b"")
        with mock.patch("subprocess.run", return_value=completed):
            with self.assertRaises(Unsupported):
                self.adapter.open_descriptors("/drop/x", is_dir=False)

    def test_missing_binary_is_unsupported(self):
        with mock.patch("subprocess.run", side_effect=FileNotFoundError()):
            with self.assertRaises(Unsupported):
                self.adapter.open_descriptors("/drop/x", is_dir=False)


class LsofCapabilityControlTest(unittest.TestCase):
    """Startup proves both directions before eviction may proceed.

    The adapter is forced into its macOS code path so the control logic itself
    is exercised on Linux; the platform gate has its own test below.
    """

    def setUp(self):
        self.adapter = RealMacOS(lsof="lsof", platform="darwin")

    def scripted(self, results):
        calls = iter(results)

        def run(argv, **kwargs):
            code, out, err = next(calls)
            return subprocess.CompletedProcess(argv, code, out.encode(), err.encode())

        return mock.patch("subprocess.run", side_effect=run)

    def test_positive_then_negative_control_grants_capability(self):
        pid = os.getpid()
        with self.scripted([
            (0, f"p{pid}\nn/tmp/probe\n", ""),   # file form, while open
            (0, f"p{pid}\nn/tmp/probe\n", ""),   # directory form, while open
            (1, "", ""),                          # file form, after close
            (1, "", ""),                          # directory form, after close
        ]):
            capabilities = self.adapter.capabilities()
        self.assertTrue(capabilities.ownership_check)

    def test_missing_self_visibility_is_unsupported(self):
        # If we cannot see our own open descriptor, an empty result proves nothing.
        with self.scripted([(1, "", ""), (1, "", ""), (1, "", ""), (1, "", "")]):
            capabilities = self.adapter.capabilities()
        self.assertFalse(capabilities.ownership_check)
        self.assertIn("positive control", capabilities.ownership_reason)

    def test_post_close_still_visible_is_unsupported(self):
        pid = os.getpid()
        with self.scripted([
            (0, f"p{pid}\nn/tmp/probe\n", ""),
            (0, f"p{pid}\nn/tmp/probe\n", ""),
            (0, f"p{pid}\nn/tmp/probe\n", ""),   # never cleared
            (1, "", ""),
        ]):
            capabilities = self.adapter.capabilities()
        self.assertFalse(capabilities.ownership_check)

    def test_stderr_during_the_control_is_unsupported(self):
        with self.scripted([(0, "", "lsof: WARNING\n")] * 4):
            capabilities = self.adapter.capabilities()
        self.assertFalse(capabilities.ownership_check)

    def test_capability_is_never_assumed_on_a_non_darwin_platform(self):
        capabilities = RealMacOS(lsof="lsof", platform="linux").capabilities()
        self.assertFalse(capabilities.ownership_check)
        self.assertIn("macOS-only", capabilities.ownership_reason)


@unittest.skipUnless(sys.platform == "linux", "requires Linux /proc ownership adapter")
class LinuxOwnershipTest(unittest.TestCase):
    """Runs for real: `/proc` is present in the container."""

    def setUp(self):
        self.adapter = LinuxOwnership()
        self.temp = tempfile.TemporaryDirectory(prefix="dropin-own-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_capability_probe_passes_where_proc_is_readable(self):
        capabilities = self.adapter.capabilities()
        self.assertTrue(capabilities.ownership_check, capabilities.ownership_reason)

    def test_our_own_open_file_is_detected(self):
        path = self.root / "held.txt"
        path.write_bytes(b"x")
        with path.open("rb"):
            self.assertIn(os.getpid(),
                          self.adapter.open_descriptors(str(path), is_dir=False))

    def test_closed_file_is_clear(self):
        path = self.root / "quiet.txt"
        path.write_bytes(b"x")
        self.assertEqual(self.adapter.open_descriptors(str(path), is_dir=False), [])

    def test_an_open_descendant_makes_the_directory_form_report_it(self):
        tree = self.root / "tree"
        tree.mkdir()
        inner = tree / "inner.txt"
        inner.write_bytes(b"x")
        with inner.open("rb"):
            self.assertIn(os.getpid(),
                          self.adapter.open_descriptors(str(tree), is_dir=True))

    def test_directory_form_is_clear_when_nothing_is_open(self):
        tree = self.root / "tree"
        tree.mkdir()
        (tree / "inner.txt").write_bytes(b"x")
        self.assertEqual(self.adapter.open_descriptors(str(tree), is_dir=True), [])

    def test_unreadable_proc_self_fd_is_unsupported(self):
        with mock.patch("os.listdir", side_effect=PermissionError()):
            capabilities = self.adapter.capabilities()
        self.assertFalse(capabilities.ownership_check)

    def test_missing_proc_is_unsupported(self):
        adapter = LinuxOwnership(proc_root=str(self.root / "absent-proc"))
        self.assertFalse(adapter.capabilities().ownership_check)
        with self.assertRaises(Unsupported):
            adapter.open_descriptors(str(self.root), is_dir=False)

    def test_scope_and_mmap_limits_are_stated_not_hidden(self):
        capabilities = self.adapter.capabilities()
        self.assertIn("invoking user", capabilities.scope_note)
        self.assertIn("mmap", capabilities.scope_note)

    def test_unreadable_other_process_is_skipped_not_fatal(self):
        # Another user's /proc/<pid>/fd is not readable; that must not fail the
        # check, and it is exactly why the scope note exists.
        path = self.root / "quiet.txt"
        path.write_bytes(b"x")
        self.assertEqual(self.adapter.open_descriptors(str(path), is_dir=False), [])
