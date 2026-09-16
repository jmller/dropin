"""Spool scan, admission, preflight walk, and streaming hashes."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from dropin.spool.admission import admit
from dropin.spool.hashing import sha256_stream
from dropin.spool.scan import scan
from dropin.spool.walk import SpecialEntry, walk


class SpoolTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dropin-spool-")
        self.addCleanup(self.temp.cleanup)
        self.drop = Path(self.temp.name)

    def file(self, name: str, content: bytes = b"x") -> Path:
        path = self.drop / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path


class ScanTest(SpoolTestCase):
    def test_top_level_entries_only(self):
        self.file("alpha.txt")
        self.file("tree/inner.txt")
        self.assertEqual([p.name for p in scan(self.drop)], ["alpha.txt", "tree"])

    def test_byte_order_not_locale_order(self):
        # Distinct even on case-insensitive filesystems; still tests byte order.
        for name in ("b.txt", "A.txt", "a2.txt", "B2.txt"):
            self.file(name)
        self.assertEqual([p.name for p in scan(self.drop)],
                         ["A.txt", "B2.txt", "a2.txt", "b.txt"])

    def test_ignores_ds_store_and_our_own_scratch(self):
        self.file(".DS_Store")
        self.file(".dropin-restore-01J/inner")
        self.file("keep.txt")
        self.assertEqual([p.name for p in scan(self.drop)], ["keep.txt"])

    @unittest.skipIf(sys.platform == "darwin", "macOS filesystem rejects invalid UTF-8 names")
    def test_non_utf8_name_survives(self):
        raw = os.fsdecode(b"caf\xe9.txt")
        self.file(raw)
        self.assertEqual([p.name for p in scan(self.drop)], [raw])

    def test_empty_spool(self):
        self.assertEqual(list(scan(self.drop)), [])


class AdmissionTest(SpoolTestCase):
    """Admission is quiescence only; it grants no ownership."""

    def setUp(self):
        super().setUp()
        self.now = 1_000_000.0
        self.clock = mock.patch("dropin.spool.admission.now",
                                side_effect=lambda: self.now)
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def age(self, path: Path, seconds: float) -> None:
        stamp = self.now - seconds
        os.utime(path, (stamp, stamp))

    def test_young_root_is_not_admitted(self):
        path = self.file("fresh.txt")
        self.age(path, 1)
        self.assertFalse(admit(path, settle_seconds=5, sample_gap_seconds=0))

    def test_settled_file_is_admitted(self):
        path = self.file("settled.txt")
        self.age(path, 60)
        self.assertTrue(admit(path, settle_seconds=5, sample_gap_seconds=0))

    def test_directory_with_a_young_descendant_is_not_admitted(self):
        inner = self.file("tree/inner.txt")
        self.age(self.drop / "tree", 60)
        self.age(inner, 1)
        self.assertFalse(admit(self.drop / "tree", settle_seconds=5,
                               sample_gap_seconds=0))

    def test_change_between_the_two_samples_is_not_admitted(self):
        path = self.file("changing.txt")
        self.age(path, 60)

        def grow(_seconds):
            path.write_bytes(b"more content")
            self.age(path, 60)

        with mock.patch("dropin.spool.admission.sleep", side_effect=grow):
            self.assertFalse(admit(path, settle_seconds=5, sample_gap_seconds=2))

    def test_stable_across_both_samples_is_admitted(self):
        path = self.file("stable.txt")
        self.age(path, 60)
        with mock.patch("dropin.spool.admission.sleep"):
            self.assertTrue(admit(path, settle_seconds=5, sample_gap_seconds=2))

    def test_vanished_item_is_not_admitted(self):
        path = self.file("gone.txt")
        self.age(path, 60)
        path.unlink()
        self.assertFalse(admit(path, settle_seconds=5, sample_gap_seconds=0))


class WalkTest(SpoolTestCase):
    def test_file_item_yields_one_root_entry(self):
        path = self.file("solo.txt", b"12345")
        entries = list(walk(path))
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry.rel_path, "")
        self.assertEqual(entry.entry_type, "file")
        self.assertEqual(entry.size_bytes, 5)

    def test_entries_carry_every_fingerprint_field(self):
        path = self.file("solo.txt", b"12345")
        entry = next(iter(walk(path)))
        for field in ("entry_type", "size_bytes", "mtime_ns", "ctime_ns",
                      "inode", "dev", "link_target"):
            with self.subTest(field=field):
                self.assertTrue(hasattr(entry, field))
        self.assertIsNone(entry.link_target)
        self.assertGreater(entry.inode, 0)

    def test_tree_entries_are_relative_and_ordered_by_bytes(self):
        self.file("tree/b.txt")
        self.file("tree/a.txt")
        self.file("tree/sub/c.txt")
        (self.drop / "tree" / "empty").mkdir()
        rel = [e.rel_path for e in walk(self.drop / "tree")]
        self.assertEqual(rel, sorted(rel))
        self.assertEqual(rel[0], "")
        self.assertIn("a.txt", rel)
        self.assertIn("sub/c.txt", rel)
        self.assertIn("empty", rel)

    def test_symlinks_are_recorded_and_never_followed(self):
        outside = Path(self.temp.name) / "outside.txt"
        outside.write_bytes(b"must not be traversed")
        tree = self.drop / "tree"
        tree.mkdir()
        (tree / "link").symlink_to(outside)
        entries = {e.rel_path: e for e in walk(tree)}
        self.assertEqual(entries["link"].entry_type, "symlink")
        self.assertEqual(entries["link"].link_target, str(outside))
        self.assertNotIn("outside.txt", entries)

    def test_symlink_to_a_directory_is_not_descended(self):
        target = self.drop / "target"
        target.mkdir()
        (target / "deep.txt").write_bytes(b"x")
        tree = self.drop / "tree"
        tree.mkdir()
        (tree / "link").symlink_to(target)
        rel = [e.rel_path for e in walk(tree)]
        self.assertEqual(sorted(rel), ["", "link"])

    def test_fifo_refuses_the_whole_tree(self):
        tree = self.drop / "tree"
        tree.mkdir()
        (tree / "keep.txt").write_bytes(b"x")
        os.mkfifo(tree / "pipe")
        with self.assertRaises(SpecialEntry) as caught:
            list(walk(tree))
        self.assertEqual(caught.exception.rel_path, "pipe")
        self.assertEqual(caught.exception.entry_type, "fifo")

    def test_socket_refuses_the_whole_tree(self):
        import socket

        tree = self.drop / "tree"
        tree.mkdir()
        sock = socket.socket(socket.AF_UNIX)
        self.addCleanup(sock.close)
        sock.bind(str(tree / "sock"))
        with self.assertRaises(SpecialEntry) as caught:
            list(walk(tree))
        self.assertEqual(caught.exception.entry_type, "socket")

    def test_walk_is_lazy_so_memory_does_not_track_entry_count(self):
        tree = self.drop / "big"
        tree.mkdir()
        for index in range(200):
            (tree / f"f{index:04d}").write_bytes(b"x")
        entries = walk(tree)
        self.assertFalse(isinstance(entries, list))
        first = next(iter(entries))
        self.assertEqual(first.rel_path, "")


class HashingTest(SpoolTestCase):
    def test_streams_in_one_mib_chunks(self):
        path = self.file("payload.bin", os.urandom(3 * 1024 * 1024 + 17))
        with mock.patch("dropin.spool.hashing.CHUNK_SIZE", 1024 * 1024):
            digest = sha256_stream(path)
        self.assertEqual(digest, hashlib.sha256(path.read_bytes()).hexdigest())

    def test_empty_file(self):
        path = self.file("zero.bin", b"")
        self.assertEqual(sha256_stream(path), hashlib.sha256(b"").hexdigest())

    def test_peak_memory_follows_the_chunk_not_the_file(self):
        import tracemalloc

        from dropin.spool.hashing import CHUNK_SIZE

        peaks = []
        for megabytes in (4, 16):
            path = self.file(f"payload{megabytes}.bin",
                             os.urandom(megabytes * 1024 * 1024))
            tracemalloc.start()
            sha256_stream(path)
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            peaks.append(peak)
        # Quadrupling the file must not move the peak, which stays within a
        # couple of chunks (the read buffer plus the chunk itself).
        for peak in peaks:
            self.assertLess(peak, 3 * CHUNK_SIZE)
        self.assertLess(abs(peaks[1] - peaks[0]), CHUNK_SIZE)
