"""The stale-store and lineage gate."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from dropin.capture.extract import capture_item
from dropin.engine.fake import FakeEngine
from dropin.engine.interface import Identity
from dropin.macos.fake import FakeMacOS
from dropin.pipeline import attempts
from dropin.pipeline.frontier import (LineageMismatch, StaleStore,
                                      check_frontier)
from dropin.store import records
from dropin.store.db import connect

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "synthetic"


class FrontierTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dropin-frontier-")
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

    def record(self, name="report.pdf", content=b"payload"):
        path = self.drop / name
        path.write_bytes(content)
        self.macos.set_mdls(str(path),
                            (FIXTURES / "mdls" / "pdf_tagged.txt").read_text())
        self.macos.set_importer(
            str(path), (FIXTURES / "mdimport" / "no_text.txt").read_text())
        self.engine.add_source_file(str(path), content)
        return records.record_occurrence(self.db, capture_item(self.macos, path),
                                         self.store_id), path

    def identity(self, attempt, kind="file"):
        return Identity(store_id=self.store_id, occ_id=attempt.occ_id,
                        attempt_id=attempt.attempt_id,
                        export_seq=attempt.export_seq, kind=kind,
                        catalog_sha256=attempt.catalog_sha256)

    def publish(self, name="report.pdf", *, adopt=True):
        occ_id, path = self.record(name, name.encode())
        attempt = attempts.start(self.db, occ_id, self.store_id, self.export_dir)
        self.engine.add_source_file(attempt.export_path, b"catalog")
        identity = self.identity(attempt)
        snapshot = self.engine.backup((str(path), attempt.export_path),
                                      identity.to_tags()).snapshot_id
        if adopt:
            attempts.adopt(self.db, attempt.attempt_id, snapshot, identity)
        return occ_id, attempt, identity, snapshot

    def check(self):
        check_frontier(self.db, self.engine, self.store_id)


class PassingTest(FrontierTestCase):
    def test_empty_repository_passes(self):
        self.check()

    def test_a_snapshot_matching_a_local_attempt_and_ledger_passes(self):
        self.publish()
        self.check()

    def test_a_failed_attempt_with_an_orphaned_snapshot_passes(self):
        _, attempt, _, snapshot = self.publish()
        attempts.fail(self.db, attempt.attempt_id, "exit 3",
                      snapshot_id=snapshot)
        self.check()

    def test_a_confirmed_attempt_passes(self):
        occ_id, attempt, _, snapshot = self.publish()
        records.set_state(self.db, occ_id, "transferred")
        records.set_state(self.db, occ_id, "verified")
        attempts.confirm(self.db, attempt.attempt_id, occ_id, snapshot,
                         attempt.export_seq)
        self.check()

    def test_a_pending_attempt_whose_snapshot_id_was_never_recorded_passes(self):
        # The attempt row is committed before the backup, so a crash
        # in between leaves snapshot_id null *by design*. Demanding a recorded
        # snapshot id here would refuse every later run and brick the store.
        _, attempt, _, snapshot = self.publish(adopt=False)
        self.assertIsNone(
            records.get_attempt(self.db, attempt.attempt_id)["snapshot_id"])
        self.assertIsNone(records.get_snapshot(self.db, snapshot))
        self.check()

    def test_that_gate_records_an_observation_without_settling_the_attempt(self):
        _, attempt, _, snapshot = self.publish(adopt=False)
        self.check()
        observed = records.get_snapshot(self.db, snapshot)
        self.assertIsNotNone(observed)
        self.assertEqual(observed["status"], "pending")
        self.assertEqual(
            records.get_attempt(self.db, attempt.attempt_id)["outcome"], "pending")

    def test_another_items_unreconciled_snapshot_does_not_block_this_one(self):
        # The crashed item is not the one being drained; the run must continue.
        self.publish("crashed.pdf", adopt=False)
        self.check()

    def test_an_exact_ledger_observation_passes_when_its_export_was_corrupt(self):
        _, attempt, identity, snapshot = self.publish()
        records.observe_snapshot(self.db, snapshot, identity, status="orphaned",
                                 reason="catalog: integrity_check")
        attempts.fail(self.db, attempt.attempt_id, "catalog: integrity_check",
                      snapshot_id=snapshot)
        self.check()

    def test_ledger_observation_passes_after_the_attempt_row_is_gone(self):
        # Recovery ledgers snapshots it cannot tie to a local attempt row.
        _, attempt, identity, snapshot = self.publish()
        records.observe_snapshot(self.db, snapshot, identity, status="orphaned",
                                 reason="catalog: missing export")
        self.db.execute("DELETE FROM publication_attempt WHERE attempt_id = ?",
                        (attempt.attempt_id,))
        records.note_observed_sequence(self.db, identity.export_seq)
        self.check()


class StaleTest(FrontierTestCase):
    def foreign_snapshot(self, store_id: str, seq: int = 1) -> str:
        occ = records.new_id(store_id)
        attempt = records.new_id(store_id)
        identity = Identity(store_id=store_id, occ_id=occ, attempt_id=attempt,
                            export_seq=seq, kind="file",
                            catalog_sha256="d" * 64)
        self.engine.add_source_file(f"/elsewhere/{seq}", b"x")
        return self.engine.backup((f"/elsewhere/{seq}",),
                                  identity.to_tags()).snapshot_id

    def test_an_unknown_local_snapshot_is_stale(self):
        self.foreign_snapshot(self.store_id, seq=7)
        with self.assertRaises(StaleStore) as caught:
            self.check()
        self.assertIn("recover --into", str(caught.exception))
        self.assertIn("7", str(caught.exception))

    def test_stale_refusal_preserves_the_existing_store(self):
        self.foreign_snapshot(self.store_id, seq=7)
        with self.assertRaises(StaleStore):
            self.check()
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM store_meta").fetchone()[0], 1)

    def test_a_mismatched_tag_set_on_a_known_snapshot_is_stale(self):
        _, attempt, identity, snapshot = self.publish()
        # Same snapshot id in the ledger, different canonical tags now.
        self.db.execute("UPDATE snapshot SET export_seq = 99,"
                        " tag_set_json = replace(tag_set_json, '\"1\"', '\"99\"')"
                        " WHERE snapshot_id = ?", (snapshot,))
        with self.assertRaises(StaleStore):
            self.check()

    def test_high_water_marks_alone_never_admit_an_unknown_snapshot(self):
        records.note_observed_sequence(self.db, 100)
        records.advance_frontier(self.db, 100)
        self.foreign_snapshot(self.store_id, seq=5)
        with self.assertRaises(StaleStore):
            self.check()

    def test_malformed_reserved_tags_are_refused_not_ignored(self):
        self.engine.add_source_file("/elsewhere/bad", b"x")
        self.engine.backup(("/elsewhere/bad",),
                           ("dropin:v=1", f"dropin:store={self.store_id}",
                            "dropin:occ=nonsense", "dropin:attempt=nonsense",
                            "dropin:seq=1", "dropin:kind=file",
                            "dropin:catalog-sha256=" + "d" * 64))
        with self.assertRaises(StaleStore) as caught:
            self.check()
        self.assertIn("tag", str(caught.exception).lower())

    def test_duplicate_reserved_tags_are_refused(self):
        _, attempt, identity, _ = self.publish()
        self.engine.add_source_file("/elsewhere/dup", b"x")
        self.engine.backup(("/elsewhere/dup",),
                           tuple(identity.to_tags()) + ("dropin:seq=2",))
        with self.assertRaises(StaleStore):
            self.check()


class LineageTest(FrontierTestCase):
    def foreign(self, seq: int = 1) -> tuple[str, str]:
        store_id = "f" * 32
        occ = records.new_id(store_id)
        attempt = records.new_id(store_id)
        identity = Identity(store_id=store_id, occ_id=occ, attempt_id=attempt,
                            export_seq=seq, kind="file",
                            catalog_sha256="d" * 64)
        self.engine.add_source_file(f"/foreign/{seq}", b"x")
        snapshot = self.engine.backup((f"/foreign/{seq}",),
                                      identity.to_tags()).snapshot_id
        return store_id, snapshot

    def test_an_unadopted_foreign_store_is_a_lineage_mismatch(self):
        self.foreign()
        with self.assertRaises(LineageMismatch) as caught:
            self.check()
        self.assertIn("f" * 32, str(caught.exception))

    def test_an_adopted_lineage_within_its_merged_sequence_passes(self):
        store_id, _ = self.foreign(seq=1)
        records.merge_lineage(self.db, store_id, merged_through_seq=1)
        self.check()

    def test_a_foreign_sequence_beyond_the_merge_point_is_refused(self):
        store_id, _ = self.foreign(seq=4)
        records.merge_lineage(self.db, store_id, merged_through_seq=1)
        with self.assertRaises(LineageMismatch):
            self.check()


class OrderingTest(FrontierTestCase):
    def test_the_gate_does_not_depend_on_engine_ordering(self):
        for name in ("a.pdf", "b.pdf", "c.pdf"):
            self.publish(name)
        # The fake returns snapshots newest-first on purpose.
        self.check()
