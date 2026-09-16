"""Snapshot contents versus the expected manifest."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from dropin.capture.extract import capture_item
from dropin.engine.fake import FakeEngine
from dropin.macos.fake import FakeMacOS
from dropin.pipeline.reconcile import ReconcileError, reconcile
from dropin.store import records
from dropin.store.db import connect

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "synthetic"


class ReconcileTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dropin-reconcile-")
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

    def build_tree(self) -> Path:
        tree = self.drop / "tree"
        (tree / "sub").mkdir(parents=True)
        (tree / "a.txt").write_bytes(b"alpha")
        (tree / "sub" / "b.txt").write_bytes(b"beta")
        (tree / "link").symlink_to("a.txt")
        (tree / "empty").mkdir()
        return tree

    def capture(self, path: Path) -> str:
        for child in [path, *path.rglob("*")]:
            self.macos.set_mdls(str(child),
                                (FIXTURES / "mdls" / "text_plain.txt").read_text())
            self.macos.set_importer(
                str(child), (FIXTURES / "mdimport" / "no_text.txt").read_text())
        self.macos.set_mdls(str(path),
                            (FIXTURES / "mdls" / "folder_plain.txt").read_text())
        return records.record_occurrence(self.db, capture_item(self.macos, path),
                                         self.store_id)

    def stage(self, path: Path) -> None:
        for child in [path, *path.rglob("*")]:
            if child.is_symlink():
                self.engine.add_source_symlink(str(child), str(child.readlink()))
            elif child.is_dir():
                self.engine.add_source_dir(str(child))
            else:
                self.engine.add_source_file(str(child), child.read_bytes())

    def run_reconcile(self, occ_id: str, snapshot_id: str, spool_path: Path):
        reconcile(self.engine, snapshot_id, occ_id, self.db, str(spool_path))


class HappyPathTest(ReconcileTestCase):
    def test_matching_node_set_passes(self):
        tree = self.build_tree()
        occ_id = self.capture(tree)
        self.stage(tree)
        snapshot = self.engine.backup((str(tree),), ("t",)).snapshot_id
        self.run_reconcile(occ_id, snapshot, tree)

    def test_single_file_item_passes(self):
        path = self.drop / "solo.txt"
        path.write_bytes(b"solo")
        self.macos.set_mdls(str(path),
                            (FIXTURES / "mdls" / "text_plain.txt").read_text())
        self.macos.set_importer(
            str(path), (FIXTURES / "mdimport" / "no_text.txt").read_text())
        occ_id = records.record_occurrence(
            self.db, capture_item(self.macos, path), self.store_id)
        self.engine.add_source_file(str(path), b"solo")
        snapshot = self.engine.backup((str(path),), ("t",)).snapshot_id
        self.run_reconcile(occ_id, snapshot, path)


class MismatchTest(ReconcileTestCase):
    def setUp(self):
        super().setUp()
        self.tree = self.build_tree()
        self.occ_id = self.capture(self.tree)
        self.stage(self.tree)

    def snapshot_without(self, path: str) -> str:
        self.engine.exit3_on_next_backup(omit=path)
        return self.engine.backup((str(self.tree),), ("t",)).snapshot_id

    def test_missing_entry_names_it(self):
        snapshot = self.snapshot_without(str(self.tree / "sub" / "b.txt"))
        with self.assertRaises(ReconcileError) as caught:
            self.run_reconcile(self.occ_id, snapshot, self.tree)
        self.assertIn("sub/b.txt", str(caught.exception))
        self.assertEqual(caught.exception.rel_path, "sub/b.txt")

    def test_extra_entry_names_it(self):
        self.engine.add_source_file(str(self.tree / "surprise.txt"), b"extra")
        snapshot = self.engine.backup((str(self.tree),), ("t",)).snapshot_id
        with self.assertRaises(ReconcileError) as caught:
            self.run_reconcile(self.occ_id, snapshot, self.tree)
        self.assertEqual(caught.exception.rel_path, "surprise.txt")

    def test_size_mismatch_names_it(self):
        self.engine.add_source_file(str(self.tree / "a.txt"), b"alpha-longer")
        snapshot = self.engine.backup((str(self.tree),), ("t",)).snapshot_id
        with self.assertRaises(ReconcileError) as caught:
            self.run_reconcile(self.occ_id, snapshot, self.tree)
        self.assertEqual(caught.exception.rel_path, "a.txt")
        self.assertIn("size", str(caught.exception))

    def test_type_mismatch_names_it(self):
        self.engine.add_source_dir(str(self.tree / "a.txt"))
        snapshot = self.engine.backup((str(self.tree),), ("t",)).snapshot_id
        with self.assertRaises(ReconcileError) as caught:
            self.run_reconcile(self.occ_id, snapshot, self.tree)
        self.assertEqual(caught.exception.rel_path, "a.txt")
        self.assertIn("type", str(caught.exception))

    def test_link_targets_are_not_compared_here(self):
        # restic 0.19.1 `ls --json` exposes no link target; the tar pass
        # owns that comparison. Reconciliation must not silently skip it either:
        # it compares what `ls` actually provides and nothing more.
        from dropin.pipeline import reconcile as module

        self.assertNotIn("link_target", module.COMPARED_FIELDS)
        self.assertEqual(module.COMPARED_FIELDS, ("entry_type", "size_bytes"))
