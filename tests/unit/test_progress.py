"""Interactive drain progress rendering."""

from __future__ import annotations

import io
import os
import pty
import struct
import termios
import time
import unittest
from unittest import mock

from dropin.progress import (_cell_width, ProgressEvent, TerminalProgress,
                             format_progress_line)


class AdvertisedTTY:
    def isatty(self):
        return True

    def fileno(self):
        return 2


class ProgressActivationTest(unittest.TestCase):
    def test_enabled_only_for_interactive_human_non_dry_run(self):
        tty = AdvertisedTTY()
        self.assertTrue(TerminalProgress.supported(
            tty, json_output=False, dry_run=False, environ={"TERM": "xterm"}))
        self.assertFalse(TerminalProgress.supported(
            tty, json_output=True, dry_run=False, environ={"TERM": "xterm"}))
        self.assertFalse(TerminalProgress.supported(
            tty, json_output=False, dry_run=True, environ={"TERM": "xterm"}))
        self.assertFalse(TerminalProgress.supported(
            io.StringIO(), json_output=False, dry_run=False,
            environ={"TERM": "xterm"}))
        self.assertFalse(TerminalProgress.supported(
            tty, json_output=False, dry_run=False, environ={"TERM": "dumb"}))


class ProgressFormattingTest(unittest.TestCase):
    def test_line_shows_activity_phase_item_position_and_completed_count(self):
        line = format_progress_line(
            ProgressEvent("upload", "Uploading encrypted archive",
                          "report.pdf", 2, 5, completed=1), tick=3, width=80)
        self.assertIn("Uploading encrypted archive", line)
        self.assertIn("report.pdf", line)
        self.assertIn("item 2/5", line)
        self.assertIn("1/5 done", line)
        self.assertNotIn("%", line)
        self.assertLessEqual(_cell_width(line), 80)

    def test_hostile_text_is_single_line_control_safe_and_bounded(self):
        line = format_progress_line(
            ProgressEvent("capture", "Capturing\nmetadata\x1b[31m",
                          "very\tlong\rname-" + "x" * 80, 1, 1, 0),
            tick=0, width=40)
        self.assertLessEqual(_cell_width(line), 40)
        self.assertNotIn("\n", line)
        self.assertNotIn("\r", line)
        self.assertNotIn("\t", line)
        self.assertNotIn("\x1b", line)
        self.assertIn("Capturing", line)

    def test_wide_unicode_is_bounded_by_display_cells(self):
        line = format_progress_line(
            ProgressEvent("capture", "Capturing metadata", "資料" * 30, 1, 1, 0),
            tick=0, width=40)
        self.assertLessEqual(_cell_width(line), 40)

    def test_narrow_line_keeps_activity_and_phase(self):
        line = format_progress_line(
            ProgressEvent("verify", "Verifying archived bytes", "long-name",
                          1, 9, 0), tick=1, width=20)
        self.assertLessEqual(_cell_width(line), 20)
        self.assertIn("Verifying", line)


class ProgressLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.master, self.slave = pty.openpty()
        self.addCleanup(os.close, self.master)
        self.addCleanup(os.close, self.slave)
        self.stream = os.fdopen(self.slave, "w", buffering=1, closefd=False)
        self.addCleanup(self.stream.close)
        os.set_blocking(self.master, False)

    def output(self) -> str:
        chunks = []
        while True:
            try:
                chunk = os.read(self.master, 65536)
            except BlockingIOError:
                break
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks).decode(errors="replace")

    def test_context_animates_repeated_frames_and_clears_on_exit(self):
        progress = TerminalProgress(stream=self.stream, interval=0.01, width=60,
                                    color=False)
        with progress:
            progress.update(ProgressEvent(
                "upload", "Uploading encrypted archive", "notes.txt", 1, 1, 0))
            time.sleep(0.05)
            during = self.output()
            self.assertGreaterEqual(during.count("Uploading encrypted archive"), 2)
        rendered = during + self.output()
        self.assertGreaterEqual(rendered.count("\r"), 3)
        self.assertFalse(progress.running)
        self.assertTrue(os.get_blocking(self.slave))
        time.sleep(0.03)
        self.assertEqual(self.output(), "", "worker wrote after cleanup")

    def test_width_comes_from_rendering_stream_fd(self):
        import fcntl

        fcntl.ioctl(self.slave, termios.TIOCSWINSZ,
                    struct.pack("HHHH", 24, 37, 0, 0))
        progress = TerminalProgress(stream=self.stream, width=None, color=False)
        with progress:
            self.assertEqual(progress._terminal_width(), 37)

    def test_thread_start_failure_restores_fd_and_stays_inactive(self):
        progress = TerminalProgress(stream=self.stream, color=False)
        with mock.patch("dropin.progress.threading.Thread.start",
                        side_effect=RuntimeError("can't start thread")):
            with progress:
                progress.update(ProgressEvent("scan", "Scanning drop folder"))
        self.assertFalse(progress.running)
        self.assertTrue(os.get_blocking(self.slave))
        self.assertEqual(self.output(), "")

    def test_closed_fd_disables_renderer_without_raising(self):
        other_master, other_slave = pty.openpty()
        os.close(other_master)
        stream = os.fdopen(other_slave, "w", closefd=False)
        os.close(other_slave)
        progress = TerminalProgress(stream=stream, color=False)
        with progress:
            progress.update(ProgressEvent("scan", "Scanning drop folder"))
        self.assertFalse(progress.running)


if __name__ == "__main__":
    unittest.main()
