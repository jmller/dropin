"""Records: identity, one-transaction capture, attempts, ledger."""

from __future__ import annotations

from pathlib import Path
import sqlite3
import tempfile
import unittest

from dropin.capture.extract import capture_item
from dropin.engine.interface import IDENTIFIER_RE, Identity, parse_tags
from dropin.macos.fake import FakeMacOS
from dropin.store import records
from dropin.store.db import connect

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "synthetic"


class RecordsTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dropin-records-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.drop = self.root / "drop"
        self.drop.mkdir()
        self.db = connect(self.root / "store.sqlite")
        self.addCleanup(self.db.close)
        self.store_id = records.initialise_store(self.db)
        self.macos = FakeMacOS()

    def capture(self, name: str = "report.pdf", content: bytes = b"payload"):
        path = self.drop / name
        path.write_bytes(content)
        self.macos.set_mdls(str(path),
                            (FIXTURES / "mdls" / "pdf_tagged.txt").read_text())
        self.macos.set_importer(
            str(path), (FIXTURES / "mdimport" / "pdf_with_text.txt").read_text())
        self.macos.set_xattr(str(path), "com.apple.metadata:_kMDItemUserTags",
                             (FIXTURES / "xattr" / "tags_plist.bin").read_bytes())
        return capture_item(self.macos, path)

    def record(self, name: str = "report.pdf", content: bytes = b"payload"):
        return records.record_occurrence(self.db, self.capture(name, content),
                                         self.store_id)


class IdentityTest(RecordsTestCase):
    def test_store_id_is_32_lowercase_hex(self):
        self.assertRegex(self.store_id, r"^[0-9a-f]{32}$")

    def test_namespaced_ids_carry_the_store_and_a_canonical_ulid(self):
        value = records.new_id(self.store_id)
        self.assertRegex(value, IDENTIFIER_RE)
        self.assertTrue(value.startswith(self.store_id + "."))

    def test_ulids_are_unique_and_time_sortable(self):
        values = [records.new_id(self.store_id) for _ in range(50)]
        self.assertEqual(len(set(values)), 50)
        self.assertEqual(values, sorted(values))

    def test_ulid_alphabet_excludes_ambiguous_letters(self):
        for _ in range(20):
            suffix = records.new_id(self.store_id).split(".", 1)[1]
            self.assertFalse(set(suffix) & set("ILOU"))

    def test_validate_rejects_a_foreign_namespace(self):
        other = "f" * 32
        with self.assertRaises(ValueError):
            records.validate_id(records.new_id(other), store_id=self.store_id)

    def test_validate_rejects_malformed_ids(self):
        for value in ("", "nope", self.store_id, f"{self.store_id}.short"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    records.validate_id(value)


class RecordOccurrenceTest(RecordsTestCase):
    def test_one_transaction_populates_every_table(self):
        occ_id = self.record()
        for table in ("occurrence", "entry", "fingerprint", "attribute",
                      "xattr", "normalized", "tag"):
            with self.subTest(table=table):
                count = self.db.execute(
                    f"SELECT count(*) FROM {table} WHERE occ_id = ?",
                    (occ_id,)).fetchone()[0]
                self.assertGreater(count, 0, table)

    def test_nothing_is_committed_when_the_transaction_fails(self):
        item = self.capture()
        item.entries[0].attributes[("bad", "nonsense-source")] = \
            item.entries[0].attributes[("kMDItemKind", "mdls")]
        with self.assertRaises(sqlite3.IntegrityError):
            records.record_occurrence(self.db, item, self.store_id)
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM occurrence").fetchone()[0], 0)

    def test_archive_path_is_namespaced_and_unique(self):
        first = self.record("same.pdf", b"one")
        second = self.record("same.pdf", b"two")
        paths = [row["archive_path"] for row in self.db.execute(
            "SELECT archive_path FROM occurrence ORDER BY occ_id")]
        self.assertEqual(len(set(paths)), 2)
        for occ_id, path in zip(sorted([first, second]), sorted(paths)):
            self.assertTrue(path.startswith(occ_id + "/"))

    def test_state_starts_at_recorded_with_a_timestamp(self):
        occ_id = self.record()
        row = records.get_occurrence(self.db, occ_id)
        self.assertEqual(row["state"], "recorded")
        self.assertTrue(row["recorded_at"])
        self.assertIsNone(row["confirmed_attempt_id"])

    def test_returning_to_recorded_preserves_original_capture_timestamp(self):
        from unittest.mock import patch

        with patch.object(records, "_now", return_value="2026-01-01T00:00:00Z"):
            occ_id = self.record()
        with patch.object(records, "_now", return_value="2026-02-01T00:00:00Z"):
            records.set_state(self.db, occ_id, "transferred")
            records.set_state(self.db, occ_id, "recorded", error="offline")
        row = records.get_occurrence(self.db, occ_id)
        self.assertEqual(row["recorded_at"], "2026-01-01T00:00:00Z")
        self.assertEqual(row["transferred_at"], "2026-02-01T00:00:00Z")
        self.assertEqual(row["state"], "recorded")
        self.assertEqual(row["last_error"], "offline")

    def test_normalized_row_feeds_full_text_search(self):
        occ_id = self.record()
        row = self.db.execute(
            "SELECT n.name, n.text FROM fulltext f JOIN normalized n"
            " ON n.rowid = f.rowid WHERE fulltext MATCH 'revenue'").fetchone()
        self.assertEqual(row["name"], "report.pdf")
        self.assertIn("revenue", row["text"])
        self.assertTrue(occ_id)

    def test_tags_are_stored_per_entry(self):
        occ_id = self.record()
        tags = [row["tag"] for row in self.db.execute(
            "SELECT tag FROM tag WHERE occ_id = ? ORDER BY tag", (occ_id,))]
        self.assertEqual(tags, ["important", "tax"])

    def test_entry_and_fingerprint_rows_are_immutable(self):
        occ_id = self.record()
        for statement in (
                "UPDATE entry SET sha256 = 'x' WHERE occ_id = ?",
                "UPDATE fingerprint SET inode = 1 WHERE occ_id = ?",
                "DELETE FROM entry WHERE occ_id = ?"):
            with self.subTest(statement=statement):
                with self.assertRaises(sqlite3.IntegrityError):
                    self.db.execute(statement, (occ_id,))

    def test_find_by_root_hash(self):
        first = self.record("a.pdf", b"identical")
        second = self.record("b.pdf", b"identical")
        row = records.get_occurrence(self.db, first)
        found = records.find_by_root_hash(self.db, row["root_sha256"])
        self.assertEqual({item["occ_id"] for item in found}, {first, second})

    def test_dedup_marks_the_earlier_occurrence(self):
        first = self.record("a.pdf", b"identical")
        second = self.record("b.pdf", b"identical")
        self.assertEqual(records.get_occurrence(self.db, second)["dedup_of"],
                         first)
        self.assertIsNone(records.get_occurrence(self.db, first)["dedup_of"])

    def test_cursor_reader_does_not_materialise_entries(self):
        occ_id = self.record()
        entries = records.iter_entries(self.db, occ_id)
        self.assertFalse(isinstance(entries, list))
        self.assertEqual(next(iter(entries))["rel_path"], "")


class AttemptTest(RecordsTestCase):
    def setUp(self):
        super().setUp()
        self.occ_id = self.record()

    def test_start_attempt_allocates_the_next_sequence(self):
        first = records.start_attempt(self.db, self.occ_id, self.store_id,
                                      export_path="/state/export/a.sqlite")
        second_occ = self.record("other.pdf", b"other")
        second = records.start_attempt(self.db, second_occ, self.store_id,
                                       export_path="/state/export/b.sqlite")
        self.assertEqual(first.export_seq, 1)
        self.assertEqual(second.export_seq, 2)
        self.assertEqual(records.store_meta(self.db)["export_seq"], 2)

    def test_attempt_is_pending_with_no_digest_until_the_export_is_final(self):
        attempt = records.start_attempt(self.db, self.occ_id, self.store_id,
                                        export_path="/state/export/a.sqlite")
        row = records.get_attempt(self.db, attempt.attempt_id)
        self.assertEqual(row["outcome"], "pending")
        self.assertIsNone(row["export_sha256"])
        self.assertIsNone(row["snapshot_id"])

    def test_two_stores_may_both_hold_sequence_one(self):
        foreign = "f" * 32
        records.merge_lineage(self.db, foreign, merged_through_seq=1)
        foreign_occ = records.new_id(foreign)
        self.db.execute(
            "INSERT INTO occurrence (occ_id, origin_store_id, item_name,"
            " archive_path, kind, spool_path, root_sha256, size_bytes,"
            " entry_count, state, recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (foreign_occ, foreign, "f.pdf", f"{foreign_occ}/f.pdf", "file",
             "/drop/f.pdf", "c" * 64, 1, 1, "recorded", "2026-09-06T00:00:00Z"))
        records.start_attempt(self.db, self.occ_id, self.store_id,
                              export_path="/x", export_seq=1)
        records.start_attempt(self.db, foreign_occ, foreign,
                              export_path="/y", export_seq=1)
        self.assertEqual(self.db.execute(
            "SELECT count(*) FROM publication_attempt WHERE export_seq = 1"
        ).fetchone()[0], 2)

    def test_duplicate_sequence_in_one_store_is_rejected(self):
        records.start_attempt(self.db, self.occ_id, self.store_id,
                              export_path="/x", export_seq=1)
        with self.assertRaises(sqlite3.IntegrityError):
            records.start_attempt(self.db, self.occ_id, self.store_id,
                                  export_path="/y", export_seq=1)

    def test_only_one_confirmed_attempt_per_occurrence(self):
        first = records.start_attempt(self.db, self.occ_id, self.store_id,
                                      export_path="/x")
        records.finish_attempt(self.db, first.attempt_id, "confirmed")
        second = records.start_attempt(self.db, self.occ_id, self.store_id,
                                       export_path="/y")
        with self.assertRaises(sqlite3.IntegrityError):
            records.finish_attempt(self.db, second.attempt_id, "confirmed")

    def test_failed_attempts_record_their_reason_and_finish_time(self):
        attempt = records.start_attempt(self.db, self.occ_id, self.store_id,
                                        export_path="/x")
        records.finish_attempt(self.db, attempt.attempt_id, "failed",
                               reason="catalog: integrity_check")
        row = records.get_attempt(self.db, attempt.attempt_id)
        self.assertEqual(row["outcome"], "failed")
        self.assertEqual(row["reason"], "catalog: integrity_check")
        self.assertTrue(row["finished_at"])

    def test_attempts_for_an_occurrence_are_ordered_oldest_first(self):
        first = records.start_attempt(self.db, self.occ_id, self.store_id,
                                      export_path="/x")
        records.finish_attempt(self.db, first.attempt_id, "failed", reason="x")
        second = records.start_attempt(self.db, self.occ_id, self.store_id,
                                       export_path="/y")
        listed = [row["attempt_id"] for row in
                  records.attempts_for(self.db, self.occ_id)]
        self.assertEqual(listed, [first.attempt_id, second.attempt_id])

    def test_pending_attempts_are_listed_for_reconciliation(self):
        attempt = records.start_attempt(self.db, self.occ_id, self.store_id,
                                        export_path="/x")
        self.assertEqual([row["attempt_id"] for row in
                          records.pending_attempts(self.db)],
                         [attempt.attempt_id])
        records.finish_attempt(self.db, attempt.attempt_id, "failed", reason="x")
        self.assertEqual(list(records.pending_attempts(self.db)), [])


class SnapshotLedgerTest(RecordsTestCase):
    def identity(self, seq: int = 1, occ: str | None = None,
                 attempt: str | None = None) -> Identity:
        return parse_tags(Identity(
            store_id=self.store_id,
            occ_id=occ or records.new_id(self.store_id),
            attempt_id=attempt or records.new_id(self.store_id),
            export_seq=seq, kind="file", catalog_sha256="d" * 64).to_tags())

    def test_observation_preserves_the_exact_canonical_tag_set(self):
        identity = self.identity()
        records.observe_snapshot(self.db, "a" * 64, identity, status="pending")
        row = records.get_snapshot(self.db, "a" * 64)
        self.assertEqual(row["occ_id"], identity.occ_id)
        self.assertEqual(row["attempt_id"], identity.attempt_id)
        self.assertEqual(row["export_seq"], 1)
        self.assertEqual(row["catalog_sha256"], "d" * 64)
        self.assertEqual(row["status"], "pending")
        import json

        self.assertEqual(json.loads(row["tag_set_json"]), identity.tag_set())

    def test_orphan_reason_is_kept(self):
        identity = self.identity()
        records.observe_snapshot(self.db, "b" * 64, identity, status="orphaned",
                                 reason="catalog: integrity_check")
        self.assertEqual(records.get_snapshot(self.db, "b" * 64)["reason"],
                         "catalog: integrity_check")

    def test_re_observing_updates_status_and_keeps_identity(self):
        identity = self.identity()
        records.observe_snapshot(self.db, "c" * 64, identity, status="pending")
        records.observe_snapshot(self.db, "c" * 64, identity, status="confirmed")
        row = records.get_snapshot(self.db, "c" * 64)
        self.assertEqual(row["status"], "confirmed")
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM snapshot").fetchone()[0], 1)

    def test_re_observing_with_a_different_identity_is_refused(self):
        records.observe_snapshot(self.db, "d" * 64, self.identity(),
                                 status="pending")
        with self.assertRaises(records.IdentityCollision):
            records.observe_snapshot(self.db, "d" * 64, self.identity(seq=2),
                                     status="pending")

    def test_lookup_by_attempt(self):
        identity = self.identity()
        records.observe_snapshot(self.db, "e" * 64, identity, status="pending")
        rows = records.snapshots_for_attempt(self.db, identity.attempt_id)
        self.assertEqual([row["snapshot_id"] for row in rows], ["e" * 64])


class FrontierStateTest(RecordsTestCase):
    def test_frontier_advances_only_on_confirmation(self):
        occ_id = self.record()
        attempt = records.start_attempt(self.db, occ_id, self.store_id,
                                        export_path="/x")
        self.assertEqual(records.store_meta(self.db)["published_frontier"], 0)
        records.finish_attempt(self.db, attempt.attempt_id, "confirmed")
        records.advance_frontier(self.db, attempt.export_seq)
        self.assertEqual(records.store_meta(self.db)["published_frontier"], 1)

    def test_frontier_never_goes_backwards(self):
        records.advance_frontier(self.db, 5)
        records.advance_frontier(self.db, 3)
        self.assertEqual(records.store_meta(self.db)["published_frontier"], 5)

    def test_export_seq_is_the_high_water_mark(self):
        records.note_observed_sequence(self.db, 9)
        occ_id = self.record()
        attempt = records.start_attempt(self.db, occ_id, self.store_id,
                                        export_path="/x")
        self.assertEqual(attempt.export_seq, 10)
