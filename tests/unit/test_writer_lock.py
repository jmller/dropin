"""The process-wide writer lock."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest

from dropin.pipeline.writer_lock import LockHeld, writer_lock

HOLDER = textwrap.dedent("""
    import json, sys, time
    sys.path.insert(0, {repo!r})
    from dropin.pipeline.writer_lock import writer_lock

    with writer_lock({path!r}, verb="drain"):
        print("held", flush=True)
        time.sleep(float(sys.argv[1]))
""")


class WriterLockTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dropin-lock-")
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)
        self.path = self.state / "writer.lock"
        self.repo = str(Path(__file__).resolve().parents[2])

    def spawn_holder(self, seconds: float = 5) -> subprocess.Popen:
        script = HOLDER.format(repo=self.repo, path=str(self.path))
        process = subprocess.Popen([sys.executable, "-c", script, str(seconds)],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True)
        self.addCleanup(self._terminate, process)
        self.assertEqual(process.stdout.readline().strip(), "held")
        return process

    def _terminate(self, process: subprocess.Popen) -> None:
        try:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)
        finally:
            process.stdout.close()
            process.stderr.close()

    def test_holder_cleanup_closes_pipes_and_is_repeatable(self):
        holder = self.spawn_holder()
        self._terminate(holder)
        self.assertIsNotNone(holder.poll())
        self.assertTrue(holder.stdout.closed)
        self.assertTrue(holder.stderr.closed)
        self._terminate(holder)

    def test_acquires_and_releases(self):
        with writer_lock(self.path, verb="drain"):
            self.assertTrue(self.path.exists())
        with writer_lock(self.path, verb="verify"):
            pass

    def test_lock_file_records_pid_verb_and_start(self):
        with writer_lock(self.path, verb="drain"):
            recorded = json.loads(self.path.read_text())
        self.assertEqual(recorded["pid"], os.getpid())
        self.assertEqual(recorded["verb"], "drain")
        self.assertIn("since", recorded)

    def test_second_holder_in_another_process_is_refused(self):
        holder = self.spawn_holder()
        with self.assertRaises(LockHeld) as caught:
            with writer_lock(self.path, verb="drain"):
                pass
        self.assertEqual(caught.exception.pid, holder.pid)
        self.assertEqual(caught.exception.verb, "drain")
        self.assertTrue(caught.exception.since)
        self.assertIn(str(holder.pid), str(caught.exception))

    def test_lock_is_released_when_the_holder_exits(self):
        holder = self.spawn_holder(seconds=0.1)
        holder.wait(timeout=10)
        with writer_lock(self.path, verb="drain"):
            pass

    def test_lock_is_released_when_the_holder_is_killed(self):
        holder = self.spawn_holder(seconds=30)
        holder.kill()
        holder.wait(timeout=10)
        deadline = time.monotonic() + 10
        while True:
            try:
                with writer_lock(self.path, verb="drain"):
                    break
            except LockHeld:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.05)

    def test_reentrant_acquisition_in_one_process_is_refused(self):
        # Two overlapping runs are a bug even inside one interpreter.
        with writer_lock(self.path, verb="drain"):
            with self.assertRaises(LockHeld):
                with writer_lock(self.path, verb="verify"):
                    pass

    def test_exception_inside_the_block_still_releases(self):
        with self.assertRaises(RuntimeError):
            with writer_lock(self.path, verb="drain"):
                raise RuntimeError("boom")
        with writer_lock(self.path, verb="drain"):
            pass

    def test_stale_lock_file_contents_do_not_block(self):
        # Contents are advisory; the flock is the authority.
        self.path.write_text(json.dumps({"pid": 999999, "verb": "drain",
                                         "since": "2020-01-01T00:00:00Z"}))
        with writer_lock(self.path, verb="drain"):
            pass

    def test_unreadable_holder_record_still_names_the_lock(self):
        holder = self.spawn_holder()
        self.path.write_text("not json")
        with self.assertRaises(LockHeld) as caught:
            with writer_lock(self.path, verb="drain"):
                pass
        self.assertIsNone(caught.exception.pid)
        self.assertIn("another dropin", str(caught.exception))
        self.assertTrue(holder.pid)
