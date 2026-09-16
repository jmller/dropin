"""The catalog export shipped inside every snapshot."""

from __future__ import annotations

from contextlib import closing
import hashlib
from pathlib import Path
import sqlite3
import tempfile
import unittest

from dropin.capture.extract import capture_item
from dropin.macos.fake import FakeMacOS
from dropin.store import records
from dropin.store.db import connect
from dropin.store.export import discard_export, export_catalog

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "synthetic"


class ExportTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dropin-export-")
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
        self.occ_id = self.record()

    def record(self, name="report.pdf", content=b"payload"):
        path = self.drop / name
        path.write_bytes(content)
        self.macos.set_mdls(str(path),
                            (FIXTURES / "mdls" / "pdf_tagged.txt").read_text())
        self.macos.set_importer(
            str(path), (FIXTURES / "mdimport" / "pdf_with_text.txt").read_text())
        return records.record_occurrence(self.db, capture_item(self.macos, path),
                                         self.store_id)

    def export(self, occ_id=None):
        return export_catalog(self.db, occ_id or self.occ_id, self.store_id,
                              self.export_dir)


class ExportTest(ExportTestCase):
    def test_allocates_a_sequence_and_commits_a_pending_attempt_first(self):
        result = self.export()
        attempt = records.get_attempt(self.db, result.attempt_id)
        self.assertEqual(attempt["outcome"], "pending")
        self.assertEqual(attempt["export_seq"], 1)
        self.assertEqual(records.store_meta(self.db)["export_seq"], 1)

    def test_export_path_follows_the_documented_convention(self):
        result = self.export()
        self.assertEqual(
            Path(result.export_path).name,
            f"{self.occ_id}-{result.attempt_id}.sqlite")
        self.assertTrue(Path(result.export_path).exists())

    def test_digest_is_of_the_finalized_file_and_stored_on_the_live_attempt(self):
        result = self.export()
        digest = hashlib.sha256(Path(result.export_path).read_bytes()).hexdigest()
        self.assertEqual(result.catalog_sha256, digest)
        self.assertEqual(
            records.get_attempt(self.db, result.attempt_id)["export_sha256"],
            digest)

    def test_exported_file_is_a_closed_delete_mode_database(self):
        result = self.export()
        with closing(sqlite3.connect(result.export_path)) as exported, exported:
            mode = exported.execute("PRAGMA journal_mode").fetchone()[0]
        self.assertEqual(mode.lower(), "delete")
        self.assertFalse(Path(result.export_path + "-wal").exists())

    def test_exactly_one_lineage_row_identifying_this_attempt(self):
        result = self.export()
        with closing(sqlite3.connect(result.export_path)) as exported, exported:
            rows = exported.execute(
                "SELECT store_id, export_seq, occ_id, attempt_id"
                " FROM export_lineage").fetchall()
        self.assertEqual(rows, [(self.store_id, 1, self.occ_id,
                                 result.attempt_id)])

    def test_prior_lineage_rows_are_cleared(self):
        self.export()
        second = self.export(self.record("other.pdf", b"other"))
        with closing(sqlite3.connect(second.export_path)) as exported, exported:
            count = exported.execute(
                "SELECT count(*) FROM export_lineage").fetchone()[0]
        self.assertEqual(count, 1)

    def test_export_carries_no_self_digest(self):
        # A whole-file hash cannot live inside the file it describes.
        result = self.export()
        with closing(sqlite3.connect(result.export_path)) as exported, exported:
            columns = {row[1] for row in
                       exported.execute("PRAGMA table_info(export_lineage)")}
            attempt = exported.execute(
                "SELECT export_sha256 FROM publication_attempt"
                " WHERE attempt_id = ?", (result.attempt_id,)).fetchone()
        self.assertNotIn("catalog_sha256", columns)
        self.assertIsNone(attempt[0],
                          "the exported attempt row must not carry its own digest")

    def test_export_includes_committed_but_uncheckpointed_wal_rows(self):
        # The live store is in WAL mode and is not checkpointed before export;
        # a plain file copy would miss the newest committed rows.
        late = self.record("late.pdf", b"late")
        result = self.export()
        with closing(sqlite3.connect(result.export_path)) as exported, exported:
            found = exported.execute(
                "SELECT count(*) FROM occurrence WHERE occ_id = ?",
                (late,)).fetchone()[0]
        self.assertEqual(found, 1)

    def test_export_contains_the_occurrence_at_recorded_with_its_manifest(self):
        result = self.export()
        with closing(sqlite3.connect(result.export_path)) as exported, exported:
            state = exported.execute(
                "SELECT state FROM occurrence WHERE occ_id = ?",
                (self.occ_id,)).fetchone()[0]
            entries = exported.execute(
                "SELECT count(*) FROM entry WHERE occ_id = ?",
                (self.occ_id,)).fetchone()[0]
            fingerprints = exported.execute(
                "SELECT count(*) FROM fingerprint WHERE occ_id = ?",
                (self.occ_id,)).fetchone()[0]
        self.assertEqual(state, "recorded")
        self.assertEqual(entries, 1)
        self.assertEqual(fingerprints, 1)

    def test_export_is_readable_and_integral(self):
        result = self.export()
        with closing(sqlite3.connect(f"file:{result.export_path}?mode=ro", uri=True)) as ro, ro:
            self.assertEqual(ro.execute("PRAGMA integrity_check").fetchone()[0],
                             "ok")

    def test_two_exports_have_distinct_sequences_and_paths(self):
        first = self.export()
        second = self.export(self.record("second.pdf", b"second"))
        self.assertNotEqual(first.export_path, second.export_path)
        self.assertEqual(second.export_seq, first.export_seq + 1)

    def test_no_temporary_file_is_left_behind(self):
        result = self.export()
        leftovers = [p.name for p in self.export_dir.iterdir()
                     if p.name != Path(result.export_path).name]
        self.assertEqual(leftovers, [])


class DiscardTest(ExportTestCase):
    def test_discard_removes_the_export_file(self):
        result = self.export()
        discard_export(result.export_path)
        self.assertFalse(Path(result.export_path).exists())

    def test_discard_is_idempotent(self):
        result = self.export()
        discard_export(result.export_path)
        discard_export(result.export_path)
