"""Contract tests for the explicit uninstall/purge command."""

from pathlib import Path
import tempfile
import unittest

from dropin.__main__ import build_parser
from tests.support import run_cli


class UninstallTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dropin-uninstall-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.drop = self.root / "drop"
        self.state = self.root / "state"
        self.drop.mkdir()
        self.state.mkdir()
        self.password = self.root / "repo.password"
        self.password.write_text("secret\n")
        self.binary = self.root / "dropin"
        self.binary.write_text("binary")
        self.config = self.root / "config.toml"
        self.config.write_text(f'''[paths]\ndrop_dir = "{self.drop}"\nstate_dir = "{self.state}"\n
[repository]\nrepo = "rclone:archive:/dropin"\npassword_file = "{self.password}"\n
[launchd]\nlabel = "dropin.test.uninstall"\ninterval = 900\n''')

    def test_parser_requires_explicit_purge_switch(self):
        args = build_parser().parse_args(["uninstall"])
        self.assertFalse(args.purge)
        self.assertFalse(args.yes)

    def test_normal_uninstall_removes_binary_but_keeps_data(self):
        result = run_cli(["--config", str(self.config), "uninstall", "--binary", str(self.binary)])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.binary.exists())
        self.assertTrue(self.config.exists())
        self.assertTrue(self.state.exists())

    def test_purge_requires_confirmation_and_keeps_everything(self):
        result = run_cli(["--config", str(self.config), "uninstall", "--purge"], stdin=b"")
        self.assertEqual(result.returncode, 2)
        self.assertTrue(self.config.exists())
        self.assertTrue(self.state.exists())
        self.assertTrue(self.binary.exists())

    def test_purge_removes_local_installation_but_not_drop_by_default(self):
        result = run_cli(["--config", str(self.config), "uninstall", "--purge", "--yes",
                          "--binary", str(self.binary)])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.config.exists())
        self.assertFalse(self.password.exists())
        self.assertFalse(self.state.exists())
        self.assertFalse(self.binary.exists())
        self.assertTrue(self.drop.exists())

    def test_dry_run_does_not_remove_anything(self):
        result = run_cli(["--config", str(self.config), "uninstall", "--purge", "--yes",
                          "--dry-run", "--binary", str(self.binary)])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.config.exists())
        self.assertTrue(self.password.exists())
        self.assertTrue(self.state.exists())
        self.assertTrue(self.binary.exists())


if __name__ == "__main__":
    unittest.main()
