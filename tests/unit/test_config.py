"""Config loading and validation."""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest

from dropin.config import ConfigError, load


EXAMPLE = """
[paths]
drop_dir     = "{drop}"
state_dir    = "{state}"
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
settle_seconds        = 5
sample_gap_seconds    = 2
max_attempts          = 3
retry_backoff_seconds = 300
[ownership]
lsof = "lsof"
[launchd]
label    = "dev.dropin.drain"
interval = 900
"""


class ConfigTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dropin-config-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.drop = self.root / "drop"
        self.state = self.root / "state"
        self.drop.mkdir()
        self.state.mkdir()
        self.password = self.root / "repo.password"
        self.password.write_text("hunter2")
        self.password.chmod(0o600)

    def write_config(self, body: str | None = None, **overrides: str) -> Path:
        text = body if body is not None else EXAMPLE.format(
            drop=overrides.get("drop", self.drop),
            state=overrides.get("state", self.state),
            password=overrides.get("password", self.password),
        )
        path = self.root / "config.toml"
        path.write_text(text)
        return path

    def load(self, body: str | None = None, **overrides: str):
        return load(self.write_config(body, **overrides))

    # ---- the documented example -------------------------------------------

    def test_contract_example_loads(self):
        config = self.load()
        self.assertEqual(config.drop_dir, self.drop.resolve())
        self.assertEqual(config.state_dir, self.state.resolve())
        self.assertEqual(config.repo, "rclone:archive:/dropin")
        self.assertEqual(config.password_file, self.password.resolve())
        self.assertEqual(config.restic, "restic")
        self.assertEqual(config.restic_min, "0.19.1")
        self.assertEqual(config.rclone_min, "1.75.1")
        self.assertEqual(config.rclone_connections, 2)
        self.assertEqual(config.timeout_seconds, 3600)
        self.assertEqual(config.pack_size_mb, 16)
        self.assertEqual(config.cache_max_mb, 2048)
        self.assertEqual(config.settle_seconds, 5)
        self.assertEqual(config.sample_gap_seconds, 2)
        self.assertEqual(config.max_attempts, 3)
        self.assertEqual(config.retry_backoff_seconds, 300)
        self.assertEqual(config.lsof, "lsof")
        self.assertEqual(config.launchd_label, "dev.dropin.drain")
        self.assertEqual(config.launchd_interval, 900)
        self.assertEqual(config.restore_destinations, ())

    def test_derived_state_paths(self):
        config = self.load()
        self.assertEqual(config.store_path, self.state.resolve() / "store.sqlite")
        self.assertEqual(config.export_dir, self.state.resolve() / "export")
        self.assertEqual(config.cache_dir, self.state.resolve() / "cache")
        self.assertEqual(config.tmp_dir, self.state.resolve() / "tmp")
        self.assertEqual(config.writer_lock_path,
                         self.state.resolve() / "writer.lock")
        self.assertEqual(config.restore_dir,
                         self.state.resolve() / "restore")
        self.assertEqual(config.restore_state_path,
                         self.state.resolve() / "restore" / "requests.sqlite")

    # ---- restore enrollment ------------------------------------------------

    def test_restore_table_is_optional_and_exact_destination_is_enrolled(self):
        destination = self.root / "restore-output"
        destination.mkdir(mode=0o700)
        body = EXAMPLE.format(drop=self.drop, state=self.state,
                              password=self.password)
        body += f'\n[restore]\ndestinations = ["{destination}"]\n'
        config = self.load(body)
        self.assertEqual(config.restore_destinations, (destination,))

    def test_restore_destination_syntax_is_strict_but_liveness_is_not_global(self):
        base = EXAMPLE.format(drop=self.drop, state=self.state,
                              password=self.password)
        body = base + '\n[restore]\ndestinations = ["relative"]\n'
        with self.assertRaises(ConfigError) as caught:
            self.load(body)
        self.assertIn("absolute", str(caught.exception))

        # Global config loading must not touch enrolled output paths: a terminal
        # historical replay remains possible after an output is relocated.
        missing = self.root / "missing"
        body = base + f'\n[restore]\ndestinations = ["{missing}"]\n'
        self.assertEqual(self.load(body).restore_destinations, (missing,))

    def test_restore_destination_rejects_spelling_overlap_without_filesystem_io(self):
        destination = self.root / "restore-output"
        child = destination / "child"
        base = EXAMPLE.format(drop=self.drop, state=self.state,
                              password=self.password)
        cases = ([str(destination), str(destination)],
                 [str(destination), str(child)], [str(self.drop)], [str(self.state)])
        for values in cases:
            with self.subTest(values=values):
                rendered = ", ".join(f'"{value}"' for value in values)
                body = base + f'\n[restore]\ndestinations = [{rendered}]\n'
                with self.assertRaises(ConfigError):
                    self.load(body)

    def test_restore_destination_rejects_noncanonical_spelling_and_bad_element(self):
        destination = self.root / "restore-output"
        destination.mkdir(mode=0o700)
        base = EXAMPLE.format(drop=self.drop, state=self.state,
                              password=self.password)
        for declaration in (f'["{destination}/."]', '[1]'):
            with self.subTest(declaration=declaration):
                body = base + f'\n[restore]\ndestinations = {declaration}\n'
                with self.assertRaises(ConfigError):
                    self.load(body)

    # ---- unknown keys ------------------------------------------------------

    def test_unknown_key_names_itself(self):
        body = EXAMPLE.format(drop=self.drop, state=self.state,
                              password=self.password) + '\n[extra]\nnope = 1\n'
        with self.assertRaises(ConfigError) as caught:
            self.load(body)
        self.assertIn("extra", str(caught.exception))

    def test_unknown_key_inside_known_table_names_itself(self):
        body = EXAMPLE.format(drop=self.drop, state=self.state,
                              password=self.password).replace(
            "[drain]", "[drain]\nsettle_secconds = 5")
        with self.assertRaises(ConfigError) as caught:
            self.load(body)
        self.assertIn("settle_secconds", str(caught.exception))

    # ---- path overlap, both directions, after realpath ---------------

    def test_drop_dir_inside_state_dir_rejected(self):
        inner = self.state / "drop"
        inner.mkdir()
        with self.assertRaises(ConfigError) as caught:
            self.load(drop=inner)
        self.assertIn("drop_dir", str(caught.exception))

    def test_state_dir_inside_drop_dir_rejected(self):
        inner = self.drop / "state"
        inner.mkdir()
        with self.assertRaises(ConfigError):
            self.load(state=inner)

    def test_equal_paths_rejected(self):
        with self.assertRaises(ConfigError):
            self.load(drop=self.state)

    def test_symlink_alias_overlap_rejected(self):
        alias = self.root / "alias"
        alias.symlink_to(self.state)
        with self.assertRaises(ConfigError):
            self.load(drop=alias)

    def test_symlinked_child_overlap_rejected(self):
        inner = self.state / "inner"
        inner.mkdir()
        alias = self.root / "alias-inner"
        alias.symlink_to(inner)
        with self.assertRaises(ConfigError):
            self.load(drop=alias)

    def test_missing_drop_dir_rejected(self):
        with self.assertRaises(ConfigError) as caught:
            self.load(drop=self.root / "absent")
        self.assertIn("drop_dir", str(caught.exception))

    # ---- password file -----------------------------------------------------

    def test_group_or_world_readable_password_file_rejected(self):
        self.password.chmod(0o644)
        with self.assertRaises(ConfigError) as caught:
            self.load()
        self.assertIn("password_file", str(caught.exception))

    def test_missing_password_file_rejected(self):
        self.password.unlink()
        with self.assertRaises(ConfigError):
            self.load()

    # ---- repository string -------------------------------------------------

    def test_repo_must_be_rclone(self):
        body = EXAMPLE.format(drop=self.drop, state=self.state,
                              password=self.password).replace(
            'repo          = "rclone:archive:/dropin"', 'repo          = "/tmp/repo"')
        with self.assertRaises(ConfigError) as caught:
            self.load(body)
        self.assertIn("repo", str(caught.exception))

    def test_repo_remote_name_must_be_non_empty(self):
        body = EXAMPLE.format(drop=self.drop, state=self.state,
                              password=self.password).replace(
            "rclone:archive:/dropin", "rclone::/dropin")
        with self.assertRaises(ConfigError):
            self.load(body)

    def test_repo_path_must_be_absolute(self):
        body = EXAMPLE.format(drop=self.drop, state=self.state,
                              password=self.password).replace(
            "rclone:archive:/dropin", "rclone:archive:dropin")
        with self.assertRaises(ConfigError) as caught:
            self.load(body)
        self.assertIn("repository.repo path must be absolute", str(caught.exception))

    def test_cloud_repo_path_is_retained_without_normalization(self):
        repo = "rclone:onedrive:/Dropin Archive/../archive"
        body = EXAMPLE.format(drop=self.drop, state=self.state,
                              password=self.password).replace(
            "rclone:archive:/dropin", repo)
        self.assertEqual(self.load(body).repo, repo)

    # ---- numeric validation ------------------------------------------------

    def test_admission_waits_may_be_zero_but_not_negative(self):
        import re

        for key in ("settle_seconds", "sample_gap_seconds"):
            with self.subTest(key=key):
                body = EXAMPLE.format(drop=self.drop, state=self.state,
                                      password=self.password)
                zero = re.sub(rf"^{key}\s*=.*$", f"{key} = 0", body,
                              flags=re.MULTILINE)
                self.assertEqual(getattr(self.load(zero), key), 0)
                negative = re.sub(rf"^{key}\s*=.*$", f"{key} = -1", body,
                                  flags=re.MULTILINE)
                with self.assertRaises(ConfigError) as caught:
                    self.load(negative)
                self.assertIn(key, str(caught.exception))

    def test_numeric_fields_must_be_positive(self):
        for key, value in (("pack_size_mb", 0), ("cache_max_mb", -1),
                           ("rclone_connections", 0), ("max_attempts", 0),
                           ("retry_backoff_seconds", -5), ("timeout_seconds", 0)):
            with self.subTest(key=key):
                body = EXAMPLE.format(drop=self.drop, state=self.state,
                                      password=self.password)
                import re

                body = re.sub(rf"^{key}\s*=.*$", f"{key} = {value}", body,
                              flags=re.MULTILINE)
                with self.assertRaises(ConfigError) as caught:
                    self.load(body)
                self.assertIn(key, str(caught.exception))

    def test_max_attempts_must_be_integer(self):
        body = EXAMPLE.format(drop=self.drop, state=self.state,
                              password=self.password).replace(
            "max_attempts          = 3", 'max_attempts          = "three"')
        with self.assertRaises(ConfigError):
            self.load(body)

    # ---- restic environment ------------------------------------------------

    def test_restic_env_sets_only_the_documented_variables(self):
        config = self.load()
        env = config.restic_env({
            "PATH": "/usr/bin",
            "RESTIC_REPOSITORY": "somewhere-else",
            "RESTIC_PASSWORD": "leaked",
            "RCLONE_CONFIG_PASS": "leaked",
            "HOME": "/home/someone",
        })
        self.assertEqual(env["RESTIC_PASSWORD_FILE"], str(self.password.resolve()))
        self.assertEqual(env["RESTIC_CACHE_DIR"], str(config.cache_dir))
        self.assertEqual(env["TMPDIR"], str(config.tmp_dir))
        self.assertEqual(env["PATH"], "/usr/bin")
        self.assertEqual(env["HOME"], "/home/someone")
        self.assertNotIn("RESTIC_REPOSITORY", env)
        self.assertNotIn("RESTIC_PASSWORD", env)
        self.assertNotIn("RCLONE_CONFIG_PASS", env)

    def test_restic_env_passes_rclone_config_through(self):
        config = self.load()
        env = config.restic_env({"RCLONE_CONFIG": "/home/me/rclone.conf"})
        self.assertEqual(env["RCLONE_CONFIG"], "/home/me/rclone.conf")

    def test_restic_env_defaults_to_process_environment(self):
        config = self.load()
        os.environ["DROPIN_CONFIG_TEST_MARKER"] = "1"
        self.addCleanup(os.environ.pop, "DROPIN_CONFIG_TEST_MARKER", None)
        self.assertEqual(config.restic_env()["DROPIN_CONFIG_TEST_MARKER"], "1")

    # ---- config discovery --------------------------------------------------

    def test_missing_config_file_is_a_config_error(self):
        with self.assertRaises(ConfigError):
            load(self.root / "absent.toml")

    def test_malformed_toml_is_a_config_error(self):
        with self.assertRaises(ConfigError):
            self.load("this is not toml =\n")
