"""The in-memory repository fake every Linux test runs against."""

from __future__ import annotations

import io
import tarfile
import unittest

from dropin.engine.fake import FakeEngine
from dropin.engine.interface import EngineError

STORE = "a" * 32
OCC = f"{STORE}.01J0000000000000000000000A"
ATTEMPT = f"{STORE}.01J0000000000000000000000B"


def tags(seq=1, kind="dir", digest="c" * 64, occ=OCC, attempt=ATTEMPT):
    return (f"dropin:v=1", f"dropin:store={STORE}", f"dropin:occ={occ}",
            f"dropin:attempt={attempt}", f"dropin:seq={seq}",
            f"dropin:kind={kind}", f"dropin:catalog-sha256={digest}")


class FakeEngineTest(unittest.TestCase):
    def setUp(self):
        self.engine = FakeEngine()
        self.engine.init()
        self.engine.add_source_file("/drop/item/file.txt", b"payload")
        self.engine.add_source_file("/drop/item/sub/deep.bin", b"deep bytes")
        self.engine.add_source_dir("/drop/item/empty")
        self.engine.add_source_symlink("/drop/item/link", "sub/deep.bin")
        self.engine.add_source_file("/state/export/e.sqlite", b"catalog")

    def backup(self, *paths, **kwargs):
        return self.engine.backup(paths or ("/drop/item", "/state/export/e.sqlite"),
                                  kwargs.pop("tags", tags()), **kwargs)

    # ---- backup ------------------------------------------------------------

    def test_backup_returns_a_new_snapshot_id_per_call(self):
        first = self.backup().snapshot_id
        second = self.backup().snapshot_id
        self.assertNotEqual(first, second)
        self.assertEqual(len(first), 64)

    def test_backup_records_tags_and_paths(self):
        result = self.backup()
        [snapshot] = [s for s in self.engine.snapshots() if s.id == result.snapshot_id]
        self.assertEqual(snapshot.tags, tags())
        self.assertEqual(snapshot.paths, ("/drop/item", "/state/export/e.sqlite"))

    def test_exit3_on_next_backup_publishes_a_partial_snapshot(self):
        self.engine.exit3_on_next_backup()
        result = self.backup()
        self.assertEqual(result.exit_code, 3)
        self.assertTrue(result.snapshot_id)
        self.assertEqual(self.backup().exit_code, 0, "the injection is one-shot")

    def test_backup_is_not_idempotent(self):
        self.backup()
        self.backup()
        self.assertEqual(len(self.engine.snapshots()), 2)

    # ---- snapshots ---------------------------------------------------------

    def test_snapshots_are_returned_unsorted_so_callers_must_sort(self):
        ids = [self.backup(tags=tags(seq=seq)).snapshot_id for seq in (1, 2, 3)]
        listed = [s.id for s in self.engine.snapshots()]
        self.assertEqual(sorted(listed), sorted(ids))
        self.assertNotEqual(listed, sorted(listed, key=lambda i: ids.index(i)),
                            "the fake must not hand back time order for free")

    def test_snapshots_filter_by_tag(self):
        first = self.backup(tags=tags(seq=1)).snapshot_id
        other_attempt = f"{STORE}.01J0000000000000000000000C"
        self.backup(tags=tags(seq=2, attempt=other_attempt))
        found = self.engine.snapshots(tag=f"dropin:attempt={ATTEMPT}")
        self.assertEqual([s.id for s in found], [first])

    def test_snapshot_times_are_distinct_and_increasing(self):
        times = [self.engine.snapshot(self.backup(tags=tags(seq=s)).snapshot_id).time
                 for s in (1, 2, 3)]
        self.assertEqual(times, sorted(times))
        self.assertEqual(len(set(times)), 3)

    # ---- ls ----------------------------------------------------------------

    def test_ls_recursive_yields_nodes_including_the_root(self):
        snapshot_id = self.backup().snapshot_id
        nodes = {n.path: n for n in self.engine.ls(snapshot_id, "/drop/item")}
        self.assertEqual(set(nodes), {
            "/drop/item", "/drop/item/empty", "/drop/item/file.txt",
            "/drop/item/link", "/drop/item/sub", "/drop/item/sub/deep.bin"})
        self.assertEqual(nodes["/drop/item"].type, "dir")
        self.assertEqual(nodes["/drop/item/file.txt"].type, "file")
        self.assertEqual(nodes["/drop/item/file.txt"].size, len(b"payload"))
        self.assertEqual(nodes["/drop/item/link"].type, "symlink")

    def test_ls_nodes_expose_no_link_target(self):
        # Matches real restic 0.19.1: targets come from the tar stream.
        snapshot_id = self.backup().snapshot_id
        link = next(n for n in self.engine.ls(snapshot_id, "/drop/item")
                    if n.type == "symlink")
        self.assertFalse(hasattr(link, "linktarget"))
        self.assertIsNone(link.size)

    def test_ls_of_a_partial_snapshot_omits_the_unreadable_entry(self):
        self.engine.exit3_on_next_backup(omit="/drop/item/sub/deep.bin")
        snapshot_id = self.backup().snapshot_id
        paths = {n.path for n in self.engine.ls(snapshot_id, "/drop/item")}
        self.assertNotIn("/drop/item/sub/deep.bin", paths)

    # ---- dump --------------------------------------------------------------

    def test_dump_streams_file_bytes(self):
        snapshot_id = self.backup().snapshot_id
        with self.engine.dump(snapshot_id, "/drop/item/file.txt") as stream:
            self.assertEqual(stream.read(), b"payload")

    def test_dump_tar_streams_the_tree(self):
        snapshot_id = self.backup().snapshot_id
        with self.engine.dump(snapshot_id, "/drop/item", archive="tar") as stream:
            with tarfile.open(fileobj=io.BytesIO(stream.read()), mode="r|") as tar:
                members = {m.name: m for m in tar}
        self.assertIn("drop/item/file.txt", members)
        self.assertIn("drop/item/empty", members)
        link = members["drop/item/link"]
        self.assertTrue(link.issym())
        self.assertEqual(link.linkname, "sub/deep.bin")

    def test_inject_truncation_breaks_the_tar_stream(self):
        snapshot_id = self.backup().snapshot_id
        self.engine.inject_truncation(snapshot_id)
        with self.engine.dump(snapshot_id, "/drop/item", archive="tar") as stream:
            data = stream.read()
        with self.assertRaises(tarfile.TarError):
            with tarfile.open(fileobj=io.BytesIO(data), mode="r|") as tar:
                for _ in tar:
                    pass

    def test_inject_corruption_on_a_payload_path(self):
        snapshot_id = self.backup().snapshot_id
        self.engine.inject_corruption(snapshot_id, "/drop/item/file.txt")
        with self.assertRaises(EngineError) as caught:
            with self.engine.dump(snapshot_id, "/drop/item/file.txt") as stream:
                stream.read()
        self.assertEqual(caught.exception.kind, "corrupt")

    def test_inject_corruption_on_the_export_path(self):
        snapshot_id = self.backup().snapshot_id
        self.engine.inject_corruption(snapshot_id, "/state/export/e.sqlite",
                                      flip_byte=True)
        with self.engine.dump(snapshot_id, "/state/export/e.sqlite") as stream:
            self.assertNotEqual(stream.read(), b"catalog")

    def test_drop_export_removes_it_from_one_snapshot_only(self):
        first = self.backup().snapshot_id
        second = self.backup().snapshot_id
        self.engine.drop_export(first)
        with self.assertRaises(EngineError) as caught:
            with self.engine.dump(first, "/state/export/e.sqlite") as stream:
                stream.read()
        self.assertEqual(caught.exception.kind, "missing")
        with self.engine.dump(second, "/state/export/e.sqlite") as stream:
            self.assertEqual(stream.read(), b"catalog")

    # ---- dedup oracle -------------------------------------------------

    def test_node_content_ids_expose_shared_blobs(self):
        first = self.backup().snapshot_id
        self.engine.add_source_file("/drop/other/copy.txt", b"payload")
        second = self.engine.backup(("/drop/other",), tags(seq=2)).snapshot_id
        self.assertEqual(
            self.engine.node_content_ids(first, "/drop/item/file.txt"),
            self.engine.node_content_ids(second, "/drop/other/copy.txt"))
        self.assertNotEqual(
            self.engine.node_content_ids(first, "/drop/item/file.txt"),
            self.engine.node_content_ids(first, "/drop/item/sub/deep.bin"))

    # ---- failures and the call log ----------------------------------------

    def test_failures_can_be_injected_by_kind(self):
        for kind in ("no-repo", "locked", "bad-password", "tool-error"):
            with self.subTest(kind=kind):
                engine = FakeEngine()
                engine.fail_with(kind)
                with self.assertRaises(EngineError) as caught:
                    engine.snapshots()
                self.assertEqual(caught.exception.kind, kind)

    def test_lock_and_unlock_are_recorded(self):
        self.engine.unlock()
        self.assertIn(("unlock",), [tuple(call[:1]) for call in self.engine.calls])

    def test_call_log_records_every_operation_in_order(self):
        self.backup()
        self.engine.snapshots()
        self.assertEqual([call[0] for call in self.engine.calls][-2:],
                         ["backup", "snapshots"])

    def test_raise_on_any_call_makes_query_tests_prove_locality(self):
        engine = FakeEngine()
        engine.raise_on_any_call()
        with self.assertRaises(AssertionError):
            engine.snapshots()
