"""Repository compatibility failures are loud and non-destructive."""

from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from dropin.config import load
from dropin.engine.interface import EngineError
from dropin.engine.restic import ResticEngine
from tests.unit.test_config import EXAMPLE


class RepositoryCompatibilityTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dropin-repo-compat-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.drop, self.state = self.root / "drop", self.root / "state"
        self.drop.mkdir()
        self.state.mkdir()
        self.source = self.drop / "queued.txt"
        self.source.write_bytes(b"preserve queued source")
        self.store = self.state / "store.sqlite"
        self.store.write_bytes(b"preserve catalog")
        self.password = self.root / "password"
        self.password.write_text("preserve password")
        self.password.chmod(0o600)
        config_path = self.root / "config.toml"
        config_path.write_text(EXAMPLE.format(
            drop=self.drop, state=self.state, password=self.password))
        self.engine = ResticEngine(load(config_path))
        self.before = {
            path: path.read_bytes()
            for path in (self.source, self.store, self.password, config_path)
        }

    def assert_preserved(self):
        self.assertEqual({path: path.read_bytes() for path in self.before}, self.before)

    def result(self, code: int, stderr: str):
        return subprocess.CompletedProcess([], code, b"", stderr.encode())

    def test_newer_repository_format_has_explicit_kind_and_preserves_state(self):
        completed = self.result(
            1,
            "Fatal: repository version 3 is too new; this restic supports version 2",
        )
        with mock.patch("subprocess.run", return_value=completed), \
             self.assertRaises(EngineError) as caught:
            self.engine.snapshots()
        self.assertEqual(caught.exception.kind, "incompatible-repository")
        self.assertIn("repository version 3", caught.exception.stderr_tail)
        self.assert_preserved()

    def test_repository_lock_remains_distinct_and_preserves_state(self):
        completed = self.result(11, "repository is already locked")
        with mock.patch("subprocess.run", return_value=completed), \
             self.assertRaises(EngineError) as caught:
            self.engine.snapshots()
        self.assertEqual(caught.exception.kind, "locked")
        self.assert_preserved()


if __name__ == "__main__":
    unittest.main()
