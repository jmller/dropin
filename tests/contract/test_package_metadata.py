"""Public package metadata and license inclusion contract."""

from pathlib import Path
import tomllib
import unittest

from dropin import __version__


ROOT = Path(__file__).resolve().parents[2]


class PackageMetadataTest(unittest.TestCase):
    def setUp(self):
        document = tomllib.loads((ROOT / "pyproject.toml").read_text())
        self.build_system = document["build-system"]
        self.metadata = document["project"]

    def test_identity_python_and_author_are_public(self):
        self.assertEqual(self.metadata["name"], "dropin-archiver")
        self.assertEqual(__version__, "0.1.0")
        self.assertEqual(self.metadata["requires-python"], ">=3.11")
        self.assertEqual(self.metadata["authors"], [
            {"name": "johannes", "email": "mail@johann3s.de"}
        ])

    def test_source_available_license_uses_custom_expression_and_files(self):
        self.assertEqual(
            self.metadata["license"],
            "LicenseRef-Apache-2.0-With-Commons-Clause-1.0",
        )
        self.assertEqual(self.metadata["license-files"], ["LICENSE", "NOTICE"])
        self.assertIn("setuptools>=77.0.3", self.build_system["requires"])
        classifiers = self.metadata.get("classifiers", [])
        self.assertFalse(any("OSI Approved" in value for value in classifiers))
        self.assertIn("Operating System :: MacOS", classifiers)
        self.assertIn("Programming Language :: Python :: 3 :: Only", classifiers)

    def test_public_install_examples_use_the_installed_command(self):
        readme = (ROOT / "README.md").read_text()
        self.assertNotIn("python3 -m dropin", readme)
        self.assertIn("dropin --config", readme)

    def test_public_urls_are_declared(self):
        self.assertEqual(self.metadata["urls"]["Homepage"],
                         "https://github.com/jmller/dropin")
        self.assertEqual(self.metadata["urls"]["Issues"],
                         "https://github.com/jmller/dropin/issues")

    def test_source_distribution_manifest_includes_legal_and_user_files(self):
        lines = {
            line.strip() for line in (ROOT / "MANIFEST.in").read_text().splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
        self.assertIn("include LICENSE", lines)
        self.assertIn("include NOTICE", lines)
        self.assertIn("include README.md", lines)
        self.assertIn("recursive-include dropin *.sql", lines)


if __name__ == "__main__":
    unittest.main()
