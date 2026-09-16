"""Fresh recovery from the repository alone.

Every scenario builds a real store with the drain driver against the engine
fake, throws that store away, and recovers into a new empty state directory.
The recovered store is then exercised: a drain over an empty spool must pass
the frontier gate with no refusals.
"""

from __future__ import annotations

import os
from pathlib import Path
import sqlite3

from dropin.config import load
from dropin.engine.fake import FakeEngine
from dropin.pipeline.drain import DrainOptions, drain
from dropin.pipeline.faults import FaultInjected
from dropin.recover import RecoverRefused, recover
from dropin.report import Outcome, Report
from dropin.store import records
from dropin.store.db import connect
from tests.pipeline.test_drain import CONFIG, DrainContext, DrainTestCase
from tests.support import FaultHook


class RecoverTestCase(DrainTestCase):
    def setUp(self):
        super().setUp()
        self.recovered_dir = self.root / "recovered"

    def recover(self, **options):
        report = Report(verb="recover", run_id="rec-1")
        result = recover(self.engine, self.recovered_dir, report, **options)
        return result, report

    def open_recovered(self) -> sqlite3.Connection:
        db = connect(self.recovered_dir / "store.sqlite")
        self.addCleanup(db.close)
        return db

    def drain_recovered(self, drop=None, **options) -> Report:
        """A drain from the recovered store, over its own (default empty) spool."""
        drop = drop or (self.root / "drop2")
        drop.mkdir(exist_ok=True)
        password = self.root / "pw"
        config_path = self.root / "config-recovered.toml"
        config_path.write_text(CONFIG.format(drop=drop, state=self.recovered_dir,
                                             password=password))
        config = load(config_path)
        db = connect(config.store_path)
        self.addCleanup(db.close)
        context = DrainContext(config, db, self.engine, self.macos, self.ownership)
        report = Report(verb="drain", run_id="run-r")
        drain(context, report, DrainOptions(**options))
        return report

    def occurrence(self, db, name: str):
        row = db.execute("SELECT * FROM occurrence WHERE item_name = ?"
                         " ORDER BY recorded_at DESC LIMIT 1", (name,)).fetchone()
        self.assertIsNotNone(row, name)
        return row

    def crash_after(self, point: str) -> None:
        with FaultHook(point), self.assertRaises(FaultInjected):
            self.run_drain()


class IndependentRecoveryTest(RecoverTestCase):
    def test_evicted_items_come_back_as_recoverable(self):
        self.drop_file("a.txt", b"alpha")
        self.drop_tree("tree")
        self.run_drain()
        original = {row["item_name"]: row for row in
                    self.db.execute("SELECT * FROM occurrence")}
        original_meta = records.store_meta(self.db)
        self.db.close()

        result, report = self.recover()
        self.assertEqual(report.exit_code(), 0, report.render_human())
        db = self.open_recovered()
        for name in ("a.txt", "tree"):
            row = self.occurrence(db, name)
            self.assertEqual(row["state"], "recoverable",
                             "local deletion is never claimed by recovery")
            self.assertEqual(row["occ_id"], original[name]["occ_id"])
            self.assertEqual(row["confirmed_attempt_id"],
                             original[name]["confirmed_attempt_id"])
            attempt = records.get_attempt(db, row["confirmed_attempt_id"])
            self.assertEqual(attempt["outcome"], "confirmed")
            self.assertEqual(records.get_snapshot(db, attempt["snapshot_id"])
                             ["status"], "confirmed")
            self.assertEqual(
                [r["rel_path"] for r in records.iter_entries(db, row["occ_id"])],
                [r["rel_path"] for r in self.db_entries(original[name]["occ_id"])])
        meta = records.store_meta(db)
        self.assertEqual(meta["store_id"], original_meta["store_id"])
        self.assertEqual(meta["export_seq"], original_meta["export_seq"])
        self.assertEqual(meta["published_frontier"],
                         original_meta["published_frontier"])
        self.assertEqual(result.counts["recoverable"], 2)

    def db_entries(self, occ_id):
        # The original store was closed above; reopen read-only for comparison.
        db = connect(self.config.store_path, read_only=True)
        self.addCleanup(db.close)
        return list(records.iter_entries(db, occ_id))

    def test_a_drain_after_recovery_passes_the_frontier_gate_quietly(self):
        self.drop_file("a.txt", b"alpha")
        self.run_drain()
        self.recover()
        report = self.drain_recovered()
        self.assertIsNone(report.run_refusal)
        self.assertEqual(report.records, [])
        self.assertEqual(report.exit_code(), 0)

    def test_recovered_rows_are_dormant_and_can_still_publish_new_items(self):
        self.drop_file("a.txt", b"alpha")
        self.run_drain()
        self.recover()
        drop = self.root / "drop2"
        drop.mkdir()
        new = drop / "b.txt"
        new.write_bytes(b"beta")
        self.describe(new)
        report = self.drain_recovered(drop)
        self.assertEqual({r.name: r.outcome for r in report.records},
                         {"b.txt": Outcome.ARCHIVED})
        self.assertFalse(new.exists())
        db = self.open_recovered()
        self.assertEqual(self.occurrence(db, "a.txt")["state"], "recoverable")

    def test_the_published_store_is_a_standalone_delete_journal_file(self):
        self.drop_file("a.txt", b"alpha")
        self.run_drain()
        self.recover()
        store = self.recovered_dir / "store.sqlite"
        self.assertTrue(store.exists())
        self.assertFalse((self.recovered_dir / "recover.tmp").exists())
        self.assertFalse(Path(f"{store}-wal").exists())
        raw = sqlite3.connect(f"file:{store}?mode=ro", uri=True)
        self.addCleanup(raw.close)
        self.assertEqual(raw.execute("PRAGMA journal_mode").fetchone()[0], "delete")
        self.assertEqual(raw.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        for name in ("export", "cache", "tmp"):
            self.assertTrue((self.recovered_dir / name).is_dir())


class RefusalTest(RecoverTestCase):
    def test_an_existing_store_is_never_overwritten(self):
        self.drop_file("a.txt", b"alpha")
        self.run_drain()
        self.recovered_dir.mkdir()
        marker = self.recovered_dir / "store.sqlite"
        marker.write_bytes(b"not ours")
        with self.assertRaises(RecoverRefused):
            self.recover()
        self.assertEqual(marker.read_bytes(), b"not ours")
        self.assertFalse((self.recovered_dir / "recover.tmp").exists())

    def test_an_empty_repository_is_refused(self):
        with self.assertRaises(RecoverRefused):
            self.recover()
        self.assertFalse((self.recovered_dir / "store.sqlite").exists())

    def test_the_old_state_directory_is_untouched(self):
        self.drop_file("a.txt", b"alpha")
        self.run_drain()
        before = self.config.store_path.read_bytes()
        self.recover()
        self.assertEqual(self.config.store_path.read_bytes(), before)

    def test_an_interrupted_recovery_is_rebuilt_from_scratch(self):
        self.drop_file("a.txt", b"alpha")
        self.run_drain()
        junk = self.recovered_dir / "recover.tmp" / "exports"
        junk.mkdir(parents=True)
        (junk / "stale.sqlite").write_bytes(b"junk")
        (self.recovered_dir / "recover.tmp" / "store.sqlite").write_bytes(b"junk")
        _, report = self.recover()
        self.assertEqual(report.exit_code(), 0)
        self.assertFalse((self.recovered_dir / "recover.tmp").exists())
        self.assertEqual(self.occurrence(self.open_recovered(), "a.txt")["state"],
                         "recoverable")


class TagStrictnessTest(RecoverTestCase):
    def test_malformed_and_duplicate_tags_are_skipped_and_reported(self):
        self.drop_file("a.txt", b"alpha")
        self.run_drain()
        good = self.engine.snapshots()[0]
        malformed = [t for t in good.tags if not t.startswith("dropin:kind=")]
        self.engine.backup(good.paths, malformed)
        duplicated = [*good.tags, "dropin:seq=999"]
        self.engine.backup(good.paths, duplicated)

        result, report = self.recover()
        skipped = [r for r in report.records if r.outcome is Outcome.ORPHANED
                   and "malformed" in (r.reason or "")]
        self.assertEqual(len(skipped), 2)
        self.assertEqual(len(result.skipped), 2)
        db = self.open_recovered()
        self.assertEqual(db.execute("SELECT count(*) FROM snapshot").fetchone()[0],
                         1, "unparseable snapshots never populate the ledger")
        self.assertEqual(self.occurrence(db, "a.txt")["state"], "recoverable")


class ExportValidationTest(RecoverTestCase):
    def test_a_missing_export_is_ledgered_as_orphaned_with_its_reason(self):
        self.drop_file("a.txt", b"alpha")
        self.drop_file("b.txt", b"beta")
        self.run_drain()
        newest = max(self.engine.snapshots(), key=lambda s: s.time)
        self.engine.drop_export(newest.id)

        result, report = self.recover()
        db = self.open_recovered()
        row = records.get_snapshot(db, newest.id)
        self.assertIsNotNone(row, "ledgered before validation")
        self.assertEqual(row["status"], "orphaned")
        self.assertIn("catalog", row["reason"])
        self.assertEqual(row["export_seq"], 2)
        self.assertEqual(records.store_meta(db)["export_seq"], 2)
        # a is recovered from its own snapshot. b's only catalog is gone, so
        # no valid export describes it: it cannot be reconstructed, and that
        # is refused loudly rather than quietly omitted.
        self.assertEqual(self.occurrence(db, "a.txt")["state"], "recoverable")
        self.assertIsNone(db.execute("SELECT 1 FROM occurrence WHERE item_name"
                                     " = 'b.txt'").fetchone())
        refused = [r for r in report.records if r.outcome is Outcome.REFUSED]
        self.assertEqual(len(refused), 1)
        self.assertEqual(refused[0].name, newest.id)
        self.assertIn("unrecoverable", refused[0].reason)
        self.assertEqual(report.exit_code(), 1)
        # The frontier gate still knows the snapshot: not stale.
        self.assertIsNone(self.drain_recovered().run_refusal)

    def test_a_corrupt_newest_export_keeps_the_max_sequence_and_the_gate(self):
        self.drop_file("a.txt", b"alpha")
        self.drop_file("b.txt", b"beta")
        self.run_drain()
        newest = max(self.engine.snapshots(), key=lambda s: s.time)
        export = next(p for p in newest.paths if p.endswith(".sqlite"))
        self.engine.inject_corruption(newest.id, export, flip_byte=True)

        self.recover()
        db = self.open_recovered()
        meta = records.store_meta(db)
        self.assertEqual(meta["export_seq"], 2, "max seq counts invalid exports")
        self.assertEqual(meta["published_frontier"], 1)
        self.assertEqual(records.get_snapshot(db, newest.id)["status"], "orphaned")
        self.assertIn("digest", records.get_snapshot(db, newest.id)["reason"])

        # Unchanged repository → the recovered store is not stale.
        report = self.drain_recovered()
        self.assertIsNone(report.run_refusal, report.run_refusal)
        self.assertEqual(report.exit_code(), 0)


class AttemptHistoryTest(RecoverTestCase):
    def test_a_pending_attempt_with_a_valid_snapshot_is_independently_confirmed(self):
        path = self.drop_file("a.txt", b"alpha")
        self.crash_after("backup-returned")  # snapshot exists, id unrecorded
        self.assertTrue(path.exists())
        self.recover()
        db = self.open_recovered()
        row = self.occurrence(db, "a.txt")
        self.assertEqual(row["state"], "recoverable")
        attempt = records.get_attempt(db, row["confirmed_attempt_id"])
        self.assertEqual(attempt["outcome"], "confirmed")
        self.assertEqual(attempt["snapshot_id"], self.engine.snapshots()[0].id)
        self.assertRegex(attempt["export_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(records.store_meta(db)["published_frontier"], 1)

    def test_a_later_export_recording_failure_excludes_the_attempt(self):
        path = self.drop_file("a.txt", b"alpha")
        self.crash_after("backup-returned")
        first = self.engine.snapshots()[0]
        self.engine.backup(first.paths, first.tags)  # a duplicate publication
        self.run_drain()  # duplicates orphaned, attempt failed, backoff
        report = self.run_drain(now_offset=1000)  # a fresh attempt evicts
        self.assertEqual(report.records[0].outcome, Outcome.ARCHIVED)
        self.assertFalse(path.exists())

        _, report = self.recover()
        db = self.open_recovered()
        row = self.occurrence(db, "a.txt")
        self.assertEqual(row["state"], "recoverable")
        confirmed = records.get_attempt(db, row["confirmed_attempt_id"])
        self.assertEqual(confirmed["export_seq"], 2)
        statuses = {s["snapshot_id"]: (s["status"], s["reason"]) for s in
                    db.execute("SELECT * FROM snapshot")}
        self.assertEqual(len(statuses), 3)
        excluded = [reason for status, reason in statuses.values()
                    if status == "orphaned"]
        self.assertEqual(len(excluded), 2)
        for reason in excluded:
            self.assertIn("later export records the attempt failed", reason)
        self.assertEqual(sum(1 for s, _ in statuses.values() if s == "confirmed"), 1)
        # Never re-verified: the excluded snapshots were not read.
        self.assertFalse(any(call[0] == "dump" and call[1] == first.id
                             and call[2] == str(path)
                             for call in self.engine.calls[-20:]))

    def test_a_recorded_occurrence_without_snapshots_stays_recorded(self):
        self.drop_file("zzz.txt", b"last in name order")
        self.crash_after("recorded")
        self.drop_file("b.txt", b"beta")
        # b runs first and its export carries zzz at `recorded` with no
        # attempt; the crash after b's `verified` leaves zzz untouched.
        with FaultHook("verified"), self.assertRaises(FaultInjected):
            self.run_drain(now_offset=1000)
        _, report = self.recover()
        db = self.open_recovered()
        zzz = self.occurrence(db, "zzz.txt")
        self.assertEqual(zzz["state"], "recorded")
        self.assertEqual(records.attempts_for(db, zzz["occ_id"]), [])
        self.assertEqual(self.occurrence(db, "b.txt")["state"], "recoverable")

    def test_an_occurrence_at_transferred_is_confirmed_from_evidence(self):
        self.drop_file("a.txt", b"alpha")
        self.crash_after("transferred")
        self.recover()
        db = self.open_recovered()
        self.assertEqual(self.occurrence(db, "a.txt")["state"], "recoverable")


class EvictingTest(RecoverTestCase):
    """An `evicting` row is only recoverable as such when a later export saw
    it; the state must have been published by some other item's attempt."""

    def evicting_in_a_later_export(self) -> Path:
        path = self.drop_file("a.txt", b"alpha")
        self.crash_after("intent-written")
        self.assertEqual(self.occurrence(self.db, "a.txt")["state"], "evicting")
        # Hold a open so the next drain retains it in `evicting` while b's
        # attempt exports the store with that state in it.
        self.ownership.hold(str(path), 999)
        self.drop_file("b.txt", b"beta")
        report = self.run_drain(now_offset=1000)
        self.assertEqual({r.name: r.outcome for r in report.records},
                         {"a.txt": Outcome.RETAINED, "b.txt": Outcome.ARCHIVED})
        self.ownership.holders.clear()
        return path

    def test_a_never_exported_evicting_state_recovers_as_recoverable(self):
        path = self.drop_file("a.txt", b"alpha")
        self.crash_after("intent-written")
        self.recover()
        # Its own export only knows `recorded`; evidence proves publication,
        # nothing proves the intent, so it is recoverable and the file stays.
        self.assertEqual(self.occurrence(self.open_recovered(), "a.txt")["state"],
                         "recoverable")
        self.assertTrue(path.exists())

    def test_an_evicting_row_keeps_its_intent_and_is_flagged_for_manual_review(self):
        path = self.evicting_in_a_later_export()
        _, report = self.recover()
        db = self.open_recovered()
        row = self.occurrence(db, "a.txt")
        self.assertEqual(row["state"], "evicting")
        self.assertIsNotNone(row["confirmed_attempt_id"])
        intent = db.execute("SELECT * FROM eviction_intent WHERE occ_id = ?",
                            (row["occ_id"],)).fetchone()
        self.assertEqual(intent["recovered_without_local_history"], 1)
        retained = [r for r in report.records if r.outcome is Outcome.RETAINED]
        self.assertEqual(len(retained), 1)
        self.assertIn("manual intervention", retained[0].reason)
        self.assertEqual(report.exit_code(), 1)
        self.assertTrue(path.exists())

    def test_a_recovered_intent_resumes_only_with_the_root_present_and_verified(self):
        path = self.evicting_in_a_later_export()
        self.recover()
        # Root present, unchanged, ownership clear: the intent pass resumes it.
        report = self.drain_recovered()
        self.assertEqual({r.name: r.outcome for r in report.records},
                         {"a.txt": Outcome.ARCHIVED})
        self.assertFalse(path.exists())

    def test_a_recovered_intent_with_an_absent_root_is_retained_not_evicted(self):
        path = self.evicting_in_a_later_export()
        self.recover()
        path.unlink()  # or: the recovering machine never had the spool
        report = self.drain_recovered()
        record = report.records[0]
        self.assertEqual(record.outcome, Outcome.RETAINED)
        self.assertIn("manual intervention", record.reason)
        self.assertEqual(self.occurrence(self.open_recovered(), "a.txt")["state"],
                         "evicting")


class AbandonedTest(RecoverTestCase):
    def test_a_gate_d_abandoned_occurrence_is_skipped_with_its_evidence_intact(self):
        path = self.drop_file("a.txt", b"alpha")
        import dropin.pipeline.drain as module

        real = module.begin_eviction

        def mutate(connection, occ_id, ownership, spool_path):
            path.write_bytes(b"changed after publication")
            return real(connection, occ_id, ownership, spool_path)

        module.begin_eviction = mutate
        self.addCleanup(setattr, module, "begin_eviction", real)
        self.run_drain()
        module.begin_eviction = real
        self.assertEqual(self.occurrence(self.db, "a.txt")["state"], "abandoned")

        # Publish something afterwards so a later export records the abandon.
        self.drop_file("b.txt", b"beta")
        self.run_drain()

        self.recover()
        db = self.open_recovered()
        row = self.occurrence(db, "a.txt")
        self.assertEqual(row["state"], "abandoned")
        self.assertIsNotNone(row["confirmed_attempt_id"])
        attempt = records.get_attempt(db, row["confirmed_attempt_id"])
        self.assertEqual(attempt["outcome"], "confirmed")
        self.assertEqual(records.get_snapshot(db, attempt["snapshot_id"])
                         ["status"], "confirmed")


class MultiStoreTest(RecoverTestCase):
    def _second_store(self):
        """Another machine's store, adopted into the first's lineage."""
        drop = self.root / "drop-b"
        state = self.root / "state-b"
        for sub in ("", "export", "cache", "tmp"):
            (state / sub).mkdir(parents=True, exist_ok=True)
        drop.mkdir()
        config_path = self.root / "config-b.toml"
        config_path.write_text(CONFIG.format(drop=drop, state=state,
                                             password=self.root / "pw"))
        config = load(config_path)
        db = connect(config.store_path)
        self.addCleanup(db.close)
        store_b = records.initialise_store(db)
        return config, db, store_b, drop

    def test_two_stores_with_sequence_one_each_merge(self):
        self.drop_file("a.txt", b"alpha")
        self.run_drain()
        config_b, db_b, store_b, drop_b = self._second_store()
        # Store B knows A's lineage through sequence 1, so the gate passes.
        records.merge_lineage(db_b, self.store_id, 1)
        item = drop_b / "b.txt"
        item.write_bytes(b"beta")
        self.describe(item)
        context = DrainContext(config_b, db_b, self.engine, self.macos,
                               self.ownership)
        report = Report(verb="drain", run_id="run-b")
        drain(context, report, DrainOptions())
        self.assertEqual(report.records[0].outcome, Outcome.ARCHIVED,
                         report.render_human())

        result, report = self.recover()
        db = self.open_recovered()
        self.assertEqual(records.store_meta(db)["store_id"], store_b,
                         "the newest lineage is the base")
        seqs = sorted((r["origin_store_id"], r["export_seq"]) for r in
                      db.execute("SELECT * FROM publication_attempt"))
        self.assertEqual(seqs, sorted([(self.store_id, 1), (store_b, 1)]))
        self.assertEqual(self.occurrence(db, "a.txt")["state"], "recoverable")
        self.assertEqual(self.occurrence(db, "b.txt")["state"], "recoverable")
        self.assertEqual(records.lineage(db), {self.store_id: 1})
        self.assertEqual(records.store_meta(db)["published_frontier"], 1)

        report = self.drain_recovered()
        self.assertIsNone(report.run_refusal, report.run_refusal)

    def test_an_identifier_naming_two_different_things_fails_closed(self):
        path = self.drop_file("a.txt", b"alpha")
        self.run_drain()
        occ = self.occurrence(self.db, "a.txt")
        # Forge a second store with the same id that reuses the occurrence id
        # for different content, and publishes it under sequence 2.
        state = self.root / "state-forged"
        for sub in ("", "export", "cache", "tmp"):
            (state / sub).mkdir(parents=True, exist_ok=True)
        forged = connect(state / "store.sqlite")
        self.addCleanup(forged.close)
        records.initialise_store(forged, self.store_id)
        from dropin.capture.extract import capture_item
        from dropin.pipeline import attempts

        path.write_bytes(b"different bytes, same identifier")
        self.describe(path)
        records.record_occurrence(forged, capture_item(self.macos, path),
                                  self.store_id, occ_id=occ["occ_id"])
        forged.execute("UPDATE store_meta SET export_seq = 1")
        started = attempts.start(forged, occ["occ_id"], self.store_id,
                                 state / "export")
        identity = attempts.identity_for(forged, started.attempt_id, "file")
        self.engine.backup((str(path), started.export_path), identity.to_tags())

        with self.assertRaises(records.IdentityCollision) as caught:
            self.recover()
        self.assertIn("identifier collision", str(caught.exception))
        self.assertFalse((self.recovered_dir / "store.sqlite").exists())
        self.assertTrue((self.recovered_dir / "recover.tmp").exists(),
                        "the failed workspace is left for inspection")


class TrustLaterExportsTest(RecoverTestCase):
    def test_default_verifies_payloads_and_trust_skips_them(self):
        self.drop_file("a.txt", b"alpha")
        self.run_drain()
        self.drop_file("b.txt", b"beta")
        self.run_drain()  # b's export records a as evicted with a confirmed attempt
        a_snapshot = min(self.engine.snapshots(), key=lambda s: s.time).id

        self.engine.calls.clear()
        self.recover()
        self.assertTrue(any(c[0] == "ls" and c[1] == a_snapshot
                            for c in self.engine.calls))

        import shutil

        shutil.rmtree(self.recovered_dir)
        self.engine.calls.clear()
        self.recover(trust_later_exports=True)
        self.assertFalse(any(c[0] == "ls" and c[1] == a_snapshot
                             for c in self.engine.calls),
                         "a's payload is trusted from b's later valid export")
        # b's own export only records b at `recorded`, so b is still verified.
        b_snapshot = max(self.engine.snapshots(), key=lambda s: s.time).id
        self.assertTrue(any(c[0] == "ls" and c[1] == b_snapshot
                            for c in self.engine.calls))
        db = self.open_recovered()
        self.assertEqual(self.occurrence(db, "a.txt")["state"], "recoverable")
        self.assertEqual(self.occurrence(db, "b.txt")["state"], "recoverable")
