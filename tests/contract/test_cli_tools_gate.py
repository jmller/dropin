"""Tool refusal reports and engine-free CLI/MCP query dispatch."""

from contextlib import ExitStack, redirect_stderr, redirect_stdout
import io
import json
from unittest.mock import PropertyMock, patch

from dropin.__main__ import main
from dropin.cli import Context
from dropin.config import load
from dropin.pipeline.writer_lock import writer_lock
from tests.query_support import QueryTestCase


class ToolsGateTest(QueryTestCase):
    def test_missing_tools_refuse_every_spool_item_without_mutating_catalog(self):
        for name in ("new-a.txt", "new-b.txt"):
            (self.temp.drop_dir / name).write_text("not archived")
        before = list(self.db.iterdump())
        for output in ([], ["--json"]):
            with self.subTest(output=output):
                result = self.cli(*output, "drain")
                self.assertEqual(result.returncode, 3, result.stderr)
                self.assertIn(b"restic", result.stderr)
                if output:
                    rows = [json.loads(line) for line in result.stdout.splitlines()]
                    self.assertEqual([r["name"] for r in rows], ["new-a.txt", "new-b.txt"])
                    self.assertTrue(all(r["outcome"] == "refused" for r in rows))
                    self.assertTrue(all("restic" in r["reason"] for r in rows))
                else:
                    self.assertEqual(result.stdout, b"")
                    for name in (b"new-a.txt", b"new-b.txt"):
                        self.assertIn(name, result.stderr)
                self.assertEqual(list(self.db.iterdump()), before)
                self.assertEqual(sorted(p.name for p in self.temp.drop_dir.iterdir()),
                                 ["new-a.txt", "new-b.txt"])

    def test_below_pin_reports_found_required_and_never_starts_engine(self):
        (self.temp.drop_dir / "queued.txt").write_text("queued")
        restic = self.temp.root / "old-restic"
        rclone = self.temp.root / "old-rclone"
        for path, version in ((restic, "restic 0.18.0"), (rclone, "rclone v1.74.0")):
            path.write_text("#!/bin/sh\nprintf '%s\\n' '" + version + "'\n")
            path.chmod(0o700)
        self.config_path.write_text(self.config_path.read_text().replace(
            "/not-installed/restic", str(restic)).replace("/not-installed/rclone", str(rclone)))
        out, err = io.StringIO(), io.StringIO()
        with patch.object(Context, "engine", new_callable=PropertyMock,
                          side_effect=AssertionError("engine constructed")), redirect_stdout(out), redirect_stderr(err):
            code = main(["--config", str(self.config_path), "--json", "drain"])
        self.assertEqual(code, 3)
        for version in ("0.18.0", "0.19.1"):
            self.assertIn(version, err.getvalue())
        self.assertEqual(json.loads(out.getvalue())["name"], "queued.txt")
        restic.write_text("#!/bin/sh\nprintf '%s\\n' 'restic 0.19.1'\n")
        result = self.cli("--json", "drain")
        self.assertEqual(result.returncode, 3)
        for version in (b"1.74.0", b"1.75.1"):
            self.assertIn(version, result.stderr)
        self.assertEqual(json.loads(result.stdout)["name"], "queued.txt")

    def test_unreadable_spool_does_not_hide_missing_tool_diagnostic(self):
        out, err = io.StringIO(), io.StringIO()
        with patch("dropin.cli.drain.scan", side_effect=PermissionError("spool unreadable")), redirect_stdout(out), redirect_stderr(err):
            code = main(["--config", str(self.config_path), "--json", "drain"])
        self.assertEqual(code, 3)
        self.assertIn("restic", err.getvalue())
        self.assertIn("spool unreadable", err.getvalue())
        self.assertEqual(out.getvalue(), "")

    def test_empty_spool_missing_tools_is_still_loud(self):
        result = self.cli("--json", "drain")
        self.assertEqual(result.returncode, 3)
        self.assertEqual(result.stdout, b"")
        self.assertIn(b"restic", result.stderr)


class LocalOnlyDispatchTest(QueryTestCase):
    def test_queries_never_construct_engine_gate_lock_or_writable_catalog(self):
        path = self.paths["Tax-March.PDF"]
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
                "name": "find", "arguments": {"name": "Tax-March"}}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
                "name": "show", "arguments": {"archive_path": path}}},
        ]
        before = list(self.db.iterdump())
        with ExitStack() as stack:
            for attr in ("engine", "db", "ownership", "macos"):
                stack.enter_context(patch.object(Context, attr, new_callable=PropertyMock,
                                                 side_effect=AssertionError(attr + " constructed")))
            for target in ("dropin.engine.restic.ResticEngine", "dropin.engine.tools.check_tools",
                           "dropin.cli.tools_gate", "dropin.pipeline.writer_lock.writer_lock"):
                stack.enter_context(patch(target, side_effect=AssertionError(target)))
            for args in (["find", "--name", "Tax-March"], ["show", path], ["ls"], ["mcp"]):
                out, err = io.StringIO(), io.StringIO()
                stdin = io.StringIO("".join(json.dumps(r) + "\n" for r in requests))
                with self.subTest(args=args), patch("sys.stdin", stdin), redirect_stdout(out), redirect_stderr(err):
                    self.assertEqual(main(["--config", str(self.config_path), "--json", *args]), 0)
                self.assertEqual(err.getvalue(), "")
                if args == ["mcp"]:
                    rows = [json.loads(line) for line in out.getvalue().splitlines()]
                    self.assertEqual([r["id"] for r in rows], [1, 2, 3])
                    self.assertFalse(rows[1]["result"]["isError"])
                    self.assertFalse(rows[2]["result"]["isError"])
                    self.assertEqual(json.loads(rows[2]["result"]["content"][0]["text"])["archive_path"], path)
                else:
                    self.assertIn(path, out.getvalue())
        self.assertEqual(list(self.db.iterdump()), before)

    def test_queries_work_with_real_writer_lock_held_and_missing_tools(self):
        path = self.paths["Tax-March.PDF"]
        with writer_lock(load(self.config_path).writer_lock_path, verb="drain"):
            for args in (["find", "--name", "Tax-March"], ["show", path], ["ls"]):
                result = self.cli("--json", *args)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(path.encode(), result.stdout)
            request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
                "name": "show", "arguments": {"archive_path": path}}}
            result = self.cli("mcp", stdin=(json.dumps(request) + "\n").encode())
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(json.loads(result.stdout)["result"]["isError"])
