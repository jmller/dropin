"""CLI contract for explicit restore-history initialization and inspection."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from dropin.pipeline.writer_lock import writer_lock
from tests.support import run_cli


class CliRestoreStateTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dropin-cli-restore-state-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.drop = self.root / "drop"
        self.state = self.root / "state"
        self.destination = self.root / "output"
        for path in (self.drop, self.state, self.destination):
            path.mkdir(mode=0o700)
        password = self.root / "password"
        password.write_text("secret")
        password.chmod(0o600)
        self.config = self.root / "config.toml"
        self.config.write_text(f'''[paths]
drop_dir = "{self.drop}"
state_dir = "{self.state}"
[repository]
repo = "rclone:archive:/dropin"
password_file = "{password}"
[tools]
restic = "restic"
rclone = "rclone"
restic_min = "0.19.1"
rclone_min = "1.75.1"
rclone_connections = 2
timeout_seconds = 3600
pack_size_mb = 16
cache_max_mb = 2048
[drain]
settle_seconds = 0
sample_gap_seconds = 0
max_attempts = 3
retry_backoff_seconds = 300
[ownership]
lsof = "lsof"
[launchd]
label = "dev.dropin.drain"
interval = 900
[restore]
destinations = ["{self.destination}"]
''')

    def test_help_registers_restore_state_actions(self):
        result = run_cli(["restore-state", "--help"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(b"init", result.stdout)
        self.assertIn(b"status", result.stdout)

    def test_init_and_status_report_blocked_profile_without_tools(self):
        result = run_cli(["--config", str(self.config), "--json",
                          "restore-state", "init"])
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertRegex(payload["generation"], r"^[0-9a-f]{32}$")
        self.assertEqual(payload["activation"], "blocked")
        self.assertEqual(payload["destinations"][0]["path"], str(self.destination))
        self.assertEqual(payload["blocked_gates"],
                         ["strict-journal", "apfs-persistence", "deployment-acceptance"])

        status = run_cli(["--config", str(self.config), "--json",
                          "restore-state", "status"])
        self.assertEqual(status.returncode, 1, status.stderr)
        self.assertEqual(json.loads(status.stdout), payload)
        self.assertNotIn(b"restic", result.stderr + status.stderr)
        self.assertEqual(list(self.destination.iterdir()), [])

    def test_init_refuses_existing_history_and_lock_contention(self):
        first = run_cli(["--config", str(self.config), "restore-state", "init"])
        self.assertEqual(first.returncode, 0, first.stderr)
        second = run_cli(["--config", str(self.config), "restore-state", "init"])
        self.assertEqual(second.returncode, 2)
        self.assertIn(b"already exists", second.stderr)

        with writer_lock(self.state / "writer.lock", verb="test"):
            blocked = run_cli(["--config", str(self.config),
                               "restore-state", "status"])
        self.assertEqual(blocked.returncode, 3)
        self.assertIn(b"another dropin", blocked.stderr)

    def test_init_requires_explicit_enrollment(self):
        text = self.config.read_text().split("[restore]", 1)[0]
        self.config.write_text(text)
        result = run_cli(["--config", str(self.config), "restore-state", "init"])
        self.assertEqual(result.returncode, 2)
        self.assertIn(b"no restore destinations", result.stderr)
        self.assertFalse((self.state / "restore").exists())


if __name__ == "__main__":
    unittest.main()
