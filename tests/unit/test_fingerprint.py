"""Source-stability gates: any difference is a refusal.

The comparison runs against stored rows, so it is exercised through a real
store; the gate itself is the unit under test.
"""

from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
import tempfile
import tracemalloc
import unittest

from dropin.pipeline.fingerprint import SourceChanged, compare, capture
from dropin.spool.walk import walk
from dropin.store.db import connect

STORE = "a" * 32
OCC = f"{STORE}.01J0000000000000000000000A"


class FingerprintTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dropin-fp-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = connect(self.root / "store.sqlite")
        self.addCleanup(self.db.close)
        self.item = self.root / "tree"
        self.item.mkdir()
        (self.item / "a.txt").write_bytes(b"alpha")
        (self.item / "sub").mkdir()
        (self.item / "sub" / "b.txt").write_bytes(b"beta")
        (self.item / "link").symlink_to("a.txt")
        self.seed()

    def seed(self):
        self.db.execute(
            "INSERT INTO occurrence (occ_id, origin_store_id, item_name,"
            " archive_path, kind, spool_path, root_sha256, size_bytes,"
            " entry_count, state, recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (OCC, STORE, "tree", f"{OCC}/tree", "dir", str(self.item),
             "b" * 64, 9, 3, "recorded", "2026-09-06T00:00:00Z"))
        for entry in walk(self.item):
            self.db.execute(
                "INSERT INTO entry (occ_id, rel_path, entry_type, size_bytes,"
                " mode, archive_path, searchable, capture_status)"
                " VALUES (?,?,?,?,?,?,1,'ok')",
                (OCC, entry.rel_path, entry.entry_type, entry.size_bytes,
                 entry.mode, f"{OCC}/tree/{entry.rel_path}".rstrip("/")))
        capture(self.db, OCC, walk(self.item))
        self.db.commit()

    def gate(self):
        compare(self.db, OCC, walk(self.item))


class UnchangedTest(FingerprintTestCase):
    def test_unchanged_tree_passes(self):
        self.gate()

    def test_repeated_gates_pass(self):
        for _ in range(3):
            self.gate()


class ChangeDetectionTest(FingerprintTestCase):
    def test_descendant_change_with_unchanged_root_metadata(self):
        # Rewriting a file in place leaves every directory above it untouched,
        # so a root-only check sees nothing at all.
        target = self.item / "sub" / "b.txt"
        before = {p: (p.stat().st_mtime_ns, p.stat().st_ctime_ns)
                  for p in (self.item, self.item / "sub")}
        target.write_bytes(b"BETA")
        after = {p: (p.stat().st_mtime_ns, p.stat().st_ctime_ns)
                 for p in (self.item, self.item / "sub")}
        self.assertEqual(before, after, "the directories must look unchanged")
        with self.assertRaises(SourceChanged) as caught:
            self.gate()
        self.assertEqual(caught.exception.rel_path, "sub/b.txt")

    def test_same_size_same_mtime_replacement_with_a_new_inode(self):
        target = self.item / "a.txt"
        original = target.stat()
        replacement = self.root / "replacement"
        replacement.write_bytes(b"ALPHA")  # same length
        replacement.replace(target)
        # Nanosecond precision matters: float utime rounds and the comparison
        # would then differ for an uninteresting reason.
        os.utime(target, ns=(original.st_atime_ns, original.st_mtime_ns))
        # Size and mtime are identical to the recorded ones; the inode is not.
        self.assertEqual(target.stat().st_size, original.st_size)
        self.assertEqual(target.stat().st_mtime_ns, original.st_mtime_ns)
        self.assertNotEqual(target.stat().st_ino, original.st_ino)
        with self.assertRaises(SourceChanged) as caught:
            self.gate()
        # A rename also moves the parent directory's timestamps, so either the
        # directory or the entry may be named first; both are the same refusal.
        self.assertIn(caught.exception.rel_path, ("", "a.txt"))

    def test_inode_swap_is_caught_by_the_entry_row_itself(self):
        # Compare directly against a live walk whose only difference is the
        # inode, so nothing above the entry can be what raises.
        entries = []
        for entry in walk(self.item):
            if entry.rel_path == "a.txt":
                entry = replace(entry, inode=entry.inode + 1)
            entries.append(entry)
        with self.assertRaises(SourceChanged) as caught:
            compare(self.db, OCC, iter(entries))
        self.assertEqual(caught.exception.rel_path, "a.txt")
        self.assertIn("inode", caught.exception.detail)

    def test_metadata_only_change_is_still_a_change(self):
        # ctime moves even when content and mtime do not.
        (self.item / "a.txt").chmod(0o600)
        with self.assertRaises(SourceChanged):
            self.gate()

    def test_added_entry(self):
        (self.item / "sub" / "new.txt").write_bytes(b"new")
        with self.assertRaises(SourceChanged):
            self.gate()

    def test_added_entry_is_named(self):
        entries = list(walk(self.item))
        entries.append(replace(entries[1], rel_path="sub/new.txt"))
        with self.assertRaises(SourceChanged) as caught:
            compare(self.db, OCC, iter(entries))
        self.assertEqual(caught.exception.rel_path, "sub/new.txt")
        self.assertEqual(caught.exception.detail, "entry added")

    def test_removed_entry(self):
        (self.item / "a.txt").unlink()
        with self.assertRaises(SourceChanged):
            self.gate()

    def test_removed_entry_is_named(self):
        entries = [e for e in walk(self.item) if e.rel_path != "a.txt"]
        with self.assertRaises(SourceChanged) as caught:
            compare(self.db, OCC, iter(entries))
        self.assertEqual(caught.exception.rel_path, "a.txt")
        self.assertEqual(caught.exception.detail, "entry removed")

    def test_type_change(self):
        target = self.item / "a.txt"
        target.unlink()
        target.symlink_to("sub/b.txt")
        with self.assertRaises(SourceChanged):
            self.gate()

    def test_symlink_retarget(self):
        entries = []
        for entry in walk(self.item):
            if entry.rel_path == "link":
                entry = replace(entry, link_target="sub/b.txt")
            entries.append(entry)
        with self.assertRaises(SourceChanged) as caught:
            compare(self.db, OCC, iter(entries))
        self.assertEqual(caught.exception.rel_path, "link")
        self.assertIn("link_target", caught.exception.detail)

    def test_root_replaced_by_a_different_directory(self):
        import shutil

        shutil.rmtree(self.item)
        self.item.mkdir()
        (self.item / "a.txt").write_bytes(b"alpha")
        (self.item / "sub").mkdir()
        (self.item / "sub" / "b.txt").write_bytes(b"beta")
        (self.item / "link").symlink_to("a.txt")
        with self.assertRaises(SourceChanged):
            self.gate()

    def test_vanished_root(self):
        import shutil

        shutil.rmtree(self.item)
        with self.assertRaises(SourceChanged):
            self.gate()


class CursorMemoryTest(unittest.TestCase):
    """The comparison must iterate, not materialise the entry list."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dropin-fp-big-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = connect(self.root / "store.sqlite")
        self.addCleanup(self.db.close)
        self.item = self.root / "big"
        self.item.mkdir()
        for index in range(4000):
            (self.item / f"f{index:05d}").write_bytes(b"x")
        self.db.execute(
            "INSERT INTO occurrence (occ_id, origin_store_id, item_name,"
            " archive_path, kind, spool_path, root_sha256, size_bytes,"
            " entry_count, state, recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (OCC, STORE, "big", f"{OCC}/big", "dir", str(self.item),
             "b" * 64, 4000, 4001, "recorded", "2026-09-06T00:00:00Z"))
        for entry in walk(self.item):
            self.db.execute(
                "INSERT INTO entry (occ_id, rel_path, entry_type, size_bytes,"
                " mode, archive_path, searchable, capture_status)"
                " VALUES (?,?,?,?,?,?,1,'ok')",
                (OCC, entry.rel_path, entry.entry_type, entry.size_bytes,
                 entry.mode, f"{OCC}/big/{entry.rel_path}".rstrip("/")))
        capture(self.db, OCC, walk(self.item))
        self.db.commit()

    def test_peak_memory_stays_far_below_the_entry_count(self):
        tracemalloc.start()
        compare(self.db, OCC, walk(self.item))
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        self.assertLess(peak, 1024 * 1024,
                        "the gate materialised the entry set")
