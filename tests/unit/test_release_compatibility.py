"""Release compatibility boundaries fail closed without modifying user state."""

from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from dropin.config import ConfigError, load
from dropin.store.db import SCHEMA_VERSION, StoreCompatibilityError, connect
from tests.unit.test_config import EXAMPLE


class ReleaseCompatibilityTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dropin-release-compat-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_unknown_future_config_is_refused_without_mutation(self):
        drop, state = self.root / "drop", self.root / "state"
        drop.mkdir()
        state.mkdir()
        password = self.root / "password"
        password.write_text("preserve-me")
        password.chmod(0o600)
        config = self.root / "config.toml"
        config.write_text(EXAMPLE.format(
            drop=drop, state=state, password=password) +
            "\n[future]\nformat = 2\n")
        before = {path: path.read_bytes() for path in (config, password)}

        with self.assertRaises(ConfigError) as caught:
            load(config)
        self.assertIn("future", str(caught.exception))
        self.assertEqual({path: path.read_bytes() for path in before}, before)
        self.assertEqual(list(drop.iterdir()), [])
        self.assertEqual(list(state.iterdir()), [])

    def future_store(self) -> Path:
        path = self.root / "future.sqlite"
        with closing(sqlite3.connect(path)) as db, db:
            db.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
            db.execute("CREATE TABLE future_data(value TEXT)")
            db.execute("INSERT INTO future_data VALUES ('preserve-me')")
        return path

    def test_newer_store_is_refused_read_write_without_modification(self):
        path = self.future_store()
        before = path.read_bytes()
        with self.assertRaises(StoreCompatibilityError) as caught:
            connect(path)
        self.assertIn(str(SCHEMA_VERSION + 1), str(caught.exception))
        self.assertEqual(path.read_bytes(), before)
        with closing(sqlite3.connect(path)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0],
                             SCHEMA_VERSION + 1)
            self.assertEqual(db.execute("SELECT value FROM future_data").fetchone()[0],
                             "preserve-me")

    def test_newer_store_is_refused_read_only(self):
        path = self.future_store()
        before = path.read_bytes()
        with self.assertRaises(StoreCompatibilityError):
            connect(path, read_only=True)
        self.assertEqual(path.read_bytes(), before)

    def test_current_store_reopens_normally(self):
        path = self.root / "current.sqlite"
        connect(path).close()
        with connect(path, read_only=True) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0],
                             SCHEMA_VERSION)


if __name__ == "__main__":
    unittest.main()
