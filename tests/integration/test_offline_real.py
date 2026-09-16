"""Real pinned restic/rclone, nonexistent remote, disposable state only.

No real macOS ownership/eviction validation: use the existing real-tool fixture
(fake Spotlight; Linux ownership on Linux, fake ownership elsewhere).
"""

import json
import os
import unittest

from dropin.engine.tools import check_tools
from tests.integration import test_restic_roundtrip as roundtrip
from tests.support import run_cli


@unittest.skipUnless(os.environ.get("DROPIN_RESTIC_BIN") and os.environ.get("DROPIN_RCLONE_BIN"),
                     "pinned tool environment not provided")
class OfflineRealTest(roundtrip.ResticTestCase):
    def test_absent_remote_refuses_each_item_and_local_queries_survive(self):
        check_tools(self.config)
        self.drop_file("archived.txt")
        report = self.run_drain()
        self.assertEqual(report.exit_code(), 0, report.render_human())
        archive_path = self.occurrence("archived.txt")["archive_path"]
        original_config = self.config_path.read_text()
        queued = [self.drop_file("queued-a.txt"), self.drop_file("queued-b.txt")]
        before = [p.stat() for p in queued]
        self.config_path.write_text(original_config.replace(f"rclone:local:{self.repo}",
                                                           "rclone:phase6-absent:/archive"))
        result = run_cli(["--config", str(self.config_path), "--json", "drain"],
                         env={"DROPIN_ENGINE_FAKE": "", "DROPIN_MACOS_FAKE": "1"})
        self.assertEqual(result.returncode, 3, result.stderr)
        rows = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual([r["name"] for r in rows], ["queued-a.txt", "queued-b.txt"])
        for row in rows:
            self.assertEqual(row["outcome"], "refused")
            self.assertIn("phase6-absent", row["reason"])
        self.assertEqual([p.stat() for p in queued], before)
        # The pipeline may record the refused run; immutable metadata is unchanged.
        self.assertEqual(self.db.execute("SELECT count(*) FROM occurrence").fetchone()[0], 1)
        for args in (["find", "--name", "archived"], ["show", archive_path], ["ls"]):
            query = run_cli(["--config", str(self.config_path), "--json", *args])
            self.assertEqual(query.returncode, 0, query.stderr)
            self.assertIn(archive_path.encode(), query.stdout)
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "show", "arguments": {"archive_path": archive_path}}}
        query = run_cli(["--config", str(self.config_path), "mcp"],
                        stdin=(json.dumps(request) + "\n").encode())
        self.assertEqual(query.returncode, 0, query.stderr)
        self.assertFalse(json.loads(query.stdout)["result"]["isError"])
        self.config_path.write_text(original_config)
        self.assertEqual(self.run_drain().exit_code(), 0)
        self.assertTrue(all(not p.exists() for p in queued))
