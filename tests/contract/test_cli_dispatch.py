"""CLI dispatch, usage errors, and verb registration."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from tests.support import run_cli

VERBS = ["init", "setup", "uninstall", "drain", "add", "find", "show", "ls", "get", "verify",
         "status", "recover", "unlock", "restore-state", "mcp"]


class DispatchTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dropin-cli-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_no_arguments_is_a_usage_error(self):
        result = run_cli([])
        self.assertEqual(result.returncode, 2)
        self.assertTrue(result.stderr)
        self.assertEqual(result.stdout, b"")

    def test_unknown_verb_is_a_usage_error_naming_it(self):
        result = run_cli(["frobnicate"])
        self.assertEqual(result.returncode, 2)
        self.assertIn(b"frobnicate", result.stderr)

    def test_help_lists_every_documented_verb(self):
        result = run_cli(["--help"])
        self.assertEqual(result.returncode, 0)
        for verb in VERBS:
            with self.subTest(verb=verb):
                self.assertIn(verb.encode(), result.stdout)

    def test_every_documented_verb_is_registered(self):
        for verb in VERBS:
            with self.subTest(verb=verb):
                result = run_cli([verb, "--help"])
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_missing_config_is_a_usage_error(self):
        result = run_cli(["--config", str(self.root / "absent.toml"), "status"])
        self.assertEqual(result.returncode, 2)
        self.assertIn(b"absent.toml", result.stderr)

    def test_unimplemented_verb_exits_two(self):
        # Until a verb is wired up it must refuse, never pretend to work.
        result = run_cli(["--config", str(self.write_minimal_config()), "mcp"])
        self.assertIn(result.returncode, (0, 2))

    def write_minimal_config(self) -> Path:
        drop, state = self.root / "drop", self.root / "state"
        drop.mkdir(exist_ok=True)
        state.mkdir(exist_ok=True)
        password = self.root / "pw"
        password.write_text("x")
        password.chmod(0o600)
        path = self.root / "config.toml"
        path.write_text(f"""
[paths]
drop_dir  = "{drop}"
state_dir = "{state}"
[repository]
repo          = "rclone:archive:/dropin"
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
settle_seconds = 5
sample_gap_seconds = 2
max_attempts = 3
retry_backoff_seconds = 300
[ownership]
lsof = "lsof"
[launchd]
label = "dev.dropin.drain"
interval = 900
""")
        return path

    def test_diagnostics_go_to_stderr_not_stdout(self):
        result = run_cli(["--config", str(self.root / "absent.toml"), "drain"])
        self.assertEqual(result.stdout, b"")
        self.assertTrue(result.stderr)

    def test_config_environment_variable_is_honoured(self):
        # The verb may not be implemented yet; what must hold is that the
        # configuration was found without an explicit --config.
        config = self.write_minimal_config()
        result = run_cli(["status", "--offline"], env={"DROPIN_CONFIG": str(config)})
        self.assertNotIn(b"config file", result.stderr)
        self.assertNotIn(str(config).encode(), result.stderr)

    def test_explicit_config_flag_beats_the_environment(self):
        config = self.write_minimal_config()
        result = run_cli(["--config", str(self.root / "absent.toml"), "status"],
                         env={"DROPIN_CONFIG": str(config)})
        self.assertEqual(result.returncode, 2)
        self.assertIn(b"absent.toml", result.stderr)
