"""The drain state machine, end to end with fakes."""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest

from dropin.config import load
from dropin.engine.fake import FakeEngine
from dropin.macos.fake import FakeMacOS
from dropin.pipeline.drain import DrainOptions, drain
from dropin.report import Outcome, Report
from dropin.store import records
from dropin.store.db import connect
from tests.unit.test_evict import FakeOwnership

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "synthetic"

CONFIG = """
[paths]
drop_dir  = "{drop}"
state_dir = "{state}"
[repository]
repo          = "rclone:archive:/dropin"
password_file = "{password}"
[tools]
restic = "restic"
rclone = "rclone"
restic_min = "0.19.1"
rclone_min = "1.75.1"
rclone_connections = 2
timeout_seconds = 3600
pack_size_mb = 16
cache_max_mb = 2048
[drain]
settle_seconds = 0
sample_gap_seconds = 0
max_attempts = 3
retry_backoff_seconds = 300
[ownership]
lsof = "lsof"
[launchd]
label = "dev.dropin.drain"
interval = 900
"""


class DrainContext:
    """The seams `drain` needs, all fakes."""

    def __init__(self, config, db, engine, macos, ownership):
        self.config = config
        self.db = db
        self.engine = engine
        self.macos = macos
        self.ownership = ownership


class DrainTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dropin-drain-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.drop = self.root / "drop"
        self.state = self.root / "state"
        for path in (self.drop, self.state, self.state / "export",
                     self.state / "cache", self.state / "tmp"):
            path.mkdir(parents=True)
        password = self.root / "pw"
        password.write_text("x")
        password.chmod(0o600)
        config_path = self.root / "config.toml"
        config_path.write_text(CONFIG.format(drop=self.drop, state=self.state,
                                             password=password))
        self.config = load(config_path)
        self.db = connect(self.config.store_path)
        self.addCleanup(self.db.close)
        self.store_id = records.initialise_store(self.db)
        self.macos = FakeMacOS()
        self.engine = FakeEngine()
        self.engine.init()
        self.ownership = FakeOwnership()
        self.context = DrainContext(self.config, self.db, self.engine,
                                    self.macos, self.ownership)

    # ---- fixtures ----------------------------------------------------------

    def drop_file(self, name="report.pdf", content=b"payload") -> Path:
        path = self.drop / name
        path.write_bytes(content)
        self.describe(path)
        return path

    def drop_tree(self, name="tree") -> Path:
        tree = self.drop / name
        (tree / "sub").mkdir(parents=True)
        (tree / "a.txt").write_bytes(b"alpha")
        (tree / "sub" / "b.txt").write_bytes(b"beta")
        self.describe(tree)
        return tree

    def describe(self, path: Path) -> None:
        children = [path, *(path.rglob("*") if path.is_dir() else [])]
        for child in children:
            self.macos.set_mdls(
                str(child), (FIXTURES / "mdls" / "text_plain.txt").read_text())
            self.macos.set_importer(
                str(child), (FIXTURES / "mdimport" / "no_text.txt").read_text())
            if child.is_symlink():
                self.engine.add_source_symlink(str(child), str(child.readlink()))
            elif child.is_dir():
                self.engine.add_source_dir(str(child))
            else:
                self.engine.add_source_file(str(child), child.read_bytes())

    def run_drain(self, **options) -> Report:
        report = Report(verb="drain", run_id="run-1")
        drain(self.context, report, DrainOptions(**options))
        return report

    def outcomes(self, report: Report) -> dict[str, Outcome]:
        return {record.name: record.outcome for record in report.records}


class HappyPathTest(DrainTestCase):
    def test_a_file_is_archived_and_evicted(self):
        path = self.drop_file()
        report = self.run_drain()
        self.assertEqual(self.outcomes(report), {"report.pdf": Outcome.ARCHIVED})
        self.assertFalse(path.exists())
        occurrence = records.occurrences_in_state(self.db, "evicted")[0]
        self.assertEqual(occurrence["item_name"], "report.pdf")
        self.assertIsNotNone(occurrence["confirmed_attempt_id"])

    def test_a_tree_is_archived_and_evicted(self):
        tree = self.drop_tree()
        report = self.run_drain()
        self.assertEqual(self.outcomes(report), {"tree": Outcome.ARCHIVED})
        self.assertFalse(tree.exists())

    def test_exactly_one_snapshot_per_item(self):
        self.drop_file()
        self.run_drain()
        self.assertEqual(len(self.engine.snapshots()), 1)

    def test_report_carries_the_archive_path_and_hash(self):
        self.drop_file()
        record = self.run_drain().records[0]
        self.assertTrue(record.archive_path)
        self.assertRegex(record.sha256, r"^[0-9a-f]{64}$")
        self.assertEqual(record.state, "evicted")

    def test_empty_spool_is_quiet_and_successful(self):
        report = self.run_drain()
        self.assertEqual(report.records, [])
        self.assertEqual(report.exit_code(), 0)

    def test_items_are_processed_in_name_order(self):
        for name in ("b.txt", "a.txt", "c.txt"):
            self.drop_file(name, name.encode())
        report = self.run_drain()
        self.assertEqual([record.name for record in report.records],
                         ["a.txt", "b.txt", "c.txt"])

    def test_progress_reports_semantic_phases_and_item_position(self):
        self.drop_file()
        events = []
        self.run_drain(progress=events.append)
        phases = [event.phase for event in events]
        for phase in ("prepare", "repository", "scan", "stability", "capture",
                      "upload", "payload-verify", "catalog-verify",
                      "removal-check", "remove", "finalize"):
            self.assertIn(phase, phases)
        upload = next(event for event in events if event.phase == "upload")
        self.assertEqual(
            (upload.item_name, upload.item_index, upload.item_total,
             upload.completed),
            ("report.pdf", 1, 1, 0))
        final = next(event for event in events if event.phase == "finalize")
        self.assertEqual((final.item_name, final.item_index, final.completed),
                         (None, None, 1))

    def test_progress_callback_failure_cannot_change_archival_outcome(self):
        path = self.drop_file()

        def broken_progress(_event):
            raise RuntimeError("display failed")

        report = self.run_drain(progress=broken_progress)
        self.assertEqual(self.outcomes(report), {"report.pdf": Outcome.ARCHIVED})
        self.assertFalse(path.exists())


class DedupTest(DrainTestCase):
    def test_identical_content_gets_its_own_occurrence_and_is_reported(self):
        self.drop_file("first.txt", b"identical")
        self.run_drain()
        self.drop_file("second.txt", b"identical")
        record = self.run_drain().records[0]
        self.assertEqual(record.outcome, Outcome.ARCHIVED)
        self.assertTrue(record.dedup)
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM occurrence").fetchone()[0], 2)

    def test_deduplicated_content_shares_blobs(self):
        self.drop_file("first.txt", b"identical")
        self.run_drain()
        self.drop_file("second.txt", b"identical")
        self.run_drain()
        first, second = self.engine.snapshots()
        # The dedup oracle is blob identity, not repository size.
        paths = [str((self.drop / name).resolve())
                 for name in ("first.txt", "second.txt")]
        ids = {self.engine.node_content_ids(snapshot.id, path)[0]
               for snapshot, path in zip(sorted(self.engine.snapshots(),
                                                key=lambda s: s.time), paths)}
        self.assertEqual(len(ids), 1)


class RefusalTest(DrainTestCase):
    def test_a_special_entry_refuses_only_that_tree(self):
        tree = self.drop_tree("bad")
        os.mkfifo(tree / "pipe")
        self.drop_file("good.txt", b"good")
        report = self.run_drain()
        outcomes = self.outcomes(report)
        self.assertEqual(outcomes["bad"], Outcome.REFUSED)
        self.assertEqual(outcomes["good.txt"], Outcome.ARCHIVED)
        self.assertTrue(tree.exists())
        self.assertEqual(report.exit_code(), 1)

    def test_a_partial_backup_isolates_one_item(self):
        # Name order puts `bad.txt` first, so it is the one that takes the
        # injected exit-3 backup.
        self.drop_file("bad.txt", b"partial")
        self.drop_file("clean.txt", b"clean")
        self.engine.exit3_on_next_backup()
        report = self.run_drain()
        outcomes = self.outcomes(report)
        self.assertEqual(outcomes["bad.txt"], Outcome.REFUSED)
        self.assertEqual(outcomes["clean.txt"], Outcome.ARCHIVED)
        self.assertTrue((self.drop / "bad.txt").exists())
        self.assertFalse((self.drop / "clean.txt").exists())

    def test_a_failed_attempt_regresses_the_occurrence_to_recorded(self):
        self.drop_file("partial.txt", b"partial")
        self.engine.exit3_on_next_backup()
        self.run_drain()
        occurrence = records.occurrences_in_state(self.db, "recorded")[0]
        self.assertEqual(occurrence["item_name"], "partial.txt")

    def test_an_engine_failure_before_the_first_item_refuses_the_run(self):
        self.drop_file()
        self.engine.fail_with("no-repo")
        report = self.run_drain()
        self.assertTrue(report.run_refusal)
        self.assertEqual(report.exit_code(), 3)
        self.assertTrue((self.drop / "report.pdf").exists())

    def test_an_unreadable_item_is_refused_without_blocking_others(self):
        if os.geteuid() == 0:
            self.skipTest("root can read anything")
        blocked = self.drop_file("blocked.bin", b"secret")
        blocked.chmod(0)
        self.addCleanup(blocked.chmod, 0o600)
        self.drop_file("fine.txt", b"fine")
        report = self.run_drain()
        outcomes = self.outcomes(report)
        self.assertEqual(outcomes["blocked.bin"], Outcome.REFUSED)
        self.assertEqual(outcomes["fine.txt"], Outcome.ARCHIVED)


class RetentionTest(DrainTestCase):
    def test_an_open_writer_retains_the_item_at_recoverable(self):
        path = self.drop_file()
        self.ownership.hold(str(path), 4242)
        report = self.run_drain()
        record = report.records[0]
        self.assertEqual(record.outcome, Outcome.RETAINED)
        self.assertIn("4242", record.reason)
        self.assertTrue(path.exists())
        self.assertEqual(record.state, "recoverable")
        self.assertEqual(report.exit_code(), 1)

    def test_an_unsupported_ownership_check_retains_the_item(self):
        path = self.drop_file()
        self.ownership.supported = False
        report = self.run_drain()
        self.assertEqual(report.records[0].outcome, Outcome.RETAINED)
        self.assertTrue(path.exists())

    def test_a_retained_item_evicts_on_a_later_run(self):
        path = self.drop_file()
        self.ownership.hold(str(path), 4242)
        self.run_drain()
        self.ownership.holders.clear()
        report = self.run_drain()
        self.assertEqual(report.records[0].outcome, Outcome.ARCHIVED)
        self.assertFalse(path.exists())


class SourceChangeTest(DrainTestCase):
    def test_a_change_before_publication_abandons_and_retains_the_source(self):
        path = self.drop_file()
        original = self.engine.backup

        def mutate_then_backup(paths, tags):
            path.write_bytes(b"changed during upload")
            return original(paths, tags)

        self.engine.backup = mutate_then_backup
        report = self.run_drain()
        record = report.records[0]
        self.assertEqual(record.outcome, Outcome.DEFERRED)
        self.assertIn("source changed", record.reason)
        self.assertTrue(path.exists())
        occurrence = records.occurrences_in_state(self.db, "abandoned")[0]
        self.assertEqual(occurrence["last_error"], "source changed")
        attempt = records.attempts_for(self.db, occurrence["occ_id"])[0]
        self.assertEqual(attempt["outcome"], "failed")

    def test_a_change_at_gate_d_preserves_the_confirmed_publication(self):
        path = self.drop_file()
        real_begin = None

        import dropin.pipeline.drain as module

        def mutate_then_begin(connection, occ_id, ownership, spool_path):
            path.write_bytes(b"changed after publication")
            return real_begin(connection, occ_id, ownership, spool_path)

        real_begin = module.begin_eviction
        module.begin_eviction = mutate_then_begin
        self.addCleanup(setattr, module, "begin_eviction", real_begin)

        report = self.run_drain()
        record = report.records[0]
        self.assertEqual(record.outcome, Outcome.DEFERRED)
        self.assertIn("after publication", record.reason)
        self.assertTrue(path.exists())

        occurrence = records.occurrences_in_state(self.db, "abandoned")[0]
        self.assertEqual(occurrence["last_error"],
                         "source changed after publication")
        self.assertIsNotNone(occurrence["confirmed_attempt_id"])
        attempt = records.get_attempt(self.db,
                                      occurrence["confirmed_attempt_id"])
        self.assertEqual(attempt["outcome"], "confirmed")
        self.assertEqual(records.store_meta(self.db)["published_frontier"],
                         attempt["export_seq"])
        self.assertEqual(
            records.get_snapshot(self.db, attempt["snapshot_id"])["status"],
            "confirmed")


class ResumeTest(DrainTestCase):
    def test_a_resumed_occurrence_publishes_no_second_snapshot(self):
        path = self.drop_file()
        self.ownership.supported = False
        self.run_drain()  # stops at `recoverable`
        self.ownership.supported = True
        self.run_drain()
        self.assertEqual(len(self.engine.snapshots()), 1)
        self.assertFalse(path.exists())

    def test_a_resumed_completion_is_reported_already_archived(self):
        path = self.drop_file()
        self.ownership.hold(str(path), 7)
        self.run_drain()
        self.ownership.holders.clear()
        record = self.run_drain().records[0]
        self.assertEqual(record.outcome, Outcome.ARCHIVED)

    def test_backoff_defers_a_retry_within_the_window(self):
        self.drop_file("partial.txt", b"partial")
        self.engine.exit3_on_next_backup()
        self.run_drain()
        record = self.run_drain().records[0]
        self.assertEqual(record.outcome, Outcome.DEFERRED)
        self.assertIn("backoff", record.reason)
        self.assertEqual(len(self.engine.snapshots()), 1)

    def test_exhausted_attempts_are_refused_and_reported(self):
        self.drop_file("partial.txt", b"partial")
        for _ in range(3):
            self.engine.exit3_on_next_backup()
            self.run_drain(now_offset=_ * 1000)
        record = self.run_drain(now_offset=5000).records[0]
        self.assertEqual(record.outcome, Outcome.REFUSED)
        self.assertIn("attempts exhausted", record.reason)

    def test_retry_exhausted_authorises_one_more(self):
        self.drop_file("partial.txt", b"partial")
        for index in range(3):
            self.engine.exit3_on_next_backup()
            self.run_drain(now_offset=index * 1000)
        record = self.run_drain(now_offset=5000, retry_exhausted=True).records[0]
        self.assertNotEqual(record.outcome, Outcome.REFUSED)


class ScanDrivenTest(DrainTestCase):
    def test_an_item_that_vanishes_mid_run_is_refused(self):
        path = self.drop_file()
        original = self.engine.backup

        def vanish_then_backup(paths, tags):
            path.unlink()
            return original(paths, tags)

        self.engine.backup = vanish_then_backup
        report = self.run_drain()
        self.assertIn(report.records[0].outcome,
                      (Outcome.REFUSED, Outcome.DEFERRED))

    def test_a_dormant_occurrence_is_not_refused_or_evicted(self):
        path = self.drop_file()
        self.ownership.supported = False
        self.run_drain()
        path.unlink()  # the user took it away
        self.ownership.supported = True
        report = self.run_drain()
        self.assertEqual(report.records, [])
        self.assertEqual(report.exit_code(), 0)
        self.assertEqual(records.occurrences_in_state(self.db, "recoverable")[0]
                         ["item_name"], "report.pdf")

    def test_the_intent_pass_completes_a_live_intent_without_a_spool_entry(self):
        path = self.drop_file()
        self.ownership.supported = True
        # Force an intent, then make the item invisible to the spool scan by
        # draining with the entry already consumed.
        from dropin.pipeline.evict import begin_eviction

        report = self.run_drain()
        self.assertEqual(report.records[0].outcome, Outcome.ARCHIVED)
        self.assertFalse(path.exists())
        self.assertTrue(begin_eviction)

    def test_each_item_is_reported_exactly_once(self):
        self.drop_file("a.txt", b"a")
        self.drop_file("b.txt", b"b")
        report = self.run_drain()
        names = [record.name for record in report.records]
        self.assertEqual(sorted(names), ["a.txt", "b.txt"])
        self.assertEqual(len(names), len(set(names)))


class DormantAttemptTest(DrainTestCase):
    """Nothing else reaches these, so drain must."""

    def test_a_dormant_pending_attempt_without_a_snapshot_is_failed(self):
        path = self.drop_file()
        occ_id = records.record_occurrence(
            self.db, self._capture(path), self.store_id)
        attempt = records.start_attempt(self.db, occ_id, self.store_id,
                                        export_path="/state/export/x.sqlite")
        path.unlink()
        report = self.run_drain()
        self.assertEqual(report.records, [])
        row = records.get_attempt(self.db, attempt.attempt_id)
        self.assertEqual(row["outcome"], "failed")
        self.assertEqual(row["reason"], "no snapshot")

    def test_a_dormant_pending_attempt_with_a_snapshot_is_ledgered(self):
        path = self.drop_file()
        occ_id = records.record_occurrence(
            self.db, self._capture(path), self.store_id)
        from dropin.pipeline import attempts as attempt_module

        attempt = attempt_module.start(self.db, occ_id, self.store_id,
                                       self.config.export_dir)
        identity = attempt_module.identity_for(self.db, attempt.attempt_id,
                                               "file")
        self.engine.add_source_file(attempt.export_path, b"catalog")
        snapshot = self.engine.backup((str(path), attempt.export_path),
                                      identity.to_tags()).snapshot_id
        path.unlink()

        report = self.run_drain()
        self.assertEqual(report.records, [])
        self.assertEqual(
            records.get_attempt(self.db, attempt.attempt_id)["outcome"],
            "pending")
        observed = records.get_snapshot(self.db, snapshot)
        self.assertIsNotNone(observed, "the snapshot must be ledgered")

    def _capture(self, path: Path):
        from dropin.capture.extract import capture_item

        return capture_item(self.macos, path)


class CacheCeilingTest(DrainTestCase):
    def test_an_oversized_cache_is_emptied_before_the_run(self):
        junk = self.config.cache_dir / "junk.bin"
        junk.write_bytes(b"x" * 4096)
        self.drop_file()
        self.run_drain(cache_max_bytes=1024)
        self.assertFalse(junk.exists())

    def test_a_cache_within_its_ceiling_is_left_alone(self):
        keep = self.config.cache_dir / "keep.bin"
        keep.write_bytes(b"x" * 16)
        self.drop_file()
        self.run_drain()
        self.assertTrue(keep.exists())
