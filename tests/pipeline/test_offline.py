"""Offline refusal boundaries and reconnect metadata preservation."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from unittest.mock import patch

from dropin.cli import emit
from dropin.engine.interface import EngineError
from dropin.pipeline import attempts
from dropin.pipeline.faults import FaultInjected
from dropin.query.search import show
from dropin.report import Outcome, Report
from dropin.store import records
from tests.pipeline.test_drain import DrainTestCase
from tests.support import FaultHook

KINDS = ("no-repo", "tool-error", "locked", "bad-password")


class OfflineTest(DrainTestCase):
    def setUp(self):
        super().setUp()
        # Config canonicalizes /var -> /private/var on macOS; fake keys and
        # injected path comparisons must use the same spelling as the driver.
        self.drop = self.config.drop_dir

    def test_preflight_refuses_each_observed_item_without_capture_or_deletion(self):
        paths = [self.drop_file("a.txt"), self.drop_file("b.txt")]
        before = [p.stat() for p in paths]
        for kind in KINDS:
            with self.subTest(kind=kind), patch.object(
                    self.engine, "snapshots", side_effect=EngineError(
                        kind, "unavailable", "backend diagnostic; pid 4321 on host test")):
                report = self.run_drain()
                self.assertEqual(report.exit_code(), 3)
                self.assertEqual([r.name for r in report.records], ["a.txt", "b.txt"])
                self.assertTrue(all(r.outcome == Outcome.REFUSED for r in report.records))
                self.assertTrue(all(kind in r.reason and "pid 4321" in r.reason
                                    for r in report.records))
                self.assertEqual(self.db.execute("SELECT count(*) FROM occurrence").fetchone()[0], 0)
                self.assertEqual(self.db.execute("SELECT count(*) FROM publication_attempt").fetchone()[0], 0)
                self.assertEqual([p.stat() for p in paths], before)
                self.assertEqual(self.db.execute("SELECT exit_code FROM run").fetchone()[0], 3)
        self.assertFalse(any(c[0] in ("backup", "unlock") for c in self.engine.calls))

    def test_preflight_scan_failure_preserves_original_repository_refusal(self):
        with patch.object(self.engine, "snapshots", side_effect=EngineError("locked", "busy")), patch(
                "dropin.pipeline.drain.scan", side_effect=PermissionError("spool unreadable")):
            report = self.run_drain()
        self.assertEqual(report.exit_code(), 3)
        self.assertIn("locked", report.run_refusal)
        self.assertIn("spool unreadable", report.run_refusal)
        self.assertEqual(report.records, [])

    def test_backend_diagnostics_are_bounded_and_not_fabricated(self):
        for tail in ("", "omitted-old-line\n" + "\n".join("detail" for _ in range(19)) + "\nlatest-line",
                     "omitted-prefix" + "x" * 9000 + "last"):
            with self.subTest(tail_length=len(tail)), patch.object(
                    self.engine, "snapshots", side_effect=EngineError("locked", "busy", tail)):
                report = self.run_drain()
            reason = report.run_refusal
            if not tail:
                self.assertEqual(reason, "repository: locked: busy")
                self.assertNotIn("pid", reason)
            else:
                self.assertNotIn("omitted-", reason)
                self.assertLessEqual(len(reason), len("repository: locked: busy\n") + 8192)
                self.assertTrue(reason.endswith(tail.splitlines()[-1][-8192:]))

    def test_empty_spool_preflight_still_reports_global_failure(self):
        with patch.object(self.engine, "snapshots", side_effect=EngineError("locked", "busy")):
            report = self.run_drain()
        self.assertEqual(report.exit_code(), 3)
        self.assertIn("locked", report.run_refusal)
        self.assertEqual(report.records, [])

    def test_backup_failure_continues_and_reconnect_keeps_original_metadata(self):
        first = self.drop_file("a.txt")
        second = self.drop_file("b.txt")
        self.macos.set_importer(str(first), 'Attributes: {\n originalKey = "captured offline";\n}\n')
        original_backup = self.engine.backup
        for kind in KINDS:
            # Isolate kinds with a fresh failed item; the first loop also proves
            # a later independent item succeeds in the same drain.
            if kind != KINDS[0]:
                first = self.drop_file(kind + ".txt")
                self.macos.set_importer(str(first), 'Attributes: {\n originalKey = "captured offline";\n}\n')
            def fail_selected(paths, tags):
                if paths[0] == str(first):
                    raise EngineError(kind, "backup uncertain", "backend offline detail")
                return original_backup(paths, tags)
            with self.subTest(kind=kind), patch.object(self.engine, "backup", side_effect=fail_selected):
                report = self.run_drain()
            self.assertEqual(report.exit_code(), 1)
            refused = next(r for r in report.records if r.name == first.name)
            self.assertIn("backend offline detail", refused.reason)
            self.assertTrue(first.exists())
            self.assertFalse(second.exists())
            occurrence = self.db.execute("SELECT * FROM occurrence WHERE spool_path=?", (str(first),)).fetchone()
            occ_id = occurrence["occ_id"]
            recorded_at = "2026-01-01T00:00:00Z"
            self.db.execute("UPDATE occurrence SET recorded_at=? WHERE occ_id=?", (recorded_at, occ_id))
            self.assertEqual(occurrence["state"], "recorded")
            self.assertEqual(self.db.execute("SELECT outcome FROM publication_attempt WHERE occ_id=?", (occ_id,)).fetchone()[0], "pending")
            self.macos.set_importer(str(first), 'Attributes: {\n originalKey = "changed later";\n}\n')
            self.run_drain()  # resolves absent snapshot, then honors retry backoff
            report = self.run_drain(now_offset=301)
            self.assertEqual(report.exit_code(), 0, report.render_human())
            after = records.get_occurrence(self.db, occ_id)
            self.assertEqual(after["recorded_at"], recorded_at)
            self.assertEqual(after["state"], "evicted")
            self.assertFalse(first.exists())
            self.assertEqual(show(self.db, archive_path=after["archive_path"])["attributes"]["originalKey"]["value"], "captured offline")
            self.assertEqual(self.db.execute("SELECT count(*) FROM publication_attempt WHERE occ_id=? AND outcome='confirmed'", (occ_id,)).fetchone()[0], 1)

    def test_late_repository_error_fails_pending_but_other_item_completes(self):
        for kind in KINDS:
            bad = self.drop_file(kind + "-a.txt")
            good = self.drop_file(kind + "-b.txt")
            original_ls = self.engine.ls
            def fail_selected(snapshot, path):
                if path == str(bad):
                    raise EngineError(kind, "ls failed", "holder pid 4321")
                return original_ls(snapshot, path)
            with self.subTest(kind=kind), patch.object(self.engine, "ls", side_effect=fail_selected):
                report = self.run_drain()
            self.assertEqual(report.exit_code(), 1)
            self.assertTrue(bad.exists())
            self.assertFalse(good.exists())
            refused = next(r for r in report.records if r.name == bad.name)
            self.assertIn("holder pid 4321", refused.reason)
            row = self.db.execute("SELECT * FROM occurrence WHERE spool_path=?", (str(bad),)).fetchone()
            self.assertEqual(row["state"], "recorded")
            self.assertEqual(self.db.execute("SELECT outcome FROM publication_attempt WHERE occ_id=?", (row["occ_id"],)).fetchone()[0], "failed")
            self.assertEqual(self.db.execute("SELECT status FROM snapshot WHERE occ_id=?", (row["occ_id"],)).fetchone()[0], "orphaned")
            bad.unlink()  # caller removes this failed source before the next case

    def dormant(self, name):
        path = self.drop_file(name)
        with FaultHook("attempt-started"), self.assertRaises(FaultInjected):
            self.run_drain()
        row = self.db.execute("SELECT * FROM occurrence WHERE spool_path=?", (str(path),)).fetchone()
        attempt = next(a for a in records.pending_attempts(self.db) if a["occ_id"] == row["occ_id"])
        path.unlink()
        return row["occ_id"], attempt["attempt_id"]

    def test_dormant_error_without_any_item_is_run_refusal_and_keeps_pending(self):
        occ_id, attempt_id = self.dormant("gone.txt")
        original = self.engine.snapshots
        def offline(tag=None):
            if tag == f"dropin:attempt={attempt_id}":
                raise EngineError("no-repo", "offline")
            return original(tag)
        with patch.object(self.engine, "snapshots", side_effect=offline):
            report = self.run_drain()
        self.assertEqual(report.exit_code(), 3)
        self.assertEqual(report.records, [])
        self.assertIn(attempt_id, report.run_refusal)
        self.assertEqual(records.get_occurrence(self.db, occ_id)["state"], "recorded")
        self.assertEqual(records.get_attempt(self.db, attempt_id)["outcome"], "pending")
        self.assertEqual(self.db.execute("SELECT exit_code FROM run").fetchone()[0], 3)

    def test_cache_information_does_not_count_as_an_item_for_dormant_failure(self):
        _, attempt_id = self.dormant("gone.txt")
        (self.config.cache_dir / "oversized").write_bytes(b"xx")
        original = self.engine.snapshots
        def offline(tag=None):
            if tag == f"dropin:attempt={attempt_id}":
                raise EngineError("tool-error", "offline")
            return original(tag)
        with patch.object(self.engine, "snapshots", side_effect=offline):
            report = self.run_drain(cache_max_bytes=1)
        self.assertEqual(report.exit_code(), 3)
        self.assertEqual([r.outcome for r in report.records], [Outcome.INFO])
        self.assertIsNone(report.run_error)

    def test_intent_pass_counts_as_item_before_dormant_failure(self):
        _, attempt_id = self.dormant("gone.txt")
        path = self.drop_file("intent.txt")
        original = self.engine.snapshots
        def offline(tag=None):
            if tag == f"dropin:attempt={attempt_id}":
                raise EngineError("tool-error", "offline")
            return original(tag)
        # Skip dormant settlement while arranging an interrupted live intent.
        with FaultHook("intent-written"), self.assertRaises(FaultInjected):
            self.run_drain()
        path.unlink()  # live intent can reconcile an absent root
        with patch.object(self.engine, "snapshots", side_effect=offline):
            report = self.run_drain()
        self.assertEqual(report.exit_code(), 1)
        self.assertEqual([r.name for r in report.records], ["intent.txt"])
        self.assertIn(attempt_id, report.run_error)
        self.assertIsNone(report.run_refusal)

    def test_multiple_dormant_errors_after_success_continue_without_item_outcomes(self):
        # Construct multiple dormant intents directly after capture, avoiding an
        # earlier drain settling the first while creating the second.
        ids = []
        for name in ("gone-a", "gone-b", "gone-c"):
            path = self.drop_file(name)
            from dropin.capture.extract import capture_item
            occ_id = records.record_occurrence(self.db, capture_item(self.macos, path), self.store_id)
            ids.append(attempts.start(self.db, occ_id, self.store_id, self.config.export_dir).attempt_id)
            path.unlink()
        live = self.drop_file("live.txt")
        original = self.engine.snapshots
        def offline(tag=None):
            if tag in {f"dropin:attempt={a}" for a in ids[:2]}:
                raise EngineError("locked", "busy", "pid 4321")
            return original(tag)
        with patch.object(self.engine, "snapshots", side_effect=offline):
            report = self.run_drain()
        self.assertEqual(report.exit_code(), 1)
        self.assertFalse(live.exists())
        self.assertEqual([r.name for r in report.records], ["live.txt"])
        self.assertIsNone(report.run_refusal)
        for attempt_id in ids[:2]:
            self.assertEqual(records.get_attempt(self.db, attempt_id)["outcome"], "pending")
            self.assertIn(attempt_id, report.run_error)
        self.assertEqual(records.get_attempt(self.db, ids[2])["outcome"], "failed")
        self.assertEqual(self.db.execute("SELECT exit_code FROM run").fetchone()[0], 1)
        self.context.json_output = True
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(emit(self.context, report), 1)
        self.assertEqual(json.loads(out.getvalue())["name"], "live.txt")
        self.assertIn("pid 4321", err.getvalue())
        self.assertIn(ids[0], err.getvalue())

    def test_dormant_error_after_capture_refusal_is_item_failure_not_preflight(self):
        _, attempt_id = self.dormant("gone.txt")
        self.drop_file("live.txt")
        original = self.engine.snapshots
        def offline(tag=None):
            if tag == f"dropin:attempt={attempt_id}":
                raise EngineError("tool-error", "offline")
            return original(tag)
        with patch.object(self.engine, "snapshots", side_effect=offline), patch(
                "dropin.pipeline.drain.capture_item", side_effect=PermissionError("unreadable")):
            report = self.run_drain()
        self.assertEqual(report.exit_code(), 1)
        self.assertIsNone(report.run_refusal)
        self.assertIn(attempt_id, report.run_error)
        self.assertEqual([r.outcome for r in report.records], [Outcome.REFUSED])


class ObservationReportTest(DrainTestCase):
    def test_run_error_preserves_verification_and_refusal_precedence_and_streams(self):
        for json_output in (False, True):
            for outcome, expected in ((None, 1), (Outcome.ARCHIVED, 1),
                                      (Outcome.CORRUPT, 4), (Outcome.MISSING, 4)):
                with self.subTest(json=json_output, outcome=outcome):
                    report = Report("drain", "id", run_error="observation failed")
                    if outcome:
                        report.item(outcome, "item")
                    self.context.json_output = json_output
                    out, err = io.StringIO(), io.StringIO()
                    with redirect_stdout(out), redirect_stderr(err):
                        self.assertEqual(emit(self.context, report), expected)
                    self.assertIn("observation failed", err.getvalue())
                    if json_output and outcome:
                        self.assertEqual(json.loads(out.getvalue())["outcome"], outcome.value)
                    report.run_refusal = "preflight"
                    self.assertEqual(report.exit_code(), 3)
