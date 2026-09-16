"""Phase 7 verify audits confirmed occurrences by reading archived payloads."""
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from types import SimpleNamespace
import json
import os
import unittest
from unittest import mock

from dropin.cli import Context
from dropin.config import load
from dropin.engine.interface import EngineError
from dropin.pipeline.writer_lock import writer_lock
from dropin.store import records
from tests.retrieve_support import RetrieveTestCase
from tests.support import run_cli


class VerifyVerbTest(RetrieveTestCase):
    def args(self, *paths, all=False, repo=False, subset=None, since=None):
        return SimpleNamespace(paths=list(paths), all=all, repo=repo,
                               subset=subset, since=since)

    def invoke(self, args):
        from dropin.cli.verify import run
        context = Context(load(self.config_path), json_output=True,
                          _engine=self.engine, _db=self.db)
        out, err = StringIO(), StringIO()
        with mock.patch.dict(os.environ, {"DROPIN_ENGINE_FAKE": "1"}), \
             redirect_stdout(out), redirect_stderr(err):
            code = run(context, args)
        return code, [json.loads(line) for line in out.getvalue().splitlines()], err.getvalue()

    def test_default_and_all_select_every_confirmed_occurrence_including_abandoned(self):
        expected = []
        for name, state in (("recoverable.txt", "recoverable"),
                            ("evicting.txt", "evicting"),
                            ("evicted.txt", "evicted"),
                            ("abandoned.txt", "abandoned")):
            _path, occ, _snapshot, archive = self.archived(name, name.encode())
            self.db.execute("UPDATE occurrence SET state=? WHERE occ_id=?", (state, occ))
            expected.append(archive)
        # An abandoned source before publication is not archived and must not be selected.
        path = self.drop / "prepublication.txt"
        path.write_bytes(b"not published")
        occ = self.record(path)
        self.db.execute("UPDATE occurrence SET state='abandoned' WHERE occ_id=?", (occ,))

        for args in (self.args(), self.args(all=True)):
            with self.subTest(args=args):
                code, rows, _ = self.invoke(args)
                self.assertEqual(code, 0)
                self.assertEqual({row["archive_path"] for row in rows}, set(expected))
                self.assertEqual({row["outcome"] for row in rows}, {"verified"})

    def test_explicit_descendants_deduplicate_to_one_complete_tree_audit(self):
        _path, occ, _snapshot, root_archive = self.archived("tree", tree=True)
        child = self.db.execute(
            "SELECT archive_path FROM entry WHERE occ_id=? AND rel_path='sub/a'", (occ,)).fetchone()[0]
        code, rows, _ = self.invoke(self.args(child, root_archive, child))
        self.assertEqual(code, 0)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["archive_path"], root_archive)

    def test_confirmed_attempt_without_snapshot_and_missing_payload_are_missing(self):
        path, occ, snapshot, archive = self.archived("missing.txt", b"missing")
        attempt = records.get_occurrence(self.db, occ)["confirmed_attempt_id"]
        self.db.execute("UPDATE publication_attempt SET snapshot_id=NULL WHERE attempt_id=?",
                        (attempt,))
        code, rows, _ = self.invoke(self.args(archive))
        self.assertEqual((code, rows[0]["outcome"]), (4, "missing"))
        self.db.execute("UPDATE publication_attempt SET snapshot_id=? WHERE attempt_id=?",
                        (snapshot, attempt))
        self.engine._find(snapshot).dropped.add(str(path))
        code, rows, _ = self.invoke(self.args(archive))
        self.assertEqual((code, rows[0]["outcome"]), (4, "missing"))

    def test_corrupt_and_missing_records_continue_and_exit_four(self):
        first_path, _occ, first_snapshot, first_archive = self.archived("bad.txt", b"bad")
        self.engine.inject_corruption(first_snapshot, str(first_path))
        _path, _occ, _snapshot, good_archive = self.archived("good.txt", b"good")
        code, rows, _ = self.invoke(self.args(first_archive, "unknown/path", good_archive))
        self.assertEqual(code, 4)
        self.assertEqual([row["outcome"] for row in rows],
                         ["corrupt", "missing", "verified"])

    def test_since_is_inclusive_on_recorded_at(self):
        _path, old_occ, _snapshot, old_archive = self.archived("old.txt", b"old")
        _path, new_occ, _snapshot, new_archive = self.archived("new.txt", b"new")
        self.db.execute("UPDATE occurrence SET recorded_at='2026-01-01T00:00:00Z' WHERE occ_id=?", (old_occ,))
        self.db.execute("UPDATE occurrence SET recorded_at='2026-02-01T00:00:00Z' WHERE occ_id=?", (new_occ,))
        code, rows, _ = self.invoke(self.args(since="2026-02-01"))
        self.assertEqual(code, 0)
        self.assertEqual([row["archive_path"] for row in rows], [new_archive])
        self.assertNotEqual(old_archive, new_archive)

    def test_preflight_repository_refusal_has_no_item_outcomes_and_exit_three(self):
        self.archived("unreached.txt", b"unreached")
        self.engine.fail_with("no-repo")
        code, rows, error = self.invoke(self.args())
        self.assertEqual(code, 3)
        self.assertEqual(rows, [])
        self.assertIn("no-repo", error)
        self.assertIsNotNone(self.db.execute(
            "SELECT 1 FROM run WHERE verb='verify' AND exit_code=3").fetchone())

    def test_repo_check_failure_is_separate_corruption_and_exit_four(self):
        self.archived("checked-bad.txt", b"checked")
        with mock.patch.object(self.engine, "check",
                               side_effect=EngineError("corrupt", "damaged")):
            code, rows, _ = self.invoke(self.args(repo=True))
        self.assertEqual(code, 4)
        self.assertEqual(rows[-1]["name"], "repository")
        self.assertEqual(rows[-1]["outcome"], "corrupt")
        self.assertEqual(rows[0]["outcome"], "verified")

    def test_repo_check_is_additive_and_defaults_to_full_data(self):
        self.archived("checked.txt", b"checked")
        code, rows, _ = self.invoke(self.args(repo=True))
        self.assertEqual(code, 0)
        self.assertEqual([call for call in self.engine.calls if call[0] == "check"],
                         [("check", "1/1")])
        self.assertEqual([row["name"] for row in rows].count("repository"), 1)
        self.engine.calls.clear()
        code, _rows, _ = self.invoke(self.args(repo=True, subset="2/3"))
        self.assertEqual(code, 0)
        self.assertIn(("check", "2/3"), self.engine.calls)

    def test_audit_does_not_change_archive_truth_and_records_run_history(self):
        self.archived("stable.txt", b"stable")
        # Compare the safety-critical tables directly after the run.
        critical = {table: list(map(tuple, self.db.execute(f"SELECT * FROM {table} ORDER BY 1")))
                    for table in ("occurrence", "publication_attempt", "snapshot")}
        code, _rows, _ = self.invoke(self.args())
        self.assertEqual(code, 0)
        self.assertEqual(critical, {table: list(map(tuple, self.db.execute(f"SELECT * FROM {table} ORDER BY 1")))
                                    for table in critical})
        self.assertIsNotNone(self.db.execute(
            "SELECT 1 FROM run WHERE verb='verify' AND exit_code=0").fetchone())

    def test_invalid_combinations_are_usage_errors(self):
        env = {"DROPIN_ENGINE_FAKE": "1"}
        both = run_cli(["--config", str(self.config_path), "verify", "x", "--all"], env=env)
        self.assertEqual(both.returncode, 2)
        subset = run_cli(["--config", str(self.config_path), "verify", "--subset", "1/2"], env=env)
        self.assertEqual(subset.returncode, 2)
        for value in ("0/2", "١/٢"):
            malformed = run_cli(["--config", str(self.config_path), "verify", "--repo",
                                 "--subset", value], env=env)
            self.assertEqual(malformed.returncode, 2, value)

    def test_dispatcher_refuses_a_held_writer_lock_before_engine_access(self):
        with writer_lock(self.config_path.parent / "state" / "writer.lock", verb="drain"):
            result = run_cli(["--config", str(self.config_path), "verify", "--all"],
                             env={"DROPIN_ENGINE_FAKE": "1"})
        self.assertEqual(result.returncode, 3)
        self.assertIn(b"another dropin drain", result.stderr)


if __name__ == "__main__":
    unittest.main()
