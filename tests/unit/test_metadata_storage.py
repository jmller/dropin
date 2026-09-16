"""Typed structured metadata, v1 migration, and old/new catalog compatibility."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from dropin.capture.extract import capture_item
from dropin.capture.mdls_parser import Attr
from dropin.engine.fake import FakeEngine
from dropin.engine.interface import Identity
from dropin.macos.fake import FakeMacOS
from dropin.recover import recover
from dropin.report import Report
from dropin.store.db import SCHEMA_DIR, connect
from dropin.store.export import export_catalog
from dropin.store import records
from tests.unit.test_macos_recordings import recording


class MetadataStorageTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dropin-metadata-store-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "sample.pdf"
        self.path.write_bytes(b"disposable")
        self.macos = FakeMacOS()
        self.macos.set_mdls(str(self.path), 'kMDItemContentType = "com.adobe.pdf"')
        self.macos.set_importer(str(self.path), 'Attributes: { kMDItemTextContent = "evidence"; }')

    def legacy(self, path):
        db = sqlite3.connect(path, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.executescript((SCHEMA_DIR / "0001_initial.sql").read_text() + "\nPRAGMA user_version=1;")
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def test_populated_v1_migrates_without_changing_rows_or_foreign_keys(self):
        path = self.root / "legacy.sqlite"
        with closing(self.legacy(path)) as db:
            store = records.initialise_store(db)
            records.record_occurrence(db, capture_item(self.macos, self.path), store)
            tables = ("occurrence", "entry", "attribute", "fingerprint", "normalized")
            before = {table: [tuple(row) for row in db.execute(f"SELECT * FROM {table}")] for table in tables}
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 1)
        with closing(connect(path)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)
            for table in tables:
                self.assertEqual([tuple(row) for row in db.execute(f"SELECT * FROM {table}")], before[table])
            self.assertEqual(list(db.execute("PRAGMA foreign_key_check")), [])
            item = capture_item(self.macos, self.path)
            item.entries[0].attributes[("structured", "importer")] = Attr({"": "é", "nested": [1, None]}, "dict")
            records.record_occurrence(db, item, store)
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("INSERT INTO attribute VALUES ('absent','','key','{}','dict','importer')")
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("UPDATE attribute SET value_type='invented'")

    def test_real_metadata_survives_record_export_and_reopen(self):
        self.macos.set_mdls(str(self.path), recording("mdls", "tagged.pdf")["stdout"])
        self.macos.set_importer(str(self.path), recording("mdimport", "tagged.pdf-d3")["stdout"])
        item = capture_item(self.macos, self.path)
        with closing(connect(self.root / "store.sqlite")) as db:
            store = records.initialise_store(db)
            occ = records.record_occurrence(db, item, store)
            export = export_catalog(db, occ, store, self.root / "exports")
        with closing(connect(export.export_path, read_only=True)) as db:
            for (key, source), attr in item.entries[0].attributes.items():
                row = db.execute("SELECT value_json,value_type FROM attribute WHERE key=? AND source=?", (key, source)).fetchone()
                self.assertEqual((json.loads(row[0]), row[1]), (attr.value, attr.type))
            row = db.execute("SELECT kind,text FROM normalized").fetchone()
            self.assertEqual(tuple(row), ("PDF document", "Dropin disposable PDF evidence."))
            self.assertEqual(db.execute("SELECT count(*) FROM fulltext WHERE fulltext MATCH 'disposable'").fetchone()[0], 1)
            self.assertEqual(list(db.execute("PRAGMA foreign_key_check")), [])

    def test_normalized_localized_kind_only_and_mdls_precedence(self):
        item = capture_item(self.macos, self.path)
        entry = item.entries[0]
        entry.attributes[("kMDItemKind", "importer")] = Attr({"": "PDF document", "ja": "PDF書類"}, "dict")
        self.assertEqual(entry.kind, "PDF document")
        entry.attributes[("kMDItemKind", "mdls")] = Attr("preferred", "string")
        self.assertEqual(entry.kind, "preferred")
        for value in ({"ja": "PDF書類"}, {"": ["invalid"]}, ["invalid"]):
            entry.attributes[("kMDItemKind", "mdls")] = Attr(value, "dict" if isinstance(value, dict) else "list")
            self.assertIsNone(entry.kind)  # don't replace the preferred source with a guess
        for key, prop in (("kMDItemContentType", "uti"), ("kMDItemContentCreationDate", "created"), ("kMDItemContentModificationDate", "modified")):
            entry.attributes[(key, "mdls")] = Attr({"": "not a scalar"}, "dict")
            self.assertIsNone(getattr(entry, prop))
        for value in ({"": "not text"}, ["not text"]):
            with mock.patch.object(self.macos, "importer_attributes", return_value={"kMDItemTextContent": Attr(value, "dict")}):
                self.assertIsNone(capture_item(self.macos, self.path).entries[0].text)

    def test_recovery_accepts_v1_and_v2_catalogs_in_either_base_order(self):
        for versions in ((1, 2), (2, 1)):
            with self.subTest(versions=versions):
                engine = FakeEngine()
                engine.init()
                for index, version in enumerate(versions):
                    label = f"{versions[0]}-{index}"
                    path = self.root / (label + ".sqlite")
                    with closing(self.legacy(path) if version == 1 else connect(path)) as db:
                        store = records.initialise_store(db)
                        item = capture_item(self.macos, self.path)
                        if version == 2:
                            item.entries[0].attributes[("localized", "importer")] = Attr({"": "é"}, "dict")
                        occ = records.record_occurrence(db, item, store)
                        export = export_catalog(db, occ, store, self.root / (label + "-exports"))
                        identity = Identity(store_id=store, occ_id=occ, attempt_id=export.attempt_id, export_seq=export.export_seq, kind="file", catalog_sha256=export.catalog_sha256)
                        engine.add_source_file(str(self.path), self.path.read_bytes())
                        engine.add_source_file(export.export_path, Path(export.export_path).read_bytes())
                        engine.backup((str(self.path), export.export_path), identity.to_tags())
                result = recover(engine, self.root / f"recover-{versions[0]}", Report(verb="recover", run_id="metadata-test"))
                with closing(connect(result.store_path)) as db:
                    self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)
                    self.assertEqual(db.execute("SELECT count(*) FROM occurrence WHERE state='recoverable'").fetchone()[0], 2)
                    self.assertEqual(json.loads(db.execute("SELECT value_json FROM attribute WHERE value_type='dict'").fetchone()[0]), {"": "é"})
                    self.assertEqual(list(db.execute("PRAGMA foreign_key_check")), [])
