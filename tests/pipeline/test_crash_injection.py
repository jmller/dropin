"""Crash injection at every transition, then convergence on re-run.

`DROPIN_FAULT_AFTER=<point>` aborts the driver immediately after the named
transition has been committed. The test then runs `drain` again with no fault
and asserts the store converges: exactly one confirmed attempt, duplicates
orphaned, nothing evicted before `recoverable`, and the data-model invariants
1–8 holding at every step. Convergence is *conditional*: it must not happen
when the source changed or the ownership check cannot be re-proven.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import sqlite3

from dropin.pipeline.faults import FaultInjected, POINTS as PIPELINE_FAULT_POINTS
from dropin.report import Outcome
from dropin.store import records
from tests.pipeline.test_drain import DrainTestCase
from tests.support import FaultHook

IDENTIFIER = re.compile(r"^[0-9a-f]{32}\.[0-9ABCDEFGHJKMNPQRSTVWXYZ]{26}$")
PAST_BACKOFF = 1000  # seconds; config backoff is 300


class CrashTestCase(DrainTestCase):
    # ---- driving -----------------------------------------------------------

    def crash_after(self, point: str, **options) -> None:
        with FaultHook(point):
            with self.assertRaises(FaultInjected):
                self.run_drain(**options)
        self.assert_invariants()

    def converge(self, name: str = "report.pdf", **options):
        options.setdefault("now_offset", PAST_BACKOFF)
        report = self.run_drain(**options)
        self.assert_invariants()
        return {record.name: record for record in report.records}[name]

    def occurrence(self, name: str = "report.pdf"):
        row = self.db.execute(
            "SELECT * FROM occurrence WHERE item_name = ?"
            " ORDER BY recorded_at DESC, occ_id DESC LIMIT 1", (name,)).fetchone()
        self.assertIsNotNone(row, f"no occurrence for {name}")
        return row

    def attempts(self, name: str = "report.pdf"):
        return records.attempts_for(self.db, self.occurrence(name)["occ_id"])

    def confirmed_attempts(self, name: str = "report.pdf"):
        return [row for row in self.attempts(name) if row["outcome"] == "confirmed"]

    def assert_converged(self, path: Path, name: str = "report.pdf") -> None:
        self.assertFalse(path.exists(), "the original must be gone")
        self.assertEqual(self.occurrence(name)["state"], "evicted")
        self.assertEqual(len(self.confirmed_attempts(name)), 1)
        statuses = [row["status"] for row in self.db.execute(
            "SELECT status FROM snapshot WHERE occ_id = ?",
            (self.occurrence(name)["occ_id"],))]
        self.assertEqual(statuses.count("confirmed"), 1)
        self.assertNotIn("pending", statuses)

    # ---- the catalog invariants ---------------------------------------------

    def assert_invariants(self) -> None:
        db = self.db
        # 8: every namespaced id is well formed and namespaced to its store.
        for row in db.execute("SELECT occ_id, origin_store_id FROM occurrence"):
            self.assertRegex(row["occ_id"], IDENTIFIER)
            self.assertTrue(row["occ_id"].startswith(row["origin_store_id"] + "."))
        for row in db.execute(
                "SELECT attempt_id, origin_store_id FROM publication_attempt"):
            self.assertRegex(row["attempt_id"], IDENTIFIER)
            self.assertTrue(
                row["attempt_id"].startswith(row["origin_store_id"] + "."))

        for occ in db.execute("SELECT * FROM occurrence"):
            history = records.attempts_for(db, occ["occ_id"])
            pending = [a for a in history if a["outcome"] == "pending"]
            confirmed = [a for a in history if a["outcome"] == "confirmed"]
            state = occ["state"]
            # 7: pipeline states and the attempts they imply.
            if state in ("transferred", "verified"):
                self.assertEqual(len(pending), 1, f"{state} needs one pending")
                self.assertIsNotNone(pending[0]["snapshot_id"])
                self.assertEqual(confirmed, [])
            if state in ("recoverable", "evicting", "evicted"):
                self.assertEqual(len(confirmed), 1, f"{state} needs one confirmed")
                self.assertEqual(occ["confirmed_attempt_id"],
                                 confirmed[0]["attempt_id"])
                self.assertEqual(pending, [])
                # 3: never a non-confirmed attempt or an orphaned snapshot.
                snapshot = records.get_snapshot(db, confirmed[0]["snapshot_id"])
                self.assertIsNotNone(snapshot)
                self.assertEqual(snapshot["status"], "confirmed")
            if state == "recorded":
                self.assertEqual(confirmed, [])
            # 1: `evicted` only through the whole chain, with a confirmed snapshot.
            if state == "evicted":
                self.assertIsNotNone(occ["recoverable_at"])
                self.assertIsNotNone(occ["evicting_at"])
                self.assertIsNone(db.execute(
                    "SELECT 1 FROM eviction_intent WHERE occ_id = ?",
                    (occ["occ_id"],)).fetchone())
            # 6: a live intent belongs to an `evicting` occurrence and its
            # fingerprint names every entry of the manifest.
            if state == "evicting":
                intent = db.execute(
                    "SELECT * FROM eviction_intent WHERE occ_id = ?",
                    (occ["occ_id"],)).fetchone()
                self.assertIsNotNone(intent)
                import json

                named = {e["rel_path"] for e in json.loads(intent["fingerprint_json"])}
                manifest = {r["rel_path"] for r in
                            records.iter_entries(db, occ["occ_id"])}
                self.assertEqual(named, manifest)

        # 5: a failed attempt's snapshot is orphaned; a confirmed snapshot's
        # attempt is confirmed.
        for snap in db.execute("SELECT * FROM snapshot"):
            attempt = records.get_attempt(db, snap["attempt_id"])
            if attempt["outcome"] == "failed":
                self.assertEqual(snap["status"], "orphaned", snap["snapshot_id"])
            if snap["status"] == "confirmed":
                self.assertEqual(attempt["outcome"], "confirmed")

        # 4: sequence uniqueness and the two watermarks.
        seqs = [row[0] for row in db.execute(
            "SELECT origin_store_id || ':' || export_seq FROM publication_attempt")]
        self.assertEqual(len(seqs), len(set(seqs)))
        meta = records.store_meta(db)
        top = db.execute("SELECT coalesce(max(export_seq), 0)"
                         " FROM publication_attempt").fetchone()[0]
        confirmed_top = db.execute(
            "SELECT coalesce(max(export_seq), 0) FROM publication_attempt"
            " WHERE outcome = 'confirmed'").fetchone()[0]
        self.assertGreaterEqual(meta["export_seq"], top)
        self.assertEqual(meta["published_frontier"], confirmed_top)
        self.assertLessEqual(meta["published_frontier"], meta["export_seq"])

        # 2: the manifest is immutable.
        row = db.execute("SELECT occ_id, rel_path FROM entry LIMIT 1").fetchone()
        if row is not None:
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("UPDATE entry SET sha256 = 'x' WHERE occ_id = ?"
                           " AND rel_path = ?", (row["occ_id"], row["rel_path"]))
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("DELETE FROM fingerprint WHERE occ_id = ?",
                           (row["occ_id"],))


class CrashBeforePublicationTest(CrashTestCase):
    def test_after_recorded(self):
        path = self.drop_file()
        self.crash_after("recorded")
        self.assertEqual(self.occurrence()["state"], "recorded")
        self.assertEqual(self.attempts(), [])
        self.assertTrue(path.exists())
        self.assertEqual(self.converge().outcome, Outcome.ARCHIVED)
        self.assert_converged(path)
        self.assertEqual(len(self.engine.snapshots()), 1)

    def test_after_attempt_started_needs_the_clock_to_pass_backoff(self):
        path = self.drop_file()
        self.crash_after("attempt-started")
        [attempt] = self.attempts()
        self.assertEqual(attempt["outcome"], "pending")
        self.assertIsNone(attempt["snapshot_id"])

        # Immediately: the pending attempt is settled `no snapshot` and the
        # retry waits out the backoff. No second snapshot, nothing deleted.
        record = self.converge(now_offset=0)
        self.assertEqual(record.outcome, Outcome.DEFERRED)
        self.assertIn("backoff", record.reason)
        [attempt] = self.attempts()
        self.assertEqual((attempt["outcome"], attempt["reason"]),
                         ("failed", "no snapshot"))
        self.assertTrue(path.exists())
        self.assertEqual(self.engine.snapshots(), [])

        # Past the backoff: a fresh attempt converges.
        self.assertEqual(self.converge().outcome, Outcome.ARCHIVED)
        self.assert_converged(path)
        self.assertEqual([a["outcome"] for a in self.attempts()],
                         ["failed", "confirmed"])

    def test_after_backup_returned_with_the_snapshot_id_unrecorded(self):
        path = self.drop_file()
        self.crash_after("backup-returned")
        [attempt] = self.attempts()
        self.assertIsNone(attempt["snapshot_id"], "the id must not be recorded")
        self.assertEqual(len(self.engine.snapshots()), 1)

        # Resume adopts the published snapshot instead of publishing again.
        self.assertEqual(self.converge().outcome, Outcome.ARCHIVED)
        self.assert_converged(path)
        self.assertEqual(len(self.engine.snapshots()), 1)
        [attempt] = self.attempts()
        self.assertEqual(attempt["outcome"], "confirmed")

    def test_an_unrecorded_snapshot_does_not_stall_a_different_item(self):
        """The frontier gate must match the attempt row by tags."""
        held = self.drop_file("held.txt", b"held")
        self.crash_after("backup-returned")
        other = self.drop_file("other.txt", b"other")
        report = self.run_drain(now_offset=PAST_BACKOFF)
        self.assertIsNone(report.run_refusal, report.run_refusal)
        self.assertEqual({r.name: r.outcome for r in report.records},
                         {"held.txt": Outcome.ARCHIVED,
                          "other.txt": Outcome.ARCHIVED})
        self.assertFalse(held.exists())
        self.assertFalse(other.exists())
        self.assert_invariants()

    def test_duplicate_snapshots_for_one_attempt_are_orphaned(self):
        path = self.drop_file()
        self.crash_after("backup-returned")
        [attempt] = self.attempts()
        # A second publication under the same attempt tags (a retried backup
        # whose first run we never heard back from).
        first = self.engine.snapshots()[0]
        self.engine.backup(first.paths, first.tags)
        self.assertEqual(len(self.engine.snapshots()), 2)

        record = self.converge(now_offset=0)
        self.assertEqual(record.outcome, Outcome.DEFERRED)
        self.assertEqual(records.get_attempt(self.db, attempt["attempt_id"])
                         ["reason"], "duplicate")
        orphaned = [row for row in self.db.execute("SELECT * FROM snapshot")
                    if row["status"] == "orphaned"]
        self.assertEqual(len(orphaned), 2)
        self.assertTrue(path.exists())

        self.assertEqual(self.converge().outcome, Outcome.ARCHIVED)
        self.assert_converged(path)
        self.assertEqual(len(self.engine.snapshots()), 3)
        self.assertEqual(len(self.confirmed_attempts()), 1)


class CrashAfterPublicationTest(CrashTestCase):
    def test_after_transferred(self):
        path = self.drop_file()
        self.crash_after("transferred")
        self.assertEqual(self.occurrence()["state"], "transferred")
        self.assertEqual(self.converge().outcome, Outcome.ARCHIVED)
        self.assert_converged(path)
        self.assertEqual(len(self.engine.snapshots()), 1)

    def test_after_verified(self):
        path = self.drop_file()
        self.crash_after("verified")
        self.assertEqual(self.occurrence()["state"], "verified")
        self.assertEqual(self.converge().outcome, Outcome.ARCHIVED)
        self.assert_converged(path)
        self.assertEqual(len(self.engine.snapshots()), 1)

    def test_after_recoverable_nothing_was_deleted_yet(self):
        path = self.drop_file()
        self.crash_after("recoverable")
        self.assertEqual(self.occurrence()["state"], "recoverable")
        self.assertTrue(path.exists())
        self.assertEqual(self.converge().outcome, Outcome.ARCHIVED)
        self.assert_converged(path)

    def test_a_source_change_after_transferred_is_caught_at_gate_c(self):
        path = self.drop_file()
        self.crash_after("transferred")
        path.write_bytes(b"rewritten while the archiver was down")
        record = self.converge()
        self.assertEqual(record.outcome, Outcome.DEFERRED)
        self.assertIn("source changed", record.reason)
        self.assertTrue(path.exists())
        self.assertEqual(self.occurrence()["state"], "abandoned")
        self.assertEqual([a["outcome"] for a in self.attempts()], ["failed"])
        self.assertEqual(
            [s["status"] for s in self.db.execute("SELECT status FROM snapshot")],
            ["orphaned"])

    def test_a_source_change_after_verified_is_caught_at_gate_d(self):
        path = self.drop_file()
        self.crash_after("verified")
        path.write_bytes(b"rewritten while the archiver was down")
        record = self.converge()
        self.assertEqual(record.outcome, Outcome.DEFERRED)
        self.assertIn("after publication", record.reason)
        self.assertTrue(path.exists())
        occurrence = self.occurrence()
        self.assertEqual(occurrence["state"], "abandoned")
        # The publication survives: attempt, snapshot, and frontier stay confirmed.
        self.assertEqual(len(self.confirmed_attempts()), 1)
        self.assertEqual(records.store_meta(self.db)["published_frontier"],
                         self.confirmed_attempts()[0]["export_seq"])


class CrashDuringEvictionTest(CrashTestCase):
    def test_after_intent_written(self):
        path = self.drop_file()
        self.crash_after("intent-written")
        self.assertEqual(self.occurrence()["state"], "evicting")
        self.assertTrue(path.exists())
        self.assertEqual(self.converge().outcome, Outcome.ARCHIVED)
        self.assert_converged(path)

    def test_an_open_writer_after_the_intent_retains_evicting(self):
        path = self.drop_file()
        self.crash_after("intent-written")
        self.ownership.hold(str(path), 31337)
        record = self.converge()
        self.assertEqual(record.outcome, Outcome.RETAINED)
        self.assertIn("31337", record.reason)
        self.assertEqual(record.state, "evicting")
        self.assertTrue(path.exists(), "no unlink may follow a refused check")
        self.assertEqual(self.occurrence()["state"], "evicting")

        self.ownership.holders.clear()
        self.assertEqual(self.converge().outcome, Outcome.ARCHIVED)
        self.assert_converged(path)

    def test_capability_loss_after_the_intent_retains_evicting(self):
        path = self.drop_file()
        self.crash_after("intent-written")
        self.ownership.calls.clear()
        self.ownership.supported = False
        self.ownership.reason = "lsof gone"
        record = self.converge()
        self.assertEqual(record.outcome, Outcome.RETAINED)
        self.assertIn("ownership check unavailable", record.reason)
        self.assertTrue(path.exists())
        self.assertEqual(self.occurrence()["state"], "evicting")
        self.assertEqual(self.ownership.calls, [],
                         "no descriptor check may run without capability")

        self.ownership.supported = True
        self.assertEqual(self.converge().outcome, Outcome.ARCHIVED)
        self.assert_converged(path)

    def test_capability_lost_mid_pass_stops_before_the_next_unlink(self):
        tree = self.drop_tree()
        self.crash_after("intent-written")
        # The item-wide scan passes, the first targeted check passes, then the
        # capability vanishes: exactly one entry may have gone.
        self.ownership.calls.clear()
        self.ownership.fail_after = 2
        record = self.converge("tree")
        self.assertEqual(record.outcome, Outcome.RETAINED)
        self.assertEqual(self.occurrence("tree")["state"], "evicting")
        self.assertTrue(tree.exists())
        remaining = sorted(str(p.relative_to(tree)) for p in tree.rglob("*"))
        self.assertGreaterEqual(len(remaining), 2)

        self.ownership.fail_after = None
        self.ownership.supported = True
        self.ownership.reason = ""
        self.assertEqual(self.converge("tree").outcome, Outcome.ARCHIVED)
        self.assert_converged(tree, "tree")

    def test_mid_deletion_touches_nothing_outside_the_intent(self):
        outside = self.root / "outside.txt"
        outside.write_bytes(b"must survive")
        tree = self.drop / "tree"
        (tree / "sub").mkdir(parents=True)
        (tree / "a.txt").write_bytes(b"alpha")
        (tree / "sub" / "b.txt").write_bytes(b"beta")
        (tree / "link").symlink_to(outside)
        self.describe(tree)
        bystander = self.drop_file("zzz-bystander.txt", b"untouched")

        self.crash_after("mid-deletion")
        self.assertEqual(self.occurrence("tree")["state"], "evicting")
        self.assertTrue(tree.exists(), "the crash landed mid-tree")
        self.assertTrue(outside.exists())
        self.assertEqual(bystander.read_bytes(), b"untouched")
        # Every entry that is gone was named by the intent.
        import json

        intent = self.db.execute(
            "SELECT fingerprint_json FROM eviction_intent WHERE occ_id = ?",
            (self.occurrence("tree")["occ_id"],)).fetchone()
        named = {e["rel_path"] for e in json.loads(intent["fingerprint_json"])}
        present = {str(p.relative_to(tree)) for p in tree.rglob("*")}
        self.assertTrue(present <= named)
        self.assertLess(len(present), len(named) - 1)

        report = self.run_drain(now_offset=PAST_BACKOFF)
        self.assert_invariants()
        outcomes = {r.name: r.outcome for r in report.records}
        self.assertEqual(outcomes["tree"], Outcome.ARCHIVED)
        self.assertEqual(outcomes["zzz-bystander.txt"], Outcome.ARCHIVED)
        self.assert_converged(tree, "tree")
        self.assertTrue(outside.exists(), "symlinks are removed, never followed")

    def test_after_root_removed_before_evicted_is_committed(self):
        path = self.drop_file()
        self.crash_after("root-removed")
        self.assertFalse(path.exists())
        self.assertEqual(self.occurrence()["state"], "evicting")
        # The item is no longer in the spool, so only the intent pass reaches it.
        record = self.converge()
        self.assertEqual(record.outcome, Outcome.ARCHIVED)
        self.assert_converged(path)

    def test_a_recovery_marked_intent_with_an_absent_root_is_retained(self):
        path = self.drop_file()
        self.crash_after("root-removed")
        self.db.execute("UPDATE eviction_intent SET"
                        " recovered_without_local_history = 1")
        record = self.converge()
        self.assertEqual(record.outcome, Outcome.RETAINED)
        self.assertIn("manual intervention", record.reason)
        self.assertEqual(self.occurrence()["state"], "evicting")


class EveryPointConvergesTest(CrashTestCase):
    """The whole ladder, one point at a time, on a tree."""

    # Bind the release gate to the production declaration so a new durable
    # boundary cannot be added without entering this convergence matrix.
    POINTS = PIPELINE_FAULT_POINTS

    def test_each_point_then_a_clean_run(self):
        for point in self.POINTS:
            with self.subTest(point=point):
                self.setUp()
                tree = self.drop_tree()
                self.crash_after(point)
                self.assertNotEqual(self.occurrence("tree")["state"], "evicted"
                                    if point != "root-removed" else "never")
                for _ in range(2):  # at most one backoff wait
                    record = self.converge("tree")
                    if record.outcome is Outcome.ARCHIVED:
                        break
                self.assert_converged(tree, "tree")
                self.assertEqual(os.listdir(self.drop), [])
