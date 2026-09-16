"""Publication attempts: one backup, one attempt, no re-adoption."""

from __future__ import annotations

from pathlib import Path
import tempfile
import time
import unittest

from dropin.capture.extract import capture_item
from dropin.engine.fake import FakeEngine
from dropin.engine.interface import Identity
from dropin.macos.fake import FakeMacOS
from dropin.pipeline import attempts
from dropin.store import records
from dropin.store.db import connect

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "synthetic"


class AttemptTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dropin-attempts-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.drop = self.root / "drop"
        self.export_dir = self.root / "state" / "export"
        self.drop.mkdir()
        self.export_dir.mkdir(parents=True)
        self.db = connect(self.root / "state" / "store.sqlite")
        self.addCleanup(self.db.close)
        self.store_id = records.initialise_store(self.db)
        self.macos = FakeMacOS()
        self.engine = FakeEngine()
        self.engine.init()
        self.occ_id = self.record()
        # Relative to real time: attempt timestamps are wall-clock, so a clock
        # starting at an arbitrary epoch would make every backoff look infinite.
        self.clock = [time.time()]

    def record(self, name="report.pdf", content=b"payload"):
        path = self.drop / name
        path.write_bytes(content)
        self.macos.set_mdls(str(path),
                            (FIXTURES / "mdls" / "pdf_tagged.txt").read_text())
        self.macos.set_importer(
            str(path), (FIXTURES / "mdimport" / "no_text.txt").read_text())
        self.engine.add_source_file(str(path), content)
        return records.record_occurrence(self.db, capture_item(self.macos, path),
                                         self.store_id)

    def start(self, occ_id=None):
        return attempts.start(self.db, occ_id or self.occ_id, self.store_id,
                              self.export_dir)

    def publish(self, attempt, *, exit3=False, extra_snapshot=False):
        identity = Identity(store_id=self.store_id, occ_id=attempt.occ_id,
                            attempt_id=attempt.attempt_id,
                            export_seq=attempt.export_seq, kind="file",
                            catalog_sha256=attempt.catalog_sha256)
        self.engine.add_source_file(attempt.export_path, b"catalog")
        if exit3:
            self.engine.exit3_on_next_backup()
        result = self.engine.backup(
            (str(self.drop / "report.pdf"), attempt.export_path),
            identity.to_tags())
        if extra_snapshot:
            self.engine.backup((str(self.drop / "report.pdf"),),
                               identity.to_tags())
        return result


class StartTest(AttemptTestCase):
    def test_start_commits_a_pending_attempt_before_any_backup(self):
        attempt = self.start()
        row = records.get_attempt(self.db, attempt.attempt_id)
        self.assertEqual(row["outcome"], "pending")
        self.assertIsNone(row["snapshot_id"])
        # The attempt is durable before anything is published, which is what
        # makes a crash mid-backup recoverable rather than invisible.
        self.assertEqual([call[0] for call in self.engine.calls
                          if call[0] == "backup"], [])

    def test_attempt_carries_the_catalog_digest_for_its_tags(self):
        attempt = self.start()
        self.assertRegex(attempt.catalog_sha256, r"^[0-9a-f]{64}$")
        self.assertEqual(
            records.get_attempt(self.db, attempt.attempt_id)["export_sha256"],
            attempt.catalog_sha256)


class ResumeTest(AttemptTestCase):
    def test_resume_finds_the_snapshot_by_its_attempt_tag(self):
        attempt = self.start()
        published = self.publish(attempt)
        resumed = attempts.resume(self.db, self.engine, attempt.attempt_id)
        self.assertEqual(resumed.snapshot_id, published.snapshot_id)
        self.assertEqual(
            records.get_attempt(self.db, attempt.attempt_id)["snapshot_id"],
            published.snapshot_id)

    def test_resume_without_a_snapshot_fails_the_attempt_and_stays_recorded(self):
        attempt = self.start()
        resumed = attempts.resume(self.db, self.engine, attempt.attempt_id)
        self.assertIsNone(resumed.snapshot_id)
        row = records.get_attempt(self.db, attempt.attempt_id)
        self.assertEqual(row["outcome"], "failed")
        self.assertEqual(row["reason"], "no snapshot")
        self.assertEqual(records.get_occurrence(self.db, self.occ_id)["state"],
                         "recorded")

    def test_two_snapshots_for_one_attempt_fail_it_and_orphan_both(self):
        attempt = self.start()
        self.publish(attempt, extra_snapshot=True)
        resumed = attempts.resume(self.db, self.engine, attempt.attempt_id)
        self.assertIsNone(resumed.snapshot_id)
        self.assertEqual(
            records.get_attempt(self.db, attempt.attempt_id)["reason"],
            "duplicate")
        statuses = {row["status"] for row in self.db.execute(
            "SELECT status FROM snapshot WHERE attempt_id = ?",
            (attempt.attempt_id,))}
        self.assertEqual(statuses, {"orphaned"})

    def test_a_running_store_never_re_adopts_its_failed_attempt(self):
        attempt = self.start()
        published = self.publish(attempt)
        attempts.fail(self.db, attempt.attempt_id, "exit 3",
                      snapshot_id=published.snapshot_id)
        with self.assertRaises(attempts.AttemptFailed):
            attempts.resume(self.db, self.engine, attempt.attempt_id)


class FailureTest(AttemptTestCase):
    def test_failure_is_atomic_across_attempt_snapshot_and_occurrence(self):
        attempt = self.start()
        published = self.publish(attempt)
        attempts.adopt(self.db, attempt.attempt_id, published.snapshot_id,
                       self.identity(attempt))
        records.set_state(self.db, self.occ_id, "transferred")
        attempts.fail(self.db, attempt.attempt_id, "ls mismatch: sub/b.txt",
                      snapshot_id=published.snapshot_id)
        self.assertEqual(
            records.get_attempt(self.db, attempt.attempt_id)["outcome"], "failed")
        self.assertEqual(
            records.get_snapshot(self.db, published.snapshot_id)["status"],
            "orphaned")
        self.assertEqual(records.get_occurrence(self.db, self.occ_id)["state"],
                         "recorded")

    def identity(self, attempt):
        return Identity(store_id=self.store_id, occ_id=attempt.occ_id,
                        attempt_id=attempt.attempt_id,
                        export_seq=attempt.export_seq, kind="file",
                        catalog_sha256=attempt.catalog_sha256)

    def test_exit_three_fails_the_attempt(self):
        attempt = self.start()
        published = self.publish(attempt, exit3=True)
        self.assertEqual(published.exit_code, 3)
        attempts.fail(self.db, attempt.attempt_id, "exit 3",
                      snapshot_id=published.snapshot_id)
        self.assertEqual(
            records.get_attempt(self.db, attempt.attempt_id)["reason"], "exit 3")

    def test_failure_discards_the_local_export_file(self):
        attempt = self.start()
        self.assertTrue(Path(attempt.export_path).exists())
        attempts.fail(self.db, attempt.attempt_id, "no snapshot")
        self.assertFalse(Path(attempt.export_path).exists())


class SourceChangeTest(AttemptTestCase):
    def identity(self, attempt):
        return Identity(store_id=self.store_id, occ_id=attempt.occ_id,
                        attempt_id=attempt.attempt_id,
                        export_seq=attempt.export_seq, kind="file",
                        catalog_sha256=attempt.catalog_sha256)

    def test_gates_a_to_c_fail_the_pending_attempt_and_abandon(self):
        attempt = self.start()
        published = self.publish(attempt)
        attempts.adopt(self.db, attempt.attempt_id, published.snapshot_id,
                       self.identity(attempt))
        attempts.abandon_source_changed(self.db, self.occ_id,
                                        attempt.attempt_id,
                                        snapshot_id=published.snapshot_id,
                                        confirmed=False)
        self.assertEqual(
            records.get_attempt(self.db, attempt.attempt_id)["outcome"], "failed")
        self.assertEqual(
            records.get_snapshot(self.db, published.snapshot_id)["status"],
            "orphaned")
        occurrence = records.get_occurrence(self.db, self.occ_id)
        self.assertEqual(occurrence["state"], "abandoned")
        self.assertEqual(occurrence["last_error"], "source changed")

    def test_gate_d_preserves_the_confirmed_publication(self):
        attempt = self.start()
        published = self.publish(attempt)
        attempts.adopt(self.db, attempt.attempt_id, published.snapshot_id,
                       self.identity(attempt))
        records.set_state(self.db, self.occ_id, "transferred")
        records.set_state(self.db, self.occ_id, "verified")
        attempts.confirm(self.db, attempt.attempt_id, self.occ_id,
                         published.snapshot_id, attempt.export_seq)
        frontier = records.store_meta(self.db)["published_frontier"]

        attempts.abandon_source_changed(self.db, self.occ_id,
                                        attempt.attempt_id,
                                        snapshot_id=published.snapshot_id,
                                        confirmed=True)
        occurrence = records.get_occurrence(self.db, self.occ_id)
        self.assertEqual(occurrence["state"], "abandoned")
        self.assertEqual(occurrence["last_error"],
                         "source changed after publication")
        self.assertEqual(occurrence["confirmed_attempt_id"], attempt.attempt_id)
        self.assertEqual(
            records.get_attempt(self.db, attempt.attempt_id)["outcome"],
            "confirmed")
        self.assertEqual(
            records.get_snapshot(self.db, published.snapshot_id)["status"],
            "confirmed")
        self.assertEqual(records.store_meta(self.db)["published_frontier"],
                         frontier)


class ConfirmTest(AttemptTestCase):
    def test_confirmation_sets_state_attempt_snapshot_and_frontier(self):
        attempt = self.start()
        published = self.publish(attempt)
        identity = Identity(store_id=self.store_id, occ_id=self.occ_id,
                            attempt_id=attempt.attempt_id,
                            export_seq=attempt.export_seq, kind="file",
                            catalog_sha256=attempt.catalog_sha256)
        attempts.adopt(self.db, attempt.attempt_id, published.snapshot_id,
                       identity)
        records.set_state(self.db, self.occ_id, "transferred")
        records.set_state(self.db, self.occ_id, "verified")
        attempts.confirm(self.db, attempt.attempt_id, self.occ_id,
                         published.snapshot_id, attempt.export_seq)
        occurrence = records.get_occurrence(self.db, self.occ_id)
        self.assertEqual(occurrence["state"], "recoverable")
        self.assertEqual(occurrence["confirmed_attempt_id"], attempt.attempt_id)
        self.assertEqual(
            records.get_snapshot(self.db, published.snapshot_id)["status"],
            "confirmed")
        self.assertEqual(records.store_meta(self.db)["published_frontier"],
                         attempt.export_seq)


class RetryPolicyTest(AttemptTestCase):
    def now(self) -> float:
        return self.clock[0]

    def policy(self, max_attempts=3, backoff=300, retry_exhausted=False):
        return attempts.retry_state(self.db, self.occ_id,
                                    max_attempts=max_attempts,
                                    retry_backoff_seconds=backoff,
                                    now=self.now(),
                                    retry_exhausted=retry_exhausted)

    def fail_once(self):
        attempt = self.start()
        attempts.fail(self.db, attempt.attempt_id, "no snapshot")
        return attempt

    def test_first_attempt_is_ready(self):
        self.assertEqual(self.policy().decision, "ready")

    def test_backoff_defers_a_retry(self):
        self.fail_once()
        state = self.policy()
        self.assertEqual(state.decision, "deferred")
        self.assertIn("backoff", state.reason)

    def test_retry_after_the_backoff_is_ready(self):
        self.fail_once()
        self.clock[0] += 301
        self.assertEqual(self.policy().decision, "ready")

    def test_cap_exhausts_the_occurrence(self):
        for _ in range(3):
            self.fail_once()
            self.clock[0] += 301
        state = self.policy()
        self.assertEqual(state.decision, "exhausted")
        self.assertIn("attempts exhausted", state.reason)

    def test_explicit_override_authorises_exactly_one_more(self):
        for _ in range(3):
            self.fail_once()
            self.clock[0] += 301
        self.assertEqual(self.policy(retry_exhausted=True).decision, "ready")
        self.fail_once()
        self.clock[0] += 301
        # The override is per invocation, not a permanent raise of the cap.
        self.assertEqual(self.policy().decision, "exhausted")

    def test_override_still_respects_the_backoff(self):
        for _ in range(3):
            self.fail_once()
            self.clock[0] += 301
        self.fail_once()
        # The newest failure is real-time "now"; the test clock has run ahead,
        # so bring it back or the backoff would look long expired.
        self.clock[0] = time.time()
        self.assertEqual(self.policy(retry_exhausted=True).decision, "deferred")

    def test_a_confirmed_attempt_stops_the_retry_question(self):
        attempt = self.start()
        published = self.publish(attempt)
        identity = Identity(store_id=self.store_id, occ_id=self.occ_id,
                            attempt_id=attempt.attempt_id,
                            export_seq=attempt.export_seq, kind="file",
                            catalog_sha256=attempt.catalog_sha256)
        attempts.adopt(self.db, attempt.attempt_id, published.snapshot_id,
                       identity)
        records.set_state(self.db, self.occ_id, "transferred")
        records.set_state(self.db, self.occ_id, "verified")
        attempts.confirm(self.db, attempt.attempt_id, self.occ_id,
                         published.snapshot_id, attempt.export_seq)
        self.assertEqual(self.policy().decision, "confirmed")
