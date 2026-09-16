"""Phase 7 status is a coherent, strictly observational health report."""
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from types import SimpleNamespace
import json
import os
import unittest
from unittest import mock

from dropin.cli import Context
from dropin.config import load
from dropin.engine.fake import FakeEngine
from dropin.engine.interface import Identity
from dropin.engine.tools import ToolGateError
from dropin.macos.fake import FakeMacOS
from dropin.pipeline.writer_lock import writer_lock
from dropin.store import records
from tests.query_support import QueryTestCase
from tests.support import run_cli


class StatusTest(QueryTestCase):
    def setUp(self):
        super().setUp()
        self.engine = FakeEngine()
        self.engine.init()
        self.macos = FakeMacOS()
        self.macos.set_ownership_supported(True)
        self.context = Context(load(self.config_path), json_output=True,
                               _engine=self.engine, _macos=self.macos,
                               _ownership=self.macos)

    def invoke(self, *, offline=False):
        from dropin.cli.status import run
        out, err = StringIO(), StringIO()
        with mock.patch.dict(os.environ, {"DROPIN_ENGINE_FAKE": "1"}), \
             redirect_stdout(out), redirect_stderr(err):
            code = run(self.context, SimpleNamespace(offline=offline))
        return code, json.loads(out.getvalue()), err.getvalue()

    def test_documented_status_json_flag_is_accepted_after_the_verb(self):
        result = run_cli(["--config", str(self.config_path), "status", "--json", "--offline"],
                         env={"DROPIN_ENGINE_FAKE": "1"})
        self.assertNotEqual(result.returncode, 2, result.stderr)
        json.loads(result.stdout)

    def test_documented_json_schema_and_healthy_exit(self):
        code, status, _ = self.invoke()
        self.assertEqual(code, 0)
        self.assertEqual(set(status), {"tools", "adapter", "writer_lock",
            "repository", "frontier", "spool", "records", "resources",
            "last_success", "attention"})
        self.assertEqual(status["adapter"]["macos"], "fake")
        self.assertEqual(status["adapter"]["ownership"], "supported")
        self.assertEqual(status["repository"]["status"], "reachable")
        self.assertEqual(status["frontier"]["status"], "ok")
        self.assertEqual(set(status["records"]["states"]),
                         {"recorded", "transferred", "verified", "recoverable",
                          "evicting", "evicted", "abandoned"})
        self.assertEqual(status["attention"], [])

    def test_offline_never_constructs_or_calls_engine_and_marks_remote_not_checked(self):
        context = Context(load(self.config_path), json_output=True,
                          _macos=self.macos, _ownership=self.macos)
        out = StringIO()
        with mock.patch.dict(os.environ, {"DROPIN_ENGINE_FAKE": "1"}), \
             mock.patch.object(Context, "engine", new_callable=mock.PropertyMock,
                               side_effect=AssertionError("engine constructed")), \
             redirect_stdout(out):
            from dropin.cli.status import run
            code = run(context, SimpleNamespace(offline=True))
        status = json.loads(out.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(status["repository"]["status"], "not-checked")
        self.assertEqual(status["frontier"]["status"], "not-checked")

    def test_online_repository_failure_exits_three_but_offline_does_not_call_it(self):
        self.engine.fail_with("no-repo")
        code, status, _ = self.invoke()
        self.assertEqual(code, 3)
        self.assertEqual(status["repository"]["status"], "unreachable")
        self.engine.calls.clear()
        code, status, _ = self.invoke(offline=True)
        self.assertEqual(code, 0)
        self.assertEqual(status["repository"]["status"], "not-checked")
        self.assertEqual(self.engine.calls, [])

    def test_offline_tool_failure_is_attention_not_unreachable(self):
        out = StringIO()
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch("dropin.cli.status.check_tools",
                        side_effect=ToolGateError("missing")), \
             redirect_stdout(out):
            from dropin.cli.status import run
            code = run(self.context, SimpleNamespace(offline=True))
        status = json.loads(out.getvalue())
        self.assertEqual(code, 1)
        self.assertEqual(status["tools"]["status"], "error")
        self.assertEqual(status["repository"]["status"], "not-checked")

    def test_real_unvalidated_adapter_and_unsupported_ownership_need_attention(self):
        self.macos.validation_state = "unvalidated"
        self.macos.set_ownership_supported(False, "probe failed")
        code, status, _ = self.invoke(offline=True)
        self.assertEqual(code, 1)
        self.assertEqual(status["adapter"]["macos"], "unvalidated")
        self.assertEqual(status["adapter"]["ownership"], "unsupported")
        self.assertIn("macOS adapter is unvalidated", status["attention"])
        self.assertIn("ownership check is unsupported", status["attention"])

    def test_online_tool_gate_failure_is_unreachable_exit_three(self):
        out = StringIO()
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch("dropin.cli.status.check_tools",
                        side_effect=ToolGateError("missing")), \
             redirect_stdout(out):
            from dropin.cli.status import run
            code = run(self.context, SimpleNamespace(offline=False))
        status = json.loads(out.getvalue())
        self.assertEqual(code, 3)
        self.assertEqual(status["repository"]["status"], "unreachable")
        self.assertEqual(status["frontier"]["status"], "not-checked")

    def test_online_frontier_assessment_reports_lineage_mismatch(self):
        other = "f" * 32
        identity = Identity(other, f"{other}.01J0000000000000000000000Z",
                            f"{other}.01J0000000000000000000000Y", 1,
                            "file", "e" * 64)
        from dropin.engine.fake import _Snapshot
        self.engine._snapshots.append(_Snapshot("e" * 64, "2026-01-01T00:00:00Z",
                                                (), tuple(identity.to_tags()), {}))
        code, status, _ = self.invoke()
        self.assertEqual(code, 1)
        self.assertEqual(status["frontier"]["status"], "lineage-mismatch")

    def test_online_frontier_assessment_is_read_only_and_reports_stale(self):
        store_id = records.store_meta(self.db)["store_id"]
        unknown = f"{store_id}.01J0000000000000000000000Z"
        attempt = f"{store_id}.01J0000000000000000000000Y"
        identity = Identity(store_id, unknown, attempt, 999, "file", "f" * 64)
        self.engine._counter += 1
        from dropin.engine.fake import _Snapshot
        self.engine._snapshots.append(_Snapshot("f" * 64, "2026-01-01T00:00:00Z",
                                                (), tuple(identity.to_tags()), {}))
        critical = {table: list(map(tuple, self.db.execute(
                        f"SELECT * FROM {table} ORDER BY 1")))
                    for table in ("occurrence", "publication_attempt", "snapshot")}
        code, status, _ = self.invoke()
        self.assertEqual(code, 1)
        self.assertEqual(status["frontier"]["status"], "stale")
        self.assertEqual(critical, {table: list(map(tuple, self.db.execute(
                            f"SELECT * FROM {table} ORDER BY 1")))
                                    for table in critical})

    def test_counts_dormant_exhausted_unresolved_orphan_and_latest_outcomes(self):
        dormant = self.seed("gone-active.txt", state="recorded", confirmed=False)
        for index in range(self.context.config.max_attempts):
            attempt = records.start_attempt(self.db, dormant,
                records.store_meta(self.db)["store_id"], export_path=f"/tmp/{index}")
            records.finish_attempt(self.db, attempt.attempt_id, "failed", "boom")
        unresolved = self.seed("gone-pending.txt", state="recorded", confirmed=False)
        pending = records.start_attempt(self.db, unresolved,
            records.store_meta(self.db)["store_id"], export_path="/tmp/pending")
        identity = Identity(records.store_meta(self.db)["store_id"], unresolved,
                            pending.attempt_id, pending.export_seq, "file", "a" * 64)
        records.observe_snapshot(self.db, "e" * 64, identity, status="pending")
        records.observe_snapshot(self.db, "d" * 64, identity, status="orphaned",
                                 reason="duplicate")
        unresolved_orphan = self.seed("gone-orphan-only.txt", state="recorded",
                                      confirmed=False)
        orphan_attempt = records.start_attempt(
            self.db, unresolved_orphan, records.store_meta(self.db)["store_id"],
            export_path="/tmp/orphan-only")
        orphan_identity = Identity(records.store_meta(self.db)["store_id"],
            unresolved_orphan, orphan_attempt.attempt_id, orphan_attempt.export_seq,
            "file", "b" * 64)
        records.observe_snapshot(self.db, "c" * 64, orphan_identity,
                                 status="orphaned", reason="catalog")
        self.db.execute("INSERT INTO run(run_id,verb,started_at,finished_at,exit_code) VALUES('latest','drain','2026-01-01','2026-01-02',1)")
        for seq, outcome in enumerate(("deferred", "retained", "refused"), 1):
            self.db.execute("INSERT INTO run_event(run_id,seq,outcome,recorded_at) VALUES('latest',?,?, '2026-01-02')", (seq, outcome))
        code, status, _ = self.invoke()
        self.assertEqual(code, 1)
        self.assertGreaterEqual(status["records"]["dormant"], 2)
        self.assertEqual(status["spool"]["attempts_exhausted"], 1)
        self.assertEqual(status["records"]["unresolved_attempts"], 2)
        self.assertEqual(status["records"]["orphaned_snapshots"], 2)
        self.assertEqual(status["spool"]["deferred"], 1)
        self.assertEqual(status["spool"]["retained"], 1)
        self.assertEqual(status["spool"]["refused_last_run"], 1)

    def test_a_real_held_lock_is_attention_but_stale_advisory_text_is_not(self):
        lock_path = self.context.config.writer_lock_path
        lock_path.write_text('{"pid":999,"verb":"old","since":"then"}')
        code, status, _ = self.invoke()
        self.assertEqual(code, 0)
        self.assertIsNone(status["writer_lock"])
        with writer_lock(lock_path, verb="drain"):
            code, status, _ = self.invoke()
        self.assertEqual(code, 1)
        self.assertEqual(status["writer_lock"]["verb"], "drain")

    def test_resource_overages_and_incomplete_measurement_need_attention(self):
        from dropin.cli import status as status_module
        cache_limit = self.context.config.cache_max_mb * 1024 * 1024
        tmp_limit = (self.context.config.pack_size_mb
                     * (self.context.config.rclone_connections + 1) * 1024 * 1024)
        with mock.patch.object(status_module, "_tree_size",
                               side_effect=[(cache_limit + 1, []),
                                            (tmp_limit + 1, [])]):
            code, status, _ = self.invoke(offline=True)
        self.assertEqual(code, 1)
        self.assertIn("cache exceeds its ceiling", status["attention"])
        self.assertIn("tmp exceeds its transport budget", status["attention"])

        with mock.patch.object(status_module, "_tree_size",
                               side_effect=[(0, ["cache: denied"]), (0, [])]):
            code, status, _ = self.invoke(offline=True)
        self.assertEqual(code, 1)
        self.assertIn("resource measurement incomplete", status["attention"])

    def test_tree_size_reports_traversal_and_lstat_errors(self):
        from dropin.cli.status import _tree_size
        with mock.patch("dropin.cli.status.os.walk",
                        side_effect=PermissionError("walk denied")):
            size, errors = _tree_size(self.context.config.cache_dir)
        self.assertEqual(size, 0)
        self.assertTrue(any("walk denied" in error for error in errors))
        with mock.patch("dropin.cli.status.os.walk",
                        return_value=[("/cache", [], ["bad"])]), \
             mock.patch("dropin.cli.status.os.lstat",
                        side_effect=PermissionError("stat denied")):
            size, errors = _tree_size(self.context.config.cache_dir)
        self.assertEqual(size, 0)
        self.assertTrue(any("stat denied" in error for error in errors))

    def test_spool_scan_failure_needs_attention(self):
        with mock.patch("dropin.cli.status.scan", side_effect=OSError("denied")):
            code, status, _ = self.invoke(offline=True)
        self.assertEqual(code, 1)
        self.assertTrue(any("spool scan failed" in reason
                            for reason in status["attention"]))

    def test_resources_and_last_success_are_reported(self):
        (self.context.config.cache_dir / "cache.bin").write_bytes(b"1234")
        (self.context.config.tmp_dir / "tmp.bin").write_bytes(b"123456")
        self.db.execute("INSERT INTO run(run_id,verb,started_at,finished_at,exit_code,restic_peak_rss_kb) VALUES('drain-ok','drain','2026-01-01','2026-01-02',0,321)")
        self.db.execute("INSERT INTO run(run_id,verb,started_at,finished_at,exit_code) VALUES('verify-ok','verify','2026-01-03','2026-01-04',0)")
        self.db.execute("UPDATE publication_attempt SET finished_at='2026-01-01' WHERE outcome='confirmed'")
        attempt = self.db.execute("SELECT attempt_id FROM publication_attempt WHERE outcome='confirmed' LIMIT 1").fetchone()[0]
        self.db.execute("UPDATE publication_attempt SET finished_at='2026-01-05' WHERE attempt_id=?", (attempt,))
        code, status, _ = self.invoke()
        self.assertEqual(code, 0)
        self.assertEqual(status["resources"]["cache_bytes"], 4)
        self.assertEqual(status["resources"]["tmp_bytes"], 6)
        self.assertEqual(status["resources"]["last_restic_peak_rss_kb"], 321)
        self.assertEqual(status["last_success"], {"drain": "2026-01-02",
                                                  "verify": "2026-01-04",
                                                  "export": "2026-01-05"})


if __name__ == "__main__":
    unittest.main()
