"""Release identity contract for package, tag, and CLI representations."""

import unittest

from dropin import __version__
from tests.support import run_cli



class ReleaseVersionTest(unittest.TestCase):
    def test_package_version_is_normalized_without_tag_prefix(self):
        self.assertEqual(__version__, "0.1.0")

    def test_global_version_runs_before_configuration_loading(self):
        result = run_cli([
            "--config", "/definitely/absent/dropin-config.toml", "--version"
        ])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, b"dropin 0.1.0\n")
        self.assertEqual(result.stderr, b"")


if __name__ == "__main__":
    unittest.main()
