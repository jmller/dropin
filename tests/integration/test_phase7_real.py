"""Phase 7 smoke against pinned restic/rclone over the local backend."""
import json
import os
import sys
import unittest

from tests.integration import test_restic_roundtrip as roundtrip
from tests.support import run_cli


@unittest.skipUnless(os.environ.get("DROPIN_RESTIC_BIN") and
                     os.environ.get("DROPIN_RCLONE_BIN"),
                     "pinned tool environment not provided")
class Phase7RealTest(roundtrip.ResticTestCase):
    def cli(self, *args):
        return run_cli(["--config", str(self.config_path), *args])

    def test_verify_status_and_unlock_use_the_real_adapter_contract(self):
        path = self.drop_file("audit.txt", b"archive audit\n")
        report = self.run_drain()
        self.assertEqual(report.exit_code(), 0, report.render_human())
        self.assertFalse(path.exists())

        verified = self.cli("--json", "verify", "--all", "--repo")
        self.assertEqual(verified.returncode, 0, verified.stderr)
        rows = [json.loads(line) for line in verified.stdout.splitlines()]
        self.assertEqual([row["outcome"] for row in rows],
                         ["verified", "verified"])
        self.assertEqual(rows[-1]["name"], "repository")

        status = self.cli("status", "--json")
        health = json.loads(status.stdout)
        self.assertEqual(health["repository"]["status"], "reachable")
        self.assertEqual(health["last_success"]["verify"] is not None, True)
        if sys.platform == "darwin":
            self.assertNotEqual(health["adapter"]["macos"], "unvalidated")
            if health["adapter"]["ownership"] == "supported":
                self.assertEqual(status.returncode, 0, status.stderr)
                self.assertEqual(health["attention"], [])
            else:
                self.assertEqual(status.returncode, 1, status.stderr)
                self.assertIn("ownership check is unsupported", health["attention"])
        else:
            self.assertEqual(status.returncode, 0, status.stderr)
            self.assertEqual(health["attention"], [])

        unlocked = self.cli("--json", "unlock")
        self.assertEqual(unlocked.returncode, 0, unlocked.stderr)
        row = json.loads(unlocked.stdout)
        self.assertEqual((row["verb"], row["outcome"]), ("unlock", "info"))


if __name__ == "__main__":
    unittest.main()
