"""Schema, migrations, and the FTS relation."""

from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from dropin.store.db import SCHEMA_VERSION, connect, transaction


class SchemaTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dropin-db-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "store.sqlite"
        self.db = connect(self.path)
        self.addCleanup(self.db.close)

    def columns(self, table: str) -> dict[str, str]:
        return {row["name"]: row["type"]
                for row in self.db.execute(f"PRAGMA table_info({table})")}

    def index_names(self, table: str) -> set[str]:
        return {row["name"]
                for row in self.db.execute(f"PRAGMA index_list({table})")}

    def table_names(self) -> set[str]:
        return {row[0] for row in self.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}


class MigrationTest(SchemaTestCase):
    def test_fresh_database_is_at_the_current_version(self):
        self.assertEqual(self.db.execute("PRAGMA user_version").fetchone()[0],
                         SCHEMA_VERSION)
        self.assertEqual(SCHEMA_VERSION, 2)

    def test_wal_and_foreign_keys_are_on(self):
        self.assertEqual(
            self.db.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
        self.assertEqual(self.db.execute("PRAGMA foreign_keys").fetchone()[0], 1)

    def test_migrates_a_database_created_by_the_previous_schema(self):
        legacy = self.root / "legacy.sqlite"
        with closing(sqlite3.connect(legacy)) as raw, raw:
            raw.execute("PRAGMA user_version = 0")
        db = connect(legacy)
        self.addCleanup(db.close)
        self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0],
                         SCHEMA_VERSION)
        self.assertIn("occurrence", {row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")})

    def test_reopening_does_not_re_apply_migrations(self):
        self.db.execute(
            "INSERT INTO store_meta (store_id, export_seq, published_frontier,"
            " created_at) VALUES ('a'*32, 0, 0, '2026-09-06T00:00:00Z')")
        self.db.commit()
        again = connect(self.path)
        self.addCleanup(again.close)
        self.assertEqual(
            again.execute("SELECT count(*) FROM store_meta").fetchone()[0], 1)

    def test_no_table_is_without_rowid(self):
        for table in self.table_names():
            if table.startswith("fulltext"):
                continue  # FTS5 shadow tables are the module's business
            with self.subTest(table=table):
                sql = self.db.execute(
                    "SELECT sql FROM sqlite_master WHERE name=?",
                    (table,)).fetchone()[0] or ""
                self.assertNotIn("WITHOUT ROWID", sql.upper())


class TableShapeTest(SchemaTestCase):
    def test_every_documented_table_exists(self):
        self.assertLessEqual(
            {"store_meta", "lineage", "occurrence", "entry", "fingerprint",
             "attribute", "xattr", "normalized", "tag", "fulltext",
             "publication_attempt", "export_lineage", "snapshot",
             "eviction_intent", "run", "run_event"},
            self.table_names())

    def test_store_meta_carries_the_frontier(self):
        self.assertLessEqual({"store_id", "export_seq", "published_frontier",
                              "created_at"}, set(self.columns("store_meta")))

    def test_occurrence_is_namespaced(self):
        columns = self.columns("occurrence")
        self.assertLessEqual({"occ_id", "origin_store_id", "item_name",
                              "archive_path", "kind", "spool_path", "root_sha256",
                              "size_bytes", "entry_count", "state",
                              "confirmed_attempt_id", "dedup_of", "last_error"},
                             set(columns))

    def test_entry_carries_capture_status(self):
        self.assertIn("capture_status", self.columns("entry"))

    def test_fingerprint_carries_every_gate_field(self):
        self.assertLessEqual(
            {"occ_id", "rel_path", "mtime_ns", "ctime_ns", "inode", "dev",
             "size_bytes", "entry_type", "link_target"},
            set(self.columns("fingerprint")))

    def test_publication_attempt_shape(self):
        self.assertLessEqual(
            {"attempt_id", "origin_store_id", "occ_id", "export_seq",
             "export_sha256", "export_path", "started_at", "snapshot_id",
             "outcome", "reason", "finished_at"},
            set(self.columns("publication_attempt")))

    def test_export_lineage_is_fully_defined(self):
        self.assertLessEqual({"store_id", "export_seq", "occ_id", "attempt_id",
                              "exported_at"}, set(self.columns("export_lineage")))

    def test_export_lineage_carries_no_self_digest(self):
        # A whole-file digest inside the file it describes is self-referential.
        self.assertNotIn("catalog_sha256", self.columns("export_lineage"))

    def test_snapshot_observation_ledger_shape(self):
        self.assertLessEqual(
            {"snapshot_id", "occ_id", "attempt_id", "store_id", "export_seq",
             "kind", "catalog_sha256", "tag_set_json", "status", "reason",
             "seen_at"},
            set(self.columns("snapshot")))

    def test_eviction_intent_marks_recovery_reconstruction(self):
        self.assertIn("recovered_without_local_history",
                      self.columns("eviction_intent"))

    def test_normalized_carries_the_fts_columns(self):
        self.assertLessEqual({"occ_id", "rel_path", "name", "uti", "kind",
                              "created", "modified", "size_bytes", "sha256",
                              "comment", "text"},
                             set(self.columns("normalized")))

    def test_run_tables_shape(self):
        self.assertLessEqual({"run_id", "verb", "started_at", "writer_lock_pid",
                              "cache_evicted"}, set(self.columns("run")))
        self.assertLessEqual({"run_id", "outcome"}, set(self.columns("run_event")))


class ConstraintTest(SchemaTestCase):
    def seed_occurrence(self, occ_id: str, store: str) -> None:
        self.db.execute(
            "INSERT INTO occurrence (occ_id, origin_store_id, item_name,"
            " archive_path, kind, spool_path, root_sha256, size_bytes,"
            " entry_count, state, recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (occ_id, store, "x.txt", f"{occ_id}/x.txt", "file", "/drop/x.txt",
             "b" * 64, 1, 1, "recorded", "2026-09-06T00:00:00Z"))

    def seed_attempt(self, attempt_id, occ_id, store, seq, outcome="pending"):
        self.db.execute(
            "INSERT INTO publication_attempt (attempt_id, origin_store_id,"
            " occ_id, export_seq, export_path, started_at, outcome)"
            " VALUES (?,?,?,?,?,?,?)",
            (attempt_id, store, occ_id, seq, "/state/export/x.sqlite",
             "2026-09-06T00:00:00Z", outcome))

    def test_two_stores_may_each_hold_sequence_one(self):
        store_a, store_b = "a" * 32, "c" * 32
        occ_a = f"{store_a}.01J0000000000000000000000A"
        occ_b = f"{store_b}.01J0000000000000000000000B"
        self.seed_occurrence(occ_a, store_a)
        self.seed_occurrence(occ_b, store_b)
        self.seed_attempt(f"{store_a}.01J000000000000000000000AA", occ_a, store_a, 1)
        self.seed_attempt(f"{store_b}.01J000000000000000000000BB", occ_b, store_b, 1)
        self.db.commit()
        self.assertEqual(self.db.execute(
            "SELECT count(*) FROM publication_attempt WHERE export_seq=1"
        ).fetchone()[0], 2)

    def test_duplicate_sequence_within_one_store_is_rejected(self):
        store = "a" * 32
        occ = f"{store}.01J0000000000000000000000A"
        self.seed_occurrence(occ, store)
        self.seed_attempt(f"{store}.01J000000000000000000000AA", occ, store, 1)
        with self.assertRaises(sqlite3.IntegrityError):
            self.seed_attempt(f"{store}.01J000000000000000000000AB", occ, store, 1)

    def test_only_one_confirmed_attempt_per_occurrence(self):
        store = "a" * 32
        occ = f"{store}.01J0000000000000000000000A"
        self.seed_occurrence(occ, store)
        self.seed_attempt(f"{store}.01J000000000000000000000AA", occ, store, 1,
                          outcome="confirmed")
        with self.assertRaises(sqlite3.IntegrityError):
            self.seed_attempt(f"{store}.01J000000000000000000000AB", occ, store, 2,
                              outcome="confirmed")

    def test_foreign_keys_are_enforced(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute(
                "INSERT INTO entry (occ_id, rel_path, entry_type, mode,"
                " archive_path, searchable, capture_status)"
                " VALUES ('missing', '', 'file', 33188, 'missing/x', 1, 'ok')")

    def test_archive_path_is_unique(self):
        store = "a" * 32
        first = f"{store}.01J0000000000000000000000A"
        second = f"{store}.01J0000000000000000000000B"
        self.seed_occurrence(first, store)
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute(
                "INSERT INTO occurrence (occ_id, origin_store_id, item_name,"
                " archive_path, kind, spool_path, root_sha256, size_bytes,"
                " entry_count, state, recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (second, store, "x.txt", f"{first}/x.txt", "file", "/drop/x.txt",
                 "b" * 64, 1, 1, "recorded", "2026-09-06T00:00:00Z"))


class FullTextTest(SchemaTestCase):
    def seed_entry(self, occ="o1", rel="", name="notes.txt", text="alpha beta",
                   comment=None):
        store = "a" * 32
        occ_id = f"{store}.01J000000000000000000000{occ[-1]}A"[:59]
        self.db.execute(
            "INSERT OR IGNORE INTO occurrence (occ_id, origin_store_id,"
            " item_name, archive_path, kind, spool_path, root_sha256,"
            " size_bytes, entry_count, state, recorded_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (occ_id, store, name, f"{occ_id}/{name}", "file", "/drop/" + name,
             "b" * 64, 1, 1, "recorded", "2026-09-06T00:00:00Z"))
        self.db.execute(
            "INSERT INTO entry (occ_id, rel_path, entry_type, mode,"
            " archive_path, searchable, capture_status)"
            " VALUES (?,?,?,?,?,?,?)",
            (occ_id, rel, "file", 33188, f"{occ_id}/{name}", 1, "ok"))
        self.db.execute(
            "INSERT INTO normalized (occ_id, rel_path, name, text, comment)"
            " VALUES (?,?,?,?,?)", (occ_id, rel, name, text, comment))
        self.db.commit()
        return occ_id

    def matches(self, query: str) -> list[str]:
        return [row["name"] for row in self.db.execute(
            "SELECT n.name FROM fulltext f JOIN normalized n"
            " ON n.rowid = f.rowid WHERE fulltext MATCH ?", (query,))]

    def test_external_content_exposes_name_comment_and_text(self):
        self.seed_entry(name="notes.txt", text="alpha beta", comment="tax stuff")
        self.assertEqual(self.matches("alpha"), ["notes.txt"])
        self.assertEqual(self.matches("tax"), ["notes.txt"])
        self.assertEqual(self.matches("notes"), ["notes.txt"])

    def test_insert_update_delete_keep_fts_consistent(self):
        occ_id = self.seed_entry(text="alpha")
        self.assertEqual(self.matches("alpha"), ["notes.txt"])
        self.db.execute("UPDATE normalized SET text='gamma' WHERE occ_id=?",
                        (occ_id,))
        self.db.commit()
        self.assertEqual(self.matches("alpha"), [])
        self.assertEqual(self.matches("gamma"), ["notes.txt"])
        self.db.execute("DELETE FROM normalized WHERE occ_id=?", (occ_id,))
        self.db.commit()
        self.assertEqual(self.matches("gamma"), [])

    def test_rebuild_reconstructs_the_index(self):
        self.seed_entry(text="delta")
        self.db.execute("INSERT INTO fulltext(fulltext) VALUES('rebuild')")
        self.db.commit()
        self.assertEqual(self.matches("delta"), ["notes.txt"])

    def test_a_recovered_catalog_reopens_with_working_fts(self):
        self.seed_entry(text="epsilon")
        copy_path = self.root / "recovered.sqlite"
        with closing(sqlite3.connect(copy_path)) as target, target:
            self.db.backup(target)
        recovered = connect(copy_path)
        self.addCleanup(recovered.close)
        rows = list(recovered.execute(
            "SELECT n.name FROM fulltext f JOIN normalized n"
            " ON n.rowid = f.rowid WHERE fulltext MATCH 'epsilon'"))
        self.assertEqual([row["name"] for row in rows], ["notes.txt"])

    def test_diacritics_are_folded(self):
        self.seed_entry(name="cafe.txt", text="café")
        self.assertEqual(self.matches("cafe"), ["cafe.txt"])


class TransactionHelperTest(SchemaTestCase):
    def test_commits_on_success(self):
        with transaction(self.db):
            self.db.execute(
                "INSERT INTO store_meta (store_id, export_seq,"
                " published_frontier, created_at) VALUES (?,0,0,?)",
                ("a" * 32, "2026-09-06T00:00:00Z"))
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM store_meta").fetchone()[0], 1)

    def test_rolls_back_on_error(self):
        with self.assertRaises(RuntimeError):
            with transaction(self.db):
                self.db.execute(
                    "INSERT INTO store_meta (store_id, export_seq,"
                    " published_frontier, created_at) VALUES (?,0,0,?)",
                    ("a" * 32, "2026-09-06T00:00:00Z"))
                raise RuntimeError("boom")
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM store_meta").fetchone()[0], 0)

    def test_rows_are_mappings(self):
        row = self.db.execute("SELECT 1 AS one").fetchone()
        self.assertEqual(row["one"], 1)
