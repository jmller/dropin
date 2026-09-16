"""CLI contract: `init`, `drain`, `add`.

Every case runs `python3 -m dropin` as a subprocess so argv, exit codes, and
stream separation are exercised exactly as a user sees them. `DROPIN_ENGINE_FAKE`
stands in for restic; under it the ownership adapter is forced unsupported, so
no case here can delete anything — the eviction path is the integration
suite's job.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
from pathlib import Path
import pty
import select
import sqlite3
import subprocess
import sys
import tempfile
import time
import tomllib
import unittest

from tests.support import run_cli

FAKE = {"DROPIN_ENGINE_FAKE": "1"}


class CliTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dropin-cli-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.drop = self.root / "drop"
        self.state = self.root / "state"
        self.password = self.root / "pw"
        self.password.write_text("secret")
        self.password.chmod(0o600)
        self.config = self.root / "config.toml"

    def init(self, *extra, env=FAKE):
        return run_cli(["--config", str(self.config), "init",
                        "--repo", "rclone:archive:/dropin",
                        "--drop-dir", str(self.drop),
                        "--state-dir", str(self.state),
                        "--password-file", str(self.password), *extra], env=env)

    def init_without_password(self, *extra, env=FAKE):
        return run_cli(["--config", str(self.config), "init",
                        "--repo", "rclone:archive:/dropin",
                        "--drop-dir", str(self.drop),
                        "--state-dir", str(self.state), *extra], env=env)

    def cli(self, *args, env=FAKE, stdin=None):
        return run_cli(["--config", str(self.config), *args], env=env, stdin=stdin)

    def ndjson(self, result) -> list[dict]:
        return [json.loads(line) for line in result.stdout.decode().splitlines()
                if line.strip()]


class InitTest(CliTestCase):
    def test_usage_documents_force(self):
        result = run_cli(["init", "--help"])
        self.assertEqual(result.returncode, 0)
        self.assertIn(b"--force", result.stdout)

    def test_init_writes_config_store_and_layout(self):
        result = self.init()
        self.assertEqual(result.returncode, 0, result.stderr)
        document = tomllib.loads(self.config.read_text())
        self.assertEqual(document["paths"]["drop_dir"], str(self.drop.resolve()))
        self.assertEqual(document["drain"]["max_attempts"], 3)
        self.assertEqual(document["drain"]["retry_backoff_seconds"], 300)
        self.assertEqual(document["repository"]["repo"], "rclone:archive:/dropin")
        for name in ("export", "cache", "tmp"):
            self.assertTrue((self.state / name).is_dir())
        db = sqlite3.connect(self.state / "store.sqlite")
        self.addCleanup(db.close)
        self.assertEqual(db.execute("SELECT count(*) FROM store_meta").fetchone()[0],
                         1)

    def test_init_expands_tilde_paths_before_creating_directories(self):
        home = self.root / "home"
        working = self.root / "working"
        home.mkdir()
        working.mkdir()
        result = run_cli([
            "--config", str(self.config), "init",
            "--repo", "rclone:archive:/dropin",
            "--drop-dir", "~/Desktop/dropin",
            "--state-dir", "~/.local/state/dropin",
            "--password-file", str(self.password),
        ], env={**FAKE, "HOME": str(home),
                "PYTHONPATH": str(Path(__file__).resolve().parents[2])}, cwd=working)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((home / "Desktop/dropin").is_dir())
        self.assertTrue((home / ".local/state/dropin").is_dir())
        self.assertFalse((working / "~").exists())
        document = tomllib.loads(self.config.read_text())
        self.assertEqual(document["paths"]["drop_dir"],
                         str((home / "Desktop/dropin").resolve()))

    def test_init_prints_the_password_obligation_to_stderr(self):
        result = self.init()
        self.assertIn(b"password", result.stderr.lower())
        self.assertIn(b"back", result.stderr.lower())
        self.assertNotIn(b"rclone crypt", result.stderr.lower())
        self.assertEqual(result.stdout, b"")

    def test_init_refuses_to_overwrite_without_force(self):
        self.assertEqual(self.init().returncode, 0)
        before = self.config.read_text()
        result = self.init("--label", "dev.other.label")
        self.assertEqual(result.returncode, 2)
        self.assertIn(b"--force", result.stderr)
        self.assertEqual(self.config.read_text(), before)

    def test_init_overwrites_only_with_force_and_keeps_the_store(self):
        self.assertEqual(self.init().returncode, 0)
        store_before = (self.state / "store.sqlite").read_bytes()
        result = self.init("--label", "dev.other.label", "--force")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(tomllib.loads(self.config.read_text())["launchd"]["label"],
                         "dev.other.label")
        self.assertEqual((self.state / "store.sqlite").read_bytes(), store_before)
        self.assertIn(b"keeping the existing store", result.stderr)

    def test_init_requires_the_password_file_to_exist(self):
        self.password.unlink()
        result = self.init()
        self.assertEqual(result.returncode, 2)
        self.assertIn(b"password file", result.stderr)
        self.assertFalse(self.config.exists())

    def test_init_generates_and_reuses_a_secure_password_file_when_omitted(self):
        result = self.init_without_password()
        self.assertEqual(result.returncode, 0, result.stderr)
        generated = self.config.with_name("repo.password")
        self.assertTrue(generated.exists())
        self.assertEqual(generated.stat().st_mode & 0o777, 0o600)
        password = generated.read_bytes()
        self.assertGreater(len(password), 32)
        self.assertEqual(password[-1:], b"\n")
        before = password
        result = self.init_without_password("--force")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(generated.read_bytes(), before)
        self.assertEqual(tomllib.loads(self.config.read_text())
                         ["repository"]["password_file"], str(generated.resolve()))

    def test_init_launchd_writes_the_agent_and_prints_the_bootstrap_line(self):
        home = self.root / "home"
        result = self.init("--launchd", "--interval", "600",
                           env={**FAKE, "HOME": str(home),
                                "DROPIN_MACOS_FAKE": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(b"launchctl bootstrap gui/", result.stdout)
        self.assertIn(b"dev.dropin.drain.plist", result.stdout)


class DrainTest(CliTestCase):
    def setUp(self):
        super().setUp()
        self.assertEqual(self.init().returncode, 0)

    def test_empty_spool_is_quiet_and_exits_zero(self):
        result = self.cli("drain")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, b"")

    def test_ndjson_records_and_unsupported_ownership_retain_the_item(self):
        item = self.drop / "note.txt"
        item.write_bytes(b"hello")
        result = self.cli("--json", "drain", "--settle", "0")
        self.assertEqual(result.returncode, 1, result.stderr)
        records = self.ndjson(result)
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["verb"], "drain")
        self.assertEqual(record["outcome"], "retained")
        self.assertEqual(record["name"], "note.txt")
        self.assertEqual(record["state"], "recoverable")
        self.assertIn("ownership", record["reason"])
        self.assertRegex(record["sha256"], r"^[0-9a-f]{64}$")
        self.assertTrue(record["archive_path"].endswith("/note.txt"))
        self.assertTrue(item.exists(), "nothing is deleted without the check")
        for key in ("kind", "snapshot", "size", "dedup", "run_id"):
            self.assertIn(key, record)
        self.assertNotIn(b"\x1b", result.stdout + result.stderr)
        self.assertNotIn(b"Uploading encrypted archive", result.stderr)

    def test_human_mode_puts_failures_on_stderr(self):
        (self.drop / "note.txt").write_bytes(b"hello")
        result = self.cli("drain", "--settle", "0")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, b"")
        self.assertIn(b"retained\tnote.txt\t", result.stderr)
        self.assertNotIn(b"\x1b", result.stderr)
        self.assertNotIn(b"Uploading encrypted archive", result.stderr)

    def test_interactive_stderr_animates_and_clears_before_final_report(self):
        (self.drop / "note.txt").write_bytes(b"hello")
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        env = dict(os.environ)
        env.update(FAKE)
        env["TERM"] = "xterm"
        process = subprocess.Popen(
            [sys.executable, "-m", "dropin", "--config", str(self.config),
             "drain", "--settle", "0"],
            cwd=Path(__file__).resolve().parents[2], env=env,
            stdout=subprocess.PIPE, stderr=slave)
        os.set_blocking(master, False)
        chunks = []
        deadline = time.monotonic() + 30
        while process.poll() is None:
            if time.monotonic() >= deadline:
                process.kill()
                process.wait()
                self.fail("interactive drain did not finish")
            readable, _, _ = select.select([master], [], [], 0.1)
            if readable:
                try:
                    chunks.append(os.read(master, 65536))
                except BlockingIOError:
                    pass
        while True:
            try:
                chunk = os.read(master, 65536)
            except BlockingIOError:
                break
            except OSError as error:
                if error.errno == errno.EIO:
                    break
                raise
            if not chunk:
                break
            chunks.append(chunk)
        stdout = process.stdout.read()
        process.stdout.close()
        terminal = b"".join(chunks)
        self.assertEqual(process.returncode, 1)
        self.assertEqual(stdout, b"")
        self.assertIn(b"Uploading encrypted archive", terminal)
        self.assertIn(b"\x1b[2K", terminal)
        self.assertIn(b"retained\tnote.txt\t", terminal)
        self.assertLess(terminal.rfind(b"Uploading encrypted archive"),
                        terminal.rfind(b"retained\tnote.txt\t"))

    def test_dry_run_reports_and_touches_nothing(self):
        item = self.drop / "note.txt"
        item.write_bytes(b"hello")
        result = self.cli("--json", "drain", "--dry-run", "--settle", "0")
        self.assertEqual(result.returncode, 0, result.stderr)
        [record] = self.ndjson(result)
        self.assertEqual(record["outcome"], "info")
        self.assertIn("dry-run", record["reason"])
        self.assertEqual(record["kind"], "file")
        self.assertTrue(item.exists())
        db = sqlite3.connect(self.state / "store.sqlite")
        self.addCleanup(db.close)
        self.assertEqual(db.execute("SELECT count(*) FROM occurrence").fetchone()[0],
                         0)

    def test_retry_exhausted_is_accepted(self):
        result = self.cli("drain", "--retry-exhausted")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_a_concurrent_drain_is_refused_naming_the_holder(self):
        lock = self.state / "writer.lock"
        handle = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, handle)
        fcntl.flock(handle, fcntl.LOCK_EX)
        os.ftruncate(handle, 0)
        os.write(handle, json.dumps({"pid": 4242, "verb": "drain",
                                     "since": "2026-09-07T00:00:00Z"}).encode())
        result = self.cli("drain")
        self.assertEqual(result.returncode, 3)
        self.assertIn(b"4242", result.stderr)
        self.assertEqual(result.stdout, b"")

    def test_drain_without_a_store_is_a_usage_error(self):
        (self.state / "store.sqlite").unlink()
        result = self.cli("drain")
        self.assertEqual(result.returncode, 2)
        self.assertIn(b"init", result.stderr)

    def test_missing_tools_refuse_the_run_before_any_item(self):
        (self.drop / "note.txt").write_bytes(b"hello")
        result = self.cli("drain", env={"DROPIN_ENGINE_FAKE": "",
                                        "PATH": str(self.root / "nowhere")})
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn(b"restic", result.stderr)
        self.assertTrue((self.drop / "note.txt").exists())


class AddTest(CliTestCase):
    def setUp(self):
        super().setUp()
        self.assertEqual(self.init().returncode, 0)
        self.inbox = self.root / "inbox"
        self.inbox.mkdir()

    def test_add_queues_by_rename(self):
        source = self.inbox / "doc.txt"
        source.write_bytes(b"doc")
        result = self.cli("--json", "add", str(source))
        self.assertEqual(result.returncode, 0, result.stderr)
        [record] = self.ndjson(result)
        self.assertEqual(record["outcome"], "queued")
        self.assertFalse(source.exists())
        self.assertEqual((self.drop / "doc.txt").read_bytes(), b"doc")

    def test_add_refuses_a_name_collision_and_keeps_the_source(self):
        (self.drop / "doc.txt").write_bytes(b"already here")
        source = self.inbox / "doc.txt"
        source.write_bytes(b"new")
        result = self.cli("--json", "add", str(source))
        self.assertEqual(result.returncode, 1)
        [record] = self.ndjson(result)
        self.assertEqual(record["outcome"], "refused")
        self.assertIn("collision", record["reason"])
        self.assertTrue(source.exists())
        self.assertEqual((self.drop / "doc.txt").read_bytes(), b"already here")

    def test_add_refuses_a_missing_path_without_blocking_others(self):
        good = self.inbox / "good.txt"
        good.write_bytes(b"ok")
        result = self.cli("--json", "add", str(self.inbox / "absent.txt"),
                          str(good))
        self.assertEqual(result.returncode, 1)
        outcomes = {r["name"]: r["outcome"] for r in self.ndjson(result)}
        self.assertEqual(outcomes, {"absent.txt": "refused", "good.txt": "queued"})
        self.assertTrue((self.drop / "good.txt").exists())

    def test_add_reads_paths_from_stdin(self):
        first, second = self.inbox / "a.txt", self.inbox / "b.txt"
        first.write_bytes(b"a")
        second.write_bytes(b"b")
        payload = f"{first}\n{second}\n".encode()
        result = self.cli("add", "-", stdin=payload)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.drop / "a.txt").exists())
        self.assertTrue((self.drop / "b.txt").exists())

    def test_add_reads_nul_separated_paths(self):
        weird = self.inbox / "with\nnewline.txt"
        weird.write_bytes(b"x")
        result = self.cli("-0", "add", "-", stdin=str(weird).encode() + b"\0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.drop / "with\nnewline.txt").exists())

    def test_add_refuses_a_cross_volume_move(self):
        candidates = [Path(p) for p in ("/dev/shm", "/run", "/tmp")]
        other = next((p for p in candidates if p.is_dir() and os.access(p, os.W_OK)
                      and os.stat(p).st_dev != os.stat(self.drop).st_dev), None)
        if other is None:
            self.skipTest("no second writable filesystem available")
        with tempfile.TemporaryDirectory(dir=other) as elsewhere:
            source = Path(elsewhere) / "far.txt"
            source.write_bytes(b"far")
            result = self.cli("--json", "add", str(source))
            self.assertEqual(result.returncode, 1)
            [record] = self.ndjson(result)
            self.assertEqual(record["outcome"], "refused")
            self.assertIn("cross-volume", record["reason"])
            self.assertTrue(source.exists())

    def test_add_never_enqueues_the_state_directory(self):
        result = self.cli("--json", "add", str(self.state))
        self.assertEqual(result.returncode, 1)
        [record] = self.ndjson(result)
        self.assertEqual(record["outcome"], "refused")
        self.assertTrue((self.state / "store.sqlite").exists())


class RecoverCliTest(CliTestCase):
    def test_recover_refuses_an_existing_store(self):
        self.assertEqual(self.init().returncode, 0)
        result = self.cli("recover", "--into", str(self.state))
        self.assertEqual(result.returncode, 3)
        self.assertIn(b"already exists", result.stderr)

    def test_recover_into_a_new_directory_from_an_empty_repository(self):
        self.assertEqual(self.init().returncode, 0)
        target = self.root / "fresh"
        result = self.cli("recover", "--into", str(target))
        # The fake repository is empty in this process: a refusal, not a crash.
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn(b"no archiver snapshots", result.stderr)
        self.assertFalse((target / "store.sqlite").exists())


if __name__ == "__main__":
    unittest.main()
