"""Production restore-state foundation: enrollment and existing-only history."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from dropin.config import load


CONFIG = """[paths]
drop_dir = "{drop}"
state_dir = "{state}"
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
destinations = ["{destination}"]
"""


class RestoreStateTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dropin-restore-state-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.drop = self.root / "drop"
        self.state = self.root / "state"
        self.destination = self.root / "output"
        for path in (self.drop, self.state, self.destination):
            path.mkdir(mode=0o700)
        self.password = self.root / "password"
        self.password.write_text("secret")
        self.password.chmod(0o600)
        self.config_path = self.root / "config.toml"
        self.config_path.write_text(CONFIG.format(
            drop=self.drop, state=self.state, password=self.password,
            destination=self.destination))
        self.config = load(self.config_path)

    def test_initialization_rejects_invalid_live_enrollment_before_state_write(self):
        from dropin.restore_state import RestoreStateError, initialize

        invalid = self.root / "invalid-output"
        invalid.mkdir(mode=0o755)
        alias = self.root / "alias-output"
        alias.symlink_to(self.destination)
        missing = self.root / "missing-output"
        base = self.config_path.read_text()
        for replacement in (str(invalid), str(alias), str(missing)):
            with self.subTest(path=replacement):
                self.config_path.write_text(base.replace(str(self.destination), replacement))
                with self.assertRaises(RestoreStateError):
                    initialize(load(self.config_path))
                self.assertFalse(self.config.restore_dir.exists())
        self.config_path.write_text(base)

    def test_live_reserved_validation_refuses_filesystem_alias_overlap(self):
        from dropin import restore_state as module

        binding = json.dumps({"components": [
            {"device": 1, "inode": 1},
            {"device": 1, "inode": 20},
            {"device": 1, "inode": 30},
        ]})
        # Simulate a case/normalization alias whose spelling passed the lexical
        # config check but whose destination is actually inside the drop tree.
        identities = {
            self.drop: ((1, 1), (1, 20)),
            self.state: ((1, 1), (1, 40)),
        }
        with patch.object(module, '_binding', return_value=binding), \
             patch.object(module, '_path_identities',
                          side_effect=lambda path: identities[path]), \
             self.assertRaisesRegex(module.RestoreStateError,
                                    'overlaps paths.drop_dir'):
            module.validate_reserved_destination(self.config, self.destination)

    def test_initialize_creates_separate_versioned_blocked_profile(self):
        from dropin.restore_state import BLOCKED_GATES, SCHEMA_VERSION, initialize

        profile = initialize(self.config)
        self.assertRegex(profile.generation, r"^[0-9a-f]{32}$")
        self.assertEqual(profile.schema_version, SCHEMA_VERSION)
        self.assertEqual(profile.activation, "blocked")
        self.assertEqual(profile.blocked_gates, BLOCKED_GATES)
        self.assertEqual([item.path for item in profile.destinations],
                         [str(self.destination)])
        self.assertEqual(list(self.destination.iterdir()), [])
        self.assertEqual(self.config.restore_state_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.config.restore_dir.stat().st_mode & 0o777, 0o700)

        db = sqlite3.connect(self.config.restore_state_path)
        try:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0], "delete")
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertIn(
                ("active_item_id", "restore_item", "item_id"),
                {(row[3], row[2], row[4]) for row in db.execute(
                    "PRAGMA foreign_key_list(restore_destination)")})
            tables = {row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertEqual(tables, {
                "restore_meta", "restore_destination", "restore_request",
                "restore_item", "restore_alias", "restore_attempt",
                "restore_transition",
            })
            gates = json.loads(db.execute(
                "SELECT blocked_gates_json FROM restore_meta WHERE id=1").fetchone()[0])
            self.assertEqual(tuple(gates), BLOCKED_GATES)
        finally:
            db.close()

    def test_inspect_is_existing_only_and_validates_enrollment_binding(self):
        from dropin.restore_state import RestoreStateError, initialize, inspect

        with self.assertRaises(RestoreStateError) as caught:
            inspect(self.config)
        self.assertIn("not initialized", str(caught.exception))
        initialized = initialize(self.config)
        reopened = inspect(self.config)
        self.assertEqual(reopened, initialized)

        moved = self.root / "old-output"
        self.destination.rename(moved)
        self.destination.mkdir(mode=0o700)
        with self.assertRaises(RestoreStateError) as caught:
            inspect(self.config)
        self.assertIn("binding differs", str(caught.exception))

    def test_existing_history_is_never_overwritten_or_reinitialized(self):
        from dropin.restore_state import RestoreStateError, initialize

        first = initialize(self.config)
        before = self.config.restore_state_path.read_bytes()
        with self.assertRaises(RestoreStateError) as caught:
            initialize(self.config)
        self.assertIn("already exists", str(caught.exception))
        self.assertEqual(self.config.restore_state_path.read_bytes(), before)
        db = sqlite3.connect(self.config.restore_state_path)
        try:
            self.assertEqual(first.generation, db.execute(
                "SELECT generation FROM restore_meta").fetchone()[0])
        finally:
            db.close()

    def test_interrupted_or_corrupt_history_refuses_without_destination_mutation(self):
        from dropin.restore_state import RestoreStateError, inspect

        self.config.restore_dir.mkdir(mode=0o700)
        interrupted = self.config.restore_dir / ".requests.sqlite.init-deadbeef"
        interrupted.write_bytes(b"partial")
        with self.assertRaises(RestoreStateError) as caught:
            inspect(self.config)
        self.assertIn("interrupted initialization", str(caught.exception))
        interrupted.unlink()
        self.config.restore_state_path.write_bytes(b"not sqlite")
        with self.assertRaises(RestoreStateError) as caught:
            inspect(self.config)
        self.assertIn("history", str(caught.exception))
        self.assertEqual(list(self.destination.iterdir()), [])

    def test_inspect_rejects_symlinked_or_nonprivate_history_file(self):
        from dropin.restore_state import RestoreStateError, initialize, inspect

        initialize(self.config)
        original = self.config.restore_dir / "saved.sqlite"
        self.config.restore_state_path.rename(original)
        self.config.restore_state_path.symlink_to(original)
        with self.assertRaises(RestoreStateError) as caught:
            inspect(self.config)
        self.assertIn("real owned mode-0600", str(caught.exception))
        self.config.restore_state_path.unlink()
        original.rename(self.config.restore_state_path)
        self.config.restore_state_path.chmod(0o644)
        with self.assertRaises(RestoreStateError) as caught:
            inspect(self.config)
        self.assertIn("real owned mode-0600", str(caught.exception))

    def test_profile_rejects_changed_config_or_schema_without_migration(self):
        from dropin.restore_state import RestoreStateError, initialize, inspect

        initialize(self.config)
        other = self.root / "other-output"
        other.mkdir(mode=0o700)
        text = self.config_path.read_text().replace(
            f'destinations = ["{self.destination}"]',
            f'destinations = ["{other}"]')
        self.config_path.write_text(text)
        with self.assertRaises(RestoreStateError) as caught:
            inspect(load(self.config_path))
        self.assertIn("enrollment differs", str(caught.exception))

        db = sqlite3.connect(self.config.restore_state_path)
        db.execute("PRAGMA user_version=2")
        db.commit()
        db.close()
        with self.assertRaises(RestoreStateError) as caught:
            inspect(self.config)
        self.assertIn("schema version", str(caught.exception))

    def test_request_id_parser_binds_profile_generation(self):
        from dropin.restore_state import RestoreStateError, initialize, parse_request_id

        profile = initialize(self.config)
        token = "1" * 32
        self.assertEqual(parse_request_id(profile, profile.generation + ":" + token), token)
        for value in (token, profile.generation + ":ABC", "0" * 32 + ":" + token):
            with self.subTest(value=value), self.assertRaises(RestoreStateError):
                parse_request_id(profile, value)


if __name__ == "__main__":
    unittest.main()
