"""Remote catalog verification before `recoverable`.

Tag inspection is not proof: the export is dumped back from the exact snapshot
and checked against the digest, its own integrity, its lineage row, and the
local rows it claims to describe.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
import sqlite3
import tempfile
import unittest

from dropin.capture.extract import capture_item
from dropin.engine.fake import FakeEngine
from dropin.engine.interface import Identity
from dropin.macos.fake import FakeMacOS
from dropin.pipeline.catalog_verify import (CatalogError, verify_catalog,
                                            verify_catalog_file)
from dropin.store import records
from dropin.store.db import connect
from dropin.store.export import export_catalog

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "synthetic"


class CatalogTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dropin-catalog-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.drop = self.root / "drop"
        self.state = self.root / "state"
        self.export_dir = self.state / "export"
        self.tmp_dir = self.state / "tmp"
        for path in (self.drop, self.export_dir, self.tmp_dir):
            path.mkdir(parents=True)
        self.db = connect(self.state / "store.sqlite")
        self.addCleanup(self.db.close)
        self.store_id = records.initialise_store(self.db)
        self.macos = FakeMacOS()
        self.engine = FakeEngine()
        self.engine.init()

        self.item_path = self.drop / "report.pdf"
        self.item_path.write_bytes(b"payload")
        self.macos.set_mdls(str(self.item_path),
                            (FIXTURES / "mdls" / "pdf_tagged.txt").read_text())
        self.macos.set_importer(
            str(self.item_path),
            (FIXTURES / "mdimport" / "pdf_with_text.txt").read_text())
        self.occ_id = records.record_occurrence(
            self.db, capture_item(self.macos, self.item_path), self.store_id)
        self.export = export_catalog(self.db, self.occ_id, self.store_id,
                                     self.export_dir)
        self.identity = Identity(
            store_id=self.store_id, occ_id=self.occ_id,
            attempt_id=self.export.attempt_id, export_seq=self.export.export_seq,
            kind="file", catalog_sha256=self.export.catalog_sha256)
        self.engine.add_source_file(str(self.item_path), b"payload")
        self.engine.add_source_file(self.export.export_path,
                                    Path(self.export.export_path).read_bytes())
        self.snapshot = self.engine.backup(
            (str(self.item_path), self.export.export_path),
            self.identity.to_tags()).snapshot_id

    def verify(self, **overrides):
        kwargs = dict(engine=self.engine, snapshot_id=self.snapshot,
                      export_path=self.export.export_path,
                      identity=self.identity, connection=self.db,
                      occ_id=self.occ_id, tmp_dir=self.tmp_dir,
                      live_digest=self.export.catalog_sha256)
        kwargs.update(overrides)
        return verify_catalog(**kwargs)


class HappyPathTest(CatalogTestCase):
    def test_matching_export_passes(self):
        self.verify()

    def test_temporary_dump_is_removed_afterwards(self):
        self.verify()
        self.assertEqual(list(self.tmp_dir.iterdir()), [])

    def test_digest_must_equal_both_the_tag_and_the_live_attempt(self):
        # Same value, checked twice on purpose: the tag is immutable evidence,
        # the live row is what this run believes it published.
        self.verify()
        self.assertEqual(self.identity.catalog_sha256,
                         records.get_attempt(self.db,
                                             self.export.attempt_id)["export_sha256"])


class FailureTest(CatalogTestCase):
    def test_missing_export_in_the_snapshot(self):
        self.engine.drop_export(self.snapshot)
        with self.assertRaises(CatalogError) as caught:
            self.verify()
        self.assertIn("dump", caught.exception.check)

    def test_dump_failure_is_a_catalog_failure(self):
        self.engine.inject_corruption(self.snapshot, self.export.export_path)
        with self.assertRaises(CatalogError) as caught:
            self.verify()
        self.assertIn("dump", caught.exception.check)

    def test_digest_mismatch_against_the_tag(self):
        self.engine.inject_corruption(self.snapshot, self.export.export_path,
                                      flip_byte=True)
        with self.assertRaises(CatalogError) as caught:
            self.verify()
        self.assertEqual(caught.exception.check, "digest")

    def test_digest_mismatch_against_the_live_attempt(self):
        with self.assertRaises(CatalogError) as caught:
            self.verify(live_digest="f" * 64)
        self.assertEqual(caught.exception.check, "digest")

    def test_integrity_check_failure(self):
        broken = self.root / "broken.sqlite"
        broken.write_bytes(b"SQLite format 3\x00" + b"\x00" * 200)
        digest = hashlib.sha256(broken.read_bytes()).hexdigest()
        self.engine.add_source_file("/state/export/broken.sqlite",
                                    broken.read_bytes())
        identity = Identity(store_id=self.store_id, occ_id=self.occ_id,
                            attempt_id=self.export.attempt_id,
                            export_seq=self.export.export_seq, kind="file",
                            catalog_sha256=digest)
        snapshot = self.engine.backup(("/state/export/broken.sqlite",),
                                      identity.to_tags()).snapshot_id
        with self.assertRaises(CatalogError) as caught:
            self.verify(snapshot_id=snapshot,
                        export_path="/state/export/broken.sqlite",
                        identity=identity, live_digest=digest)
        self.assertIn(caught.exception.check, ("integrity_check", "open"))

    def test_lineage_row_must_match_the_identity_tags(self):
        wrong = Identity(store_id=self.store_id, occ_id=self.occ_id,
                         attempt_id=self.export.attempt_id,
                         export_seq=self.export.export_seq + 5, kind="file",
                         catalog_sha256=self.export.catalog_sha256)
        with self.assertRaises(CatalogError) as caught:
            self.verify(identity=wrong)
        self.assertEqual(caught.exception.check, "lineage")

    def test_occurrence_row_must_match(self):
        self.db.execute("UPDATE occurrence SET root_sha256 = ? WHERE occ_id = ?",
                        ("f" * 64, self.occ_id))
        with self.assertRaises(CatalogError) as caught:
            self.verify()
        self.assertEqual(caught.exception.check, "occurrence")

    def test_entry_rows_must_match(self):
        other = self.drop / "other.pdf"
        other.write_bytes(b"other")
        self.macos.set_mdls(str(other),
                            (FIXTURES / "mdls" / "pdf_tagged.txt").read_text())
        self.macos.set_importer(
            str(other), (FIXTURES / "mdimport" / "no_text.txt").read_text())
        other_occ = records.record_occurrence(
            self.db, capture_item(self.macos, other), self.store_id)
        with self.assertRaises(CatalogError) as caught:
            self.verify(occ_id=other_occ)
        self.assertIn(caught.exception.check, ("occurrence", "entries"))


class RecoveryModeTest(CatalogTestCase):
    """Fresh recovery has no live digest: the tag is the expected hash."""

    def test_file_check_passes_without_a_live_store(self):
        dumped = self.root / "dumped.sqlite"
        dumped.write_bytes(Path(self.export.export_path).read_bytes())
        verify_catalog_file(dumped, self.identity)

    def test_file_check_rejects_a_wrong_digest(self):
        dumped = self.root / "dumped.sqlite"
        dumped.write_bytes(Path(self.export.export_path).read_bytes())
        wrong = Identity(store_id=self.store_id, occ_id=self.occ_id,
                         attempt_id=self.export.attempt_id,
                         export_seq=self.export.export_seq, kind="file",
                         catalog_sha256="a" * 64)
        with self.assertRaises(CatalogError) as caught:
            verify_catalog_file(dumped, wrong)
        self.assertEqual(caught.exception.check, "digest")

    def test_file_check_is_hashed_before_the_database_is_opened(self):
        # Opening an unverified SQLite file is exactly what the digest guards.
        order: list[str] = []
        import dropin.pipeline.catalog_verify as module

        real_hash, real_connect = module._sha256, sqlite3.connect

        def traced_hash(path):
            order.append("hash")
            return real_hash(path)

        def traced_connect(*args, **kwargs):
            order.append("open")
            return real_connect(*args, **kwargs)

        module._sha256 = traced_hash
        sqlite3.connect = traced_connect
        self.addCleanup(setattr, module, "_sha256", real_hash)
        self.addCleanup(setattr, sqlite3, "connect", real_connect)

        dumped = self.root / "dumped.sqlite"
        dumped.write_bytes(Path(self.export.export_path).read_bytes())
        verify_catalog_file(dumped, self.identity)
        self.assertEqual(order[0], "hash")
