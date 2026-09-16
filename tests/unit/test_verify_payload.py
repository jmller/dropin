"""Payload verification: a data read, never a structure check."""

from __future__ import annotations

import hashlib
from pathlib import Path
import tempfile
import tracemalloc
import unittest

from dropin.capture.extract import capture_item
from dropin.engine.fake import FakeEngine
from dropin.macos.fake import FakeMacOS
from dropin.pipeline.verify import PayloadError, verify_payload
from dropin.store import records
from dropin.store.db import connect

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "synthetic"


class PayloadTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dropin-payload-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.drop = self.root / "drop"
        self.drop.mkdir()
        self.db = connect(self.root / "store.sqlite")
        self.addCleanup(self.db.close)
        self.store_id = records.initialise_store(self.db)
        self.macos = FakeMacOS()
        self.engine = FakeEngine()
        self.engine.init()

    def configure(self, path: Path) -> None:
        for child in [path, *(path.rglob("*") if path.is_dir() else [])]:
            self.macos.set_mdls(str(child),
                                (FIXTURES / "mdls" / "text_plain.txt").read_text())
            self.macos.set_importer(
                str(child), (FIXTURES / "mdimport" / "no_text.txt").read_text())

    def record(self, path: Path) -> str:
        self.configure(path)
        return records.record_occurrence(self.db, capture_item(self.macos, path),
                                         self.store_id)

    def stage(self, path: Path) -> None:
        children = [path, *(path.rglob("*") if path.is_dir() else [])]
        for child in children:
            if child.is_symlink():
                self.engine.add_source_symlink(str(child), str(child.readlink()))
            elif child.is_dir():
                self.engine.add_source_dir(str(child))
            else:
                self.engine.add_source_file(str(child), child.read_bytes())

    def publish(self, path: Path) -> str:
        self.stage(path)
        return self.engine.backup((str(path),), ("t",)).snapshot_id

    def verify(self, occ_id: str, snapshot_id: str, path: Path) -> None:
        verify_payload(self.engine, snapshot_id, occ_id, self.db, str(path))


class FileTest(PayloadTestCase):
    def setUp(self):
        super().setUp()
        self.path = self.drop / "solo.bin"
        self.path.write_bytes(b"payload bytes")
        self.occ_id = self.record(self.path)
        self.snapshot = self.publish(self.path)

    def test_matching_hash_passes(self):
        self.verify(self.occ_id, self.snapshot, self.path)

    def test_corrupt_payload_is_reported(self):
        self.engine.inject_corruption(self.snapshot, str(self.path))
        with self.assertRaises(PayloadError) as caught:
            self.verify(self.occ_id, self.snapshot, self.path)
        self.assertEqual(caught.exception.rel_path, "")

    def test_flipped_bytes_fail_the_hash_comparison(self):
        self.engine.inject_corruption(self.snapshot, str(self.path),
                                      flip_byte=True)
        with self.assertRaises(PayloadError) as caught:
            self.verify(self.occ_id, self.snapshot, self.path)
        self.assertIn("hash", str(caught.exception))

    def test_missing_payload_is_reported(self):
        self.engine.drop_export(self.snapshot)  # removes .sqlite only
        self.engine.inject_corruption(self.snapshot, str(self.path))
        with self.assertRaises(PayloadError):
            self.verify(self.occ_id, self.snapshot, self.path)


class TreeTest(PayloadTestCase):
    def setUp(self):
        super().setUp()
        self.tree = self.drop / "tree"
        (self.tree / "sub").mkdir(parents=True)
        (self.tree / "a.txt").write_bytes(b"alpha")
        (self.tree / "sub" / "b.txt").write_bytes(b"beta")
        (self.tree / "link").symlink_to("a.txt")
        (self.tree / "empty").mkdir()
        self.occ_id = self.record(self.tree)
        self.snapshot = self.publish(self.tree)

    def test_intact_tree_passes(self):
        self.verify(self.occ_id, self.snapshot, self.tree)

    def test_member_hash_mismatch_names_the_entry(self):
        self.engine.inject_corruption(self.snapshot, str(self.tree / "sub" / "b.txt"),
                                      flip_byte=True)
        with self.assertRaises(PayloadError) as caught:
            self.verify(self.occ_id, self.snapshot, self.tree)
        self.assertEqual(caught.exception.rel_path, "sub/b.txt")

    def test_symlink_target_is_compared_here(self):
        # The only place link targets are proven: ls exposes none.
        self.engine.add_source_symlink(str(self.tree / "link"), "sub/b.txt")
        snapshot = self.engine.backup((str(self.tree),), ("t",)).snapshot_id
        with self.assertRaises(PayloadError) as caught:
            self.verify(self.occ_id, snapshot, self.tree)
        self.assertEqual(caught.exception.rel_path, "link")
        self.assertIn("link target", str(caught.exception))

    def test_truncated_stream_is_corrupt(self):
        self.engine.inject_truncation(self.snapshot)
        with self.assertRaises(PayloadError) as caught:
            self.verify(self.occ_id, self.snapshot, self.tree)
        self.assertIn("stream", str(caught.exception).lower())

    def test_member_set_must_equal_the_manifest(self):
        # A truncation that lands on a member boundary parses cleanly; only
        # member-set equality catches it.
        self.engine.exit3_on_next_backup(omit=str(self.tree / "sub" / "b.txt"))
        snapshot = self.engine.backup((str(self.tree),), ("t",)).snapshot_id
        with self.assertRaises(PayloadError) as caught:
            self.verify(self.occ_id, snapshot, self.tree)
        self.assertIn("sub/b.txt", str(caught.exception))

    def test_extra_member_is_refused(self):
        self.engine.add_source_file(str(self.tree / "extra.txt"), b"extra")
        snapshot = self.engine.backup((str(self.tree),), ("t",)).snapshot_id
        with self.assertRaises(PayloadError) as caught:
            self.verify(self.occ_id, snapshot, self.tree)
        self.assertEqual(caught.exception.rel_path, "extra.txt")

    def test_manifest_hash_is_recomputed_from_what_was_read(self):
        from dropin.pipeline import verify as module

        self.assertTrue(hasattr(module, "recompute_manifest_hash"))
        digest = module.recompute_manifest_hash(self.db, self.occ_id)
        self.assertEqual(
            digest, records.get_occurrence(self.db, self.occ_id)["root_sha256"])


class AdversarialMemberTest(PayloadTestCase):
    """Member names from the stream are never trusted as paths."""

    def setUp(self):
        super().setUp()
        self.tree = self.drop / "tree"
        self.tree.mkdir()
        (self.tree / "a.txt").write_bytes(b"alpha")
        self.occ_id = self.record(self.tree)

    def snapshot_with_member(self, name: str) -> str:
        self.stage(self.tree)
        snapshot = self.engine.backup((str(self.tree),), ("t",)).snapshot_id
        self.engine.rewrite_tar_member(snapshot, str(self.tree / "a.txt"), name)
        return snapshot

    def test_absolute_member_is_refused(self):
        snapshot = self.snapshot_with_member("/etc/passwd")
        with self.assertRaises(PayloadError):
            self.verify(self.occ_id, snapshot, self.tree)

    def test_parent_traversal_member_is_refused(self):
        snapshot = self.snapshot_with_member("../../escape")
        with self.assertRaises(PayloadError):
            self.verify(self.occ_id, snapshot, self.tree)

    def test_duplicate_member_is_refused(self):
        self.stage(self.tree)
        snapshot = self.engine.backup((str(self.tree),), ("t",)).snapshot_id
        self.engine.duplicate_tar_member(snapshot, str(self.tree / "a.txt"))
        with self.assertRaises(PayloadError) as caught:
            self.verify(self.occ_id, snapshot, self.tree)
        self.assertIn("duplicate", str(caught.exception))


class MemoryTest(PayloadTestCase):
    """Peak memory follows the largest member, not the entry count."""

    def test_twenty_thousand_tiny_files(self):
        tree = self.drop / "big"
        tree.mkdir()
        for index in range(20_000):
            (tree / f"f{index:05d}").write_bytes(b"x")
        occ_id = self.record(tree)
        snapshot = self.publish(tree)

        tracemalloc.start()
        self.verify(occ_id, snapshot, tree)
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        # Generous but bounded: a per-member accumulation would be far larger.
        self.assertLess(peak, 32 * 1024 * 1024)
