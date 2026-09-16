"""Full records, confirmed state scope, immutable local reads."""

from unittest.mock import patch

from dropin.query.filters import Filters
from dropin.query.search import find, show, ls, read_store
from dropin.config import load
from tests.query_support import QueryTestCase


class QuerySearchTest(QueryTestCase):
    def test_full_show_path_and_hash_preserve_all_attribute_sources(self):
        row = show(self.db, archive_path=self.paths["Tax-March.PDF"])
        self.assertEqual(row["attributes"]["overlap"], {"value": "spotlight", "type": "string", "source": "mdls"})
        self.assertEqual([v for v in row["attribute_values"] if v["key"] == "overlap"], [
            {"key": "overlap", "value": "spotlight", "type": "string", "source": "mdls"},
            {"key": "overlap", "value": "importer", "type": "string", "source": "importer"}])
        self.assertEqual(row["attributes"]["importerOnly"]["value"], {"nested": [1, True, None]})
        self.assertEqual(row["text"], "orchid invoice")
        self.assertEqual(row["kind"], "PDF document")
        self.assertEqual(row["entry"]["capture_status"], "ok")
        self.assertEqual(row["normalized"]["name"], "Tax-March.PDF")
        self.assertEqual({x["name"]: x["status"] for x in row["xattrs"]},
                         {"user.ok": "ok", "user.unreadable": "skipped:permission"})
        self.assertEqual(show(self.db, sha256=self.hashes["Tax-March.PDF"]), [row])
        self.assertEqual(show(self.db, sha256="f" * 64), [])
        with self.assertRaises(LookupError):
            show(self.db, archive_path="unknown/path")

    def test_find_full_and_compact_share_values_and_optional_attributes(self):
        compact = list(find(self.db, Filters(name="Notes")))[0]
        full = list(find(self.db, Filters(name="Notes"), full=True))[0]
        self.assertEqual(set(compact), {"archive_path", "name", "kind", "uti", "created", "modified",
                                        "size", "sha256", "tags", "comment", "state", "has_text"})
        for key, value in compact.items():
            self.assertEqual(full[key], value)
        self.assertNotIn("attributes", compact)
        self.assertNotIn("attribute_values", compact)

    def test_confirmed_scope_including_gate_d_abandoned_and_operational_ls(self):
        visible = set(self.paths.values())
        for state in ("recorded", "transferred", "verified", "abandoned"):
            name = "pre-" + state
            self.seed(name, state=state, confirmed=False)
            self.assertEqual(show(self.db, sha256=self.hashes[name]), [])
            with self.assertRaises(LookupError):
                show(self.db, archive_path=self.paths[name])
            self.assertIn(self.paths[name], {r["archive_path"] for r in ls(self.db, state=state)})
        for state in ("recoverable", "evicting", "evicted", "abandoned"):
            name = "post-" + state
            self.seed(name, state=state)
            visible.add(self.paths[name])
        self.assertEqual({r["archive_path"] for r in find(self.db, Filters())}, visible)
        self.assertEqual({r["archive_path"] for r in ls(self.db)}, visible)
        for path in visible:
            row = show(self.db, archive_path=path)
            self.assertIsNotNone(self.db.execute("SELECT * FROM snapshot WHERE snapshot_id=? AND status='confirmed'",
                                                 (row["snapshot"],)).fetchone())

    def test_plain_tree_descendants_searchable_bundle_internals_resolve_to_root(self):
        self.seed("tree", kind="dir", children=("child.txt", "other.txt"))
        self.seed("bundle.app", kind="bundle", children=("child.txt",))
        self.assertEqual({r["archive_path"] for r in find(self.db, Filters(name="child"))},
                         {self.paths["tree"] + "/child.txt"})
        bundle = show(self.db, archive_path=self.paths["bundle.app"] + "/child.txt")
        self.assertEqual(bundle["archive_path"], self.paths["bundle.app"])
        self.assertEqual([e["rel_path"] for e in bundle["manifest"]], ["", "child.txt"])
        tree = show(self.db, archive_path=self.paths["tree"])
        self.assertEqual([e["rel_path"] for e in tree["manifest"]], ["", "child.txt", "other.txt"])
        self.assertEqual(len(show(self.db, sha256=self.hashes["tree"])), 2)

    def test_listing_is_occurrences_not_descendants_and_since_is_recorded_date(self):
        self.seed("tree", kind="dir", children=("child.txt",))
        self.assertEqual(len(list(ls(self.db))), 13)
        self.assertEqual(len(list(ls(self.db, since="2026-05-01", limit=2))), 2)
        self.assertEqual(list(ls(self.db, since="2026-05-02")), [])

    def test_read_store_is_readonly_and_never_constructs_engine_or_locks(self):
        import sqlite3
        with patch("dropin.engine.restic.ResticEngine", side_effect=AssertionError("engine")), \
             patch("dropin.engine.fake.FakeEngine", side_effect=AssertionError("fake engine")), \
             patch("dropin.cli.tools_gate", side_effect=AssertionError("tools")), \
             patch("dropin.pipeline.writer_lock.writer_lock", side_effect=AssertionError("lock")):
            with read_store(load(self.config_path)) as db:
                self.assertEqual(len(list(find(db, Filters()))), 12)
                self.assertEqual(len(list(ls(db))), 12)
                show(db, archive_path=self.paths["Notes.txt"])
                with self.assertRaises(sqlite3.OperationalError):
                    db.execute("UPDATE normalized SET name='changed'")
        self.assertFalse((self.temp.state_dir / "writer.lock").exists())

    def test_multi_statement_record_reads_share_a_snapshot_during_writer_commit(self):
        with read_store(load(self.config_path)) as reader:
            original = list(find(reader, Filters(), full=True))
            # A writer can commit during a read transaction; no writer lock needed.
            self.db.execute("UPDATE normalized SET comment='later'")
            self.assertEqual(list(find(reader, Filters(), full=True)), original)
        with read_store(load(self.config_path)) as reader:
            self.assertEqual(show(reader, archive_path=self.paths["Notes.txt"])["comment"], "later")

    def test_hash_show_has_no_default_limit(self):
        for i in range(101):
            self.seed(f"repeat-{i}")
        self.db.execute("UPDATE normalized SET sha256=?", ("e" * 64,))
        self.assertEqual(len(show(self.db, sha256="e" * 64)), 113)
