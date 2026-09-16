"""Every `dropin` subpackage imports cleanly.

Cheap smoke test: a package that cannot be imported fails here rather than in
the middle of an unrelated suite.
"""

import importlib
import unittest

SUBPACKAGES = [
    "dropin",
    "dropin.cli",
    "dropin.store",
    "dropin.spool",
    "dropin.capture",
    "dropin.macos",
    "dropin.ownership",
    "dropin.engine",
    "dropin.pipeline",
    "dropin.query",
    "dropin.mcp",
]


class DiscoveryTest(unittest.TestCase):
    def test_every_subpackage_imports(self):
        for name in SUBPACKAGES:
            with self.subTest(package=name):
                self.assertIsNotNone(importlib.import_module(name))

    def test_main_module_imports(self):
        module = importlib.import_module("dropin.__main__")
        self.assertTrue(hasattr(module, "main"))
