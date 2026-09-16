"""Only the explicit unlock verb may remove stale repository locks."""
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
import ast
import json
import os
import unittest
from unittest import mock

from dropin.cli import Context
from dropin.config import load
from dropin.engine.fake import FakeEngine
from dropin.engine.interface import EngineError
from dropin.pipeline.writer_lock import writer_lock
from tests.query_support import QueryTestCase
from tests.support import run_cli


class UnlockContractTest(QueryTestCase):
    def invoke(self):
        from dropin.cli.unlock import run
        engine = FakeEngine()
        engine.init()
        context = Context(load(self.config_path), json_output=True, _engine=engine)
        out, err = StringIO(), StringIO()
        with mock.patch.dict(os.environ, {"DROPIN_ENGINE_FAKE": "1"}), \
             redirect_stdout(out), redirect_stderr(err):
            code = run(context, SimpleNamespace())
        return code, json.loads(out.getvalue()), err.getvalue(), engine.calls

    def test_success_calls_only_engine_unlock_once_and_reports_backend_result(self):
        code, row, error, calls = self.invoke()
        self.assertEqual(code, 0)
        self.assertEqual(error, "")
        self.assertEqual([call for call in calls if call[0] != "init"],
                         [("unlock",)])
        self.assertEqual(row["verb"], "unlock")
        self.assertEqual(row["outcome"], "info")
        self.assertIn("lock", row["reason"])

    def test_empty_backend_output_gets_a_truthful_neutral_acknowledgement(self):
        from dropin.cli.unlock import run
        engine = FakeEngine()
        engine.init()
        context = Context(load(self.config_path), json_output=True, _engine=engine)
        out = StringIO()
        with mock.patch.dict(os.environ, {"DROPIN_ENGINE_FAKE": "1"}), \
             mock.patch.object(engine, "unlock", return_value=""), \
             redirect_stdout(out):
            code = run(context, SimpleNamespace())
        self.assertEqual(code, 0)
        self.assertIn("completed", json.loads(out.getvalue())["reason"])

    def test_backend_error_is_a_run_refusal_without_success_output(self):
        from dropin.cli.unlock import run
        engine = FakeEngine()
        engine.init()
        context = Context(load(self.config_path), json_output=True, _engine=engine)
        out, err = StringIO(), StringIO()
        with mock.patch.dict(os.environ, {"DROPIN_ENGINE_FAKE": "1"}), \
             mock.patch.object(engine, "unlock",
                               side_effect=EngineError("locked", "still active")), \
             redirect_stdout(out), redirect_stderr(err):
            code = run(context, SimpleNamespace())
        self.assertEqual(code, 3)
        self.assertEqual(out.getvalue(), "")
        self.assertIn("still active", err.getvalue())

    def test_dispatcher_holds_the_writer_lock_before_unlock(self):
        with writer_lock(self.config_path.parent / "state" / "writer.lock", verb="verify"):
            result = run_cli(["--config", str(self.config_path), "unlock"],
                             env={"DROPIN_ENGINE_FAKE": "1"})
        self.assertEqual(result.returncode, 3)
        self.assertIn(b"another dropin verify", result.stderr)

    def test_no_other_production_caller_invokes_unlock(self):
        callers = []
        root = Path(__file__).resolve().parents[2] / "dropin"
        excluded = {root / "engine" / "interface.py", root / "engine" / "fake.py",
                    root / "engine" / "restic.py"}
        for path in root.rglob("*.py"):
            if path in excluded:
                continue
            tree = ast.parse(path.read_text(), filename=str(path))
            if any(isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                   and node.func.attr == "unlock" for node in ast.walk(tree)):
                callers.append(path.relative_to(root).as_posix())
        self.assertEqual(callers, ["cli/unlock.py"])


if __name__ == "__main__":
    unittest.main()
