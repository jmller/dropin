"""Journalled deletion and the ownership gate.

Engine-free on purpose: nothing here needs a repository, and everything here can
delete a user's only copy.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest

from dropin.capture.extract import capture_item
from dropin.macos.fake import FakeMacOS
from dropin.macos.interface import Capabilities, Unsupported
from dropin.pipeline.evict import (Retained, begin_eviction, run_eviction)
from dropin.pipeline.fingerprint import SourceChanged
from dropin.store import records
from dropin.store.db import connect

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "synthetic"


class FakeOwnership:
    """Configurable stand-in for the platform adapter, with a call log."""

    def __init__(self, supported: bool = True) -> None:
        self.supported = supported
        self.reason = "" if supported else "configured unsupported"
        self.holders: dict[tuple[str, bool], list[int]] = {}
        self.calls: list[tuple[str, bool]] = []
        self.fail_after: int | None = None

    def capabilities(self) -> Capabilities:
        return Capabilities(self.supported, self.reason, "test adapter")

    def open_descriptors(self, path: str, is_dir: bool) -> list[int]:
        self.calls.append((path, is_dir))
        if self.fail_after is not None and len(self.calls) > self.fail_after:
            self.supported = False
            self.reason = "capability lost mid-run"
        if not self.supported:
            raise Unsupported(self.reason)
        return list(self.holders.get((os.path.realpath(path), is_dir), []))

    def hold(self, path: str, pid: int, is_dir: bool = False) -> None:
        self.holders.setdefault((os.path.realpath(path), is_dir), []).append(pid)


class EvictTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dropin-evict-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.drop = self.root / "drop"
        self.drop.mkdir()
        self.db = connect(self.root / "store.sqlite")
        self.addCleanup(self.db.close)
        self.store_id = records.initialise_store(self.db)
        self.macos = FakeMacOS()
        self.ownership = FakeOwnership()

    def configure(self, path: Path) -> None:
        children = [path, *(path.rglob("*") if path.is_dir() else [])]
        for child in children:
            self.macos.set_mdls(str(child),
                                (FIXTURES / "mdls" / "text_plain.txt").read_text())
            self.macos.set_importer(
                str(child), (FIXTURES / "mdimport" / "no_text.txt").read_text())

    def recoverable(self, path: Path) -> str:
        self.configure(path)
        occ_id = records.record_occurrence(
            self.db, capture_item(self.macos, path), self.store_id)
        attempt = records.start_attempt(self.db, occ_id, self.store_id,
                                        export_path="/state/export/x.sqlite")
        records.finish_attempt(self.db, attempt.attempt_id, "confirmed")
        records.set_state(self.db, occ_id, "transferred")
        records.set_state(self.db, occ_id, "verified")
        records.set_state(self.db, occ_id, "recoverable",
                          confirmed_attempt_id=attempt.attempt_id)
        return occ_id

    def file_item(self, name="solo.txt", content=b"payload") -> tuple[str, Path]:
        path = self.drop / name
        path.write_bytes(content)
        return self.recoverable(path), path

    def tree_item(self, name="tree") -> tuple[str, Path]:
        tree = self.drop / name
        (tree / "sub").mkdir(parents=True)
        (tree / "a.txt").write_bytes(b"alpha")
        (tree / "sub" / "b.txt").write_bytes(b"beta")
        (tree / "link").symlink_to("a.txt")
        (tree / "empty").mkdir()
        return self.recoverable(tree), tree

    def evict(self, occ_id: str, path: Path) -> None:
        begin_eviction(self.db, occ_id, self.ownership, str(path))
        run_eviction(self.db, occ_id, self.ownership, str(path))


class GateBeforeIntentTest(EvictTestCase):
    def test_clean_file_is_deleted_and_committed(self):
        occ_id, path = self.file_item()
        self.evict(occ_id, path)
        self.assertFalse(path.exists())
        self.assertEqual(records.get_occurrence(self.db, occ_id)["state"],
                         "evicted")
        self.assertIsNone(self.db.execute(
            "SELECT * FROM eviction_intent WHERE occ_id = ?",
            (occ_id,)).fetchone())

    def test_unsupported_capability_leaves_the_item_recoverable(self):
        occ_id, path = self.file_item()
        self.ownership.supported = False
        with self.assertRaises(Retained) as caught:
            begin_eviction(self.db, occ_id, self.ownership, str(path))
        self.assertTrue(path.exists())
        self.assertEqual(records.get_occurrence(self.db, occ_id)["state"],
                         "recoverable")
        self.assertIn("ownership", str(caught.exception).lower())

    def test_open_writer_leaves_the_item_recoverable(self):
        occ_id, path = self.file_item()
        self.ownership.hold(str(path), 4242)
        with self.assertRaises(Retained) as caught:
            begin_eviction(self.db, occ_id, self.ownership, str(path))
        self.assertTrue(path.exists())
        self.assertIn("4242", str(caught.exception))
        self.assertEqual(records.get_occurrence(self.db, occ_id)["state"],
                         "recoverable")

    def test_source_change_at_gate_d_is_reported_as_a_source_change(self):
        occ_id, path = self.file_item()
        path.write_bytes(b"changed after publication")
        with self.assertRaises(SourceChanged):
            begin_eviction(self.db, occ_id, self.ownership, str(path))
        self.assertTrue(path.exists())

    def test_no_intent_row_is_written_when_a_gate_refuses(self):
        occ_id, path = self.file_item()
        self.ownership.hold(str(path), 1)
        with self.assertRaises(Retained):
            begin_eviction(self.db, occ_id, self.ownership, str(path))
        self.assertIsNone(self.db.execute(
            "SELECT * FROM eviction_intent WHERE occ_id = ?",
            (occ_id,)).fetchone())

    def test_intent_carries_the_final_fingerprint(self):
        occ_id, path = self.tree_item()
        begin_eviction(self.db, occ_id, self.ownership, str(path))
        row = self.db.execute(
            "SELECT * FROM eviction_intent WHERE occ_id = ?", (occ_id,)).fetchone()
        fingerprint = json.loads(row["fingerprint_json"])
        self.assertEqual({entry["rel_path"] for entry in fingerprint},
                         {"", "a.txt", "empty", "link", "sub", "sub/b.txt"})
        self.assertEqual(records.get_occurrence(self.db, occ_id)["state"],
                         "evicting")


class DeletionOrderTest(EvictTestCase):
    def test_tree_is_removed_completely(self):
        occ_id, tree = self.tree_item()
        self.evict(occ_id, tree)
        self.assertFalse(tree.exists())
        self.assertEqual(records.get_occurrence(self.db, occ_id)["state"],
                         "evicted")

    def test_each_entry_is_checked_immediately_before_its_own_removal(self):
        occ_id, tree = self.tree_item()
        self.evict(occ_id, tree)
        paths = [call[0] for call in self.ownership.calls]
        # Item-wide scan first, then a targeted check per entry.
        self.assertEqual(paths[0], str(tree))
        for entry in ("a.txt", "sub/b.txt", "link", "sub", "empty"):
            self.assertIn(str(tree / entry), paths)

    def test_directory_checks_use_the_directory_form(self):
        occ_id, tree = self.tree_item()
        self.evict(occ_id, tree)
        forms = {path: is_dir for path, is_dir in self.ownership.calls}
        self.assertTrue(forms[str(tree / "sub")])
        self.assertFalse(forms[str(tree / "a.txt")])

    def test_symlinks_are_removed_without_following_them(self):
        # Build the tree with an outward-pointing link before recording it, so
        # the manifest describes exactly what will be deleted.
        outside = self.root / "outside.txt"
        outside.write_bytes(b"must survive")
        tree = self.drop / "tree"
        (tree / "sub").mkdir(parents=True)
        (tree / "a.txt").write_bytes(b"alpha")
        (tree / "link").symlink_to(outside)
        occ_id = self.recoverable(tree)
        self.evict(occ_id, tree)
        self.assertFalse(tree.exists())
        self.assertTrue(outside.exists())


class RefusalDuringDeletionTest(EvictTestCase):
    def test_capability_loss_mid_deletion_retains_the_rest(self):
        occ_id, tree = self.tree_item()
        begin_eviction(self.db, occ_id, self.ownership, str(tree))
        self.ownership.fail_after = 1
        with self.assertRaises(Retained):
            run_eviction(self.db, occ_id, self.ownership, str(tree))
        self.assertTrue(tree.exists())
        self.assertEqual(records.get_occurrence(self.db, occ_id)["state"],
                         "evicting")

    def test_an_open_writer_appearing_mid_deletion_stops_before_that_unlink(self):
        occ_id, tree = self.tree_item()
        begin_eviction(self.db, occ_id, self.ownership, str(tree))
        self.ownership.hold(str(tree / "sub" / "b.txt"), 99)
        with self.assertRaises(Retained) as caught:
            run_eviction(self.db, occ_id, self.ownership, str(tree))
        self.assertTrue((tree / "sub" / "b.txt").exists())
        self.assertIn("99", str(caught.exception))

    def test_a_changed_entry_is_not_deleted(self):
        occ_id, tree = self.tree_item()
        begin_eviction(self.db, occ_id, self.ownership, str(tree))
        (tree / "sub" / "b.txt").write_bytes(b"rewritten after the intent")
        with self.assertRaises(Retained) as caught:
            run_eviction(self.db, occ_id, self.ownership, str(tree))
        self.assertTrue((tree / "sub" / "b.txt").exists())
        self.assertIn("sub/b.txt", str(caught.exception))
        self.assertEqual(records.get_occurrence(self.db, occ_id)["state"],
                         "evicting")

    def test_an_unexpected_entry_stops_the_removal(self):
        occ_id, tree = self.tree_item()
        begin_eviction(self.db, occ_id, self.ownership, str(tree))
        (tree / "sub" / "surprise.txt").write_bytes(b"not in the intent")
        with self.assertRaises(Retained) as caught:
            run_eviction(self.db, occ_id, self.ownership, str(tree))
        self.assertIn("unexpected", str(caught.exception))
        self.assertTrue((tree / "sub" / "surprise.txt").exists())

    def test_a_refusal_never_transitions_to_abandoned(self):
        occ_id, tree = self.tree_item()
        begin_eviction(self.db, occ_id, self.ownership, str(tree))
        (tree / "a.txt").write_bytes(b"changed")
        with self.assertRaises(Retained):
            run_eviction(self.db, occ_id, self.ownership, str(tree))
        self.assertEqual(records.get_occurrence(self.db, occ_id)["state"],
                         "evicting")


class ResumeTest(EvictTestCase):
    def test_a_live_intent_treats_absent_entries_as_its_own_work(self):
        occ_id, tree = self.tree_item()
        begin_eviction(self.db, occ_id, self.ownership, str(tree))
        (tree / "sub" / "b.txt").unlink()  # as if we crashed after this unlink
        run_eviction(self.db, occ_id, self.ownership, str(tree))
        self.assertFalse(tree.exists())
        self.assertEqual(records.get_occurrence(self.db, occ_id)["state"],
                         "evicted")

    def test_capability_is_re_proven_on_every_resumed_pass(self):
        occ_id, tree = self.tree_item()
        begin_eviction(self.db, occ_id, self.ownership, str(tree))
        self.ownership.supported = False
        with self.assertRaises(Retained):
            run_eviction(self.db, occ_id, self.ownership, str(tree))
        self.assertTrue(tree.exists())

    def test_a_recovery_marked_intent_with_an_absent_root_stays_evicting(self):
        occ_id, path = self.file_item()
        begin_eviction(self.db, occ_id, self.ownership, str(path))
        self.db.execute(
            "UPDATE eviction_intent SET recovered_without_local_history = 1"
            " WHERE occ_id = ?", (occ_id,))
        path.unlink()
        with self.assertRaises(Retained) as caught:
            run_eviction(self.db, occ_id, self.ownership, str(path))
        self.assertIn("manual intervention", str(caught.exception))
        self.assertEqual(records.get_occurrence(self.db, occ_id)["state"],
                         "evicting")

    def test_a_live_intent_with_an_absent_root_completes(self):
        # Our own crash after the final unlink must converge, not stall.
        occ_id, path = self.file_item()
        begin_eviction(self.db, occ_id, self.ownership, str(path))
        path.unlink()
        run_eviction(self.db, occ_id, self.ownership, str(path))
        self.assertEqual(records.get_occurrence(self.db, occ_id)["state"],
                         "evicted")

    def test_a_recovery_marked_intent_with_a_verifiable_root_proceeds(self):
        occ_id, path = self.file_item()
        begin_eviction(self.db, occ_id, self.ownership, str(path))
        self.db.execute(
            "UPDATE eviction_intent SET recovered_without_local_history = 1"
            " WHERE occ_id = ?", (occ_id,))
        run_eviction(self.db, occ_id, self.ownership, str(path))
        self.assertFalse(path.exists())
        self.assertEqual(records.get_occurrence(self.db, occ_id)["state"],
                         "evicted")


class CheckOrderTest(EvictTestCase):
    def test_the_ownership_check_immediately_precedes_the_unlink(self):
        occ_id, path = self.file_item()
        order: list[str] = []
        real_unlink = os.unlink

        def traced_unlink(target, **kwargs):
            order.append("unlink")
            return real_unlink(target, **kwargs)

        original = self.ownership.open_descriptors

        def traced_check(check_path, is_dir):
            order.append("check")
            return original(check_path, is_dir)

        self.ownership.open_descriptors = traced_check
        os.unlink = traced_unlink
        self.addCleanup(setattr, os, "unlink", real_unlink)
        try:
            self.evict(occ_id, path)
        finally:
            os.unlink = real_unlink
        self.assertEqual(order[-2:], ["check", "unlink"])
