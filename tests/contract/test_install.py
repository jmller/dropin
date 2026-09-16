"""Pip-free installation and packaged resource contract."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile


ROOT = Path(__file__).resolve().parents[2]


class UserInstallTest(unittest.TestCase):
    def test_installed_command_runs_from_outside_checkout_and_has_schema(self):
        with tempfile.TemporaryDirectory(prefix="dropin-install-test-") as raw:
            root = Path(raw)
            target = root / "bin" / "dropin"
            installed = subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "install-user.py"),
                 "--target", str(target)],
                cwd="/", text=True, capture_output=True,
            )
            self.assertEqual(installed.returncode, 0, installed.stderr)
            self.assertTrue(os.access(target, os.X_OK))

            help_result = subprocess.run(
                [str(target), "--help"], cwd="/", text=True, capture_output=True,
            )
            self.assertEqual(help_result.returncode, 0, help_result.stderr)
            self.assertIn("Folderless drop-in archiver", help_result.stdout)

            with zipfile.ZipFile(target) as archive:
                names = set(archive.namelist())
            self.assertIn("dropin/store/schema/0001_initial.sql", names)
            self.assertIn("dropin/store/schema/0002_attribute_dict.sql", names)
            self.assertIn("dropin/restore_state/schema/0001_initial.sql", names)

            schema = subprocess.run(
                [sys.executable, "-c",
                 "import sys; sys.path.insert(0, sys.argv[1]); "
                 "from dropin.store.db import SCHEMA_VERSION; "
                 "from dropin.restore_state import SCHEMA_VERSION as R; "
                 "print(SCHEMA_VERSION, R)",
                 str(target)],
                cwd="/", text=True, capture_output=True,
            )
            self.assertEqual(schema.returncode, 0, schema.stderr)
            self.assertEqual(schema.stdout.strip(), "2 1")


if __name__ == "__main__":
    unittest.main()
