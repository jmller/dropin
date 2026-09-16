"""Contract tests for the guided setup command."""

from pathlib import Path
from unittest import mock
import unittest

from dropin.__main__ import build_parser
from dropin.cli import setup
from tests.support import run_cli


class SetupTest(unittest.TestCase):
    def test_parser_exposes_setup_and_defaults(self):
        args = build_parser().parse_args(["setup"])
        self.assertEqual(args.verb, "setup")
        self.assertIsNone(args.repo)
        self.assertFalse(args.launchd)

    def test_interactive_setup_uses_documented_directory_defaults(self):
        args = build_parser().parse_args(["--config", "/tmp/dropin-config", "setup"])
        answers = iter(["rclone:remote:/archive", "", ""])
        with mock.patch.object(setup.sys.stdin, "isatty", return_value=True), \
                mock.patch.object(setup, "_prompt", side_effect=lambda _: next(answers)), \
                mock.patch.object(setup.init, "run", return_value=0) as run_init:
            self.assertEqual(setup.run(args), 0)
        forwarded = run_init.call_args.args[0]
        self.assertEqual(forwarded.repo, "rclone:remote:/archive")
        self.assertEqual(forwarded.drop_dir, "~/Drop")
        self.assertEqual(forwarded.state_dir, "~/.local/state/dropin")

    def test_existing_config_requires_exact_uppercase_yes(self):
        config_path = Path("/tmp/dropin-setup-existing/config.toml")
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text("existing", encoding="utf-8")
        try:
            args = build_parser().parse_args([
                "--config", str(config_path), "setup",
                "--repo", "rclone:archive:/dropin",
                "--drop-dir", "/tmp/dropin-setup-contract/drop",
                "--state-dir", "/tmp/dropin-setup-contract/state",
            ])
            with mock.patch.object(setup.sys.stdin, "isatty", return_value=True), \
                    mock.patch.object(setup, "_prompt", return_value="yes"), \
                    mock.patch.object(setup.init, "run", return_value=0) as run_init:
                self.assertEqual(setup.run(args), 2)
            run_init.assert_not_called()

            args.force = False
            with mock.patch.object(setup.sys.stdin, "isatty", return_value=True), \
                    mock.patch.object(setup, "_prompt", return_value="YES"), \
                    mock.patch.object(setup.init, "run", return_value=0) as run_init:
                self.assertEqual(setup.run(args), 0)
            self.assertTrue(run_init.call_args.args[0].force)
        finally:
            config_path.unlink(missing_ok=True)
            config_path.parent.rmdir()

    def test_noninteractive_setup_requires_missing_values(self):
        result = run_cli(["setup"], stdin=b"")
        self.assertEqual(result.returncode, 2)
        self.assertIn(b"interactive input required", result.stderr)
        self.assertIn(b"--repo", result.stderr)

    def test_explicit_values_can_be_used_without_prompts(self):
        result = run_cli([
            "--config", "/tmp/dropin-setup-contract/config.toml", "setup",
            "--repo", "rclone:archive:/dropin", "--drop-dir", "/tmp/dropin-setup-contract/drop",
            "--state-dir", "/tmp/dropin-setup-contract/state",
            "--password-file", "/tmp/dropin-setup-contract/password",
        ], env={"DROPIN_ENGINE_FAKE": "1"})
        # The explicit-path case is only a parser/dispatch contract here; the
        # referenced password file intentionally does not exist.
        self.assertEqual(result.returncode, 2)
        self.assertIn(b"password file", result.stderr)


if __name__ == "__main__":
    unittest.main()
