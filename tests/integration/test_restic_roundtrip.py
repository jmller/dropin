"""End to end against the pinned restic over an rclone local remote.

Skipped, never passed vacuously, when the binaries are absent. Linux uses real
`/proc` ownership; other platforms use fake ownership and skip the real-writer
scenario. These are NOT macOS ownership/release validation. The Spotlight
seam is the fixture-backed fake. Every scenario asserts on the store and the
filesystem; `find`/`get` assertions belong to the query and retrieval suites.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

from dropin.config import load
from dropin.engine.interface import EngineError
from dropin.engine.restic import ResticEngine
from dropin.macos.fake import FakeMacOS
from dropin.ownership.linux import LinuxOwnership
from dropin.pipeline.drain import DrainOptions, drain
from dropin.pipeline.faults import FaultInjected
from dropin.recover import recover
from dropin.report import Outcome, Report
from dropin.store import records
from dropin.store.db import connect
from tests.support import FaultHook, tools_available
from tests.unit.test_evict import FakeOwnership

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "synthetic"

CONFIG = """
[paths]
drop_dir  = "{drop}"
state_dir = "{state}"
[repository]
repo          = "rclone:local:{repo}"
password_file = "{password}"
[tools]
restic = "{restic}"
rclone = "{rclone}"
restic_min = "0.19.1"
rclone_min = "1.75.1"
rclone_connections = 2
timeout_seconds = 600
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
"""


class Context:
    def __init__(self, config, db, engine, macos, ownership):
        self.config = config
        self.db = db
        self.engine = engine
        self.macos = macos
        self.ownership = ownership


@unittest.skipIf(tools_available() is None,
                 "DROPIN_RESTIC_BIN/DROPIN_RCLONE_BIN not set or binaries unusable")
class ResticTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.restic, cls.rclone = tools_available()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dropin-it-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.drop = self.root / "drop"
        self.state = self.root / "state"
        self.repo = self.root / "repo"
        for path in (self.drop, self.state / "export", self.state / "cache",
                     self.state / "tmp", self.root / "scratch"):
            path.mkdir(parents=True)
        password = self.root / "pw"
        password.write_bytes(os.urandom(16).hex().encode())
        password.chmod(0o600)
        rclone_conf = self.root / "rclone.conf"
        rclone_conf.write_text("[local]\ntype = local\n")
        self._environ = dict(os.environ)
        os.environ["RCLONE_CONFIG"] = str(rclone_conf)
        os.environ["GOMAXPROCS"] = "2"
        self.addCleanup(self._restore_environ)

        self.config_path = self.root / "config.toml"
        self.config_path.write_text(CONFIG.format(
            drop=self.drop, state=self.state, repo=self.repo, password=password,
            restic=self.restic, rclone=self.rclone))
        self.config = load(self.config_path)
        self.engine = ResticEngine(self.config)
        self.engine.init()
        self.macos = FakeMacOS()
        self.ownership = LinuxOwnership() if sys.platform == "linux" else FakeOwnership()
        self.assertTrue(self.ownership.capabilities().ownership_check,
                        self.ownership.capabilities().ownership_reason)
        self.open_store()

    def _restore_environ(self):
        os.environ.clear()
        os.environ.update(self._environ)

    def open_store(self):
        self.db = connect(self.config.store_path)
        self.addCleanup(self._close_db)
        if records.store_meta_or_none(self.db) is None:
            self.store_id = records.initialise_store(self.db)
        else:
            self.store_id = records.store_meta(self.db)["store_id"]
        self.context = Context(self.config, self.db, self.engine, self.macos,
                               self.ownership)

    def _close_db(self):
        try:
            self.db.close()
        except Exception:
            pass

    # ---- fixtures ----------------------------------------------------------

    def describe(self, path: Path) -> None:
        children = [path, *(path.rglob("*") if path.is_dir() else [])]
        for child in children:
            self.macos.set_mdls(str(child),
                                (FIXTURES / "mdls" / "text_plain.txt").read_text())
            self.macos.set_importer(
                str(child), (FIXTURES / "mdimport" / "no_text.txt").read_text())

    def drop_file(self, name="report.txt", content=b"payload bytes\n") -> Path:
        path = self.drop / name
        path.write_bytes(content)
        self.describe(path)
        return path

    def drop_tree(self, name="tree") -> Path:
        tree = self.drop / name
        (tree / "sub").mkdir(parents=True)
        (tree / "a.txt").write_bytes(b"alpha\n" * 100)
        (tree / "sub" / "b.txt").write_bytes(b"beta\n" * 100)
        (tree / "link").symlink_to("a.txt")
        (tree / "empty").mkdir()
        self.describe(tree)
        return tree

    def run_drain(self, **options) -> Report:
        report = Report(verb="drain", run_id=records.new_run_id())
        drain(self.context, report, DrainOptions(**options))
        return report

    def outcomes(self, report) -> dict[str, Outcome]:
        return {record.name: record.outcome for record in report.records}

    def occurrence(self, name: str, db=None):
        db = db or self.db
        row = db.execute("SELECT * FROM occurrence WHERE item_name = ?"
                         " ORDER BY recorded_at DESC LIMIT 1", (name,)).fetchone()
        self.assertIsNotNone(row, name)
        return row

    def raw_restic(self, *args) -> bytes:
        argv = self.engine._base() + list(args)
        result = subprocess.run(argv, capture_output=True,
                                env=self.config.restic_env(), timeout=300)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        return result.stdout


records.store_meta_or_none = lambda db: db.execute(  # noqa: E731
    "SELECT * FROM store_meta").fetchone()


class ScenarioATest(ResticTestCase):
    def test_a_file_reaches_evicted_with_one_confirmed_snapshot(self):
        content = b"scenario A payload\n" * 64
        path = self.drop_file("report.txt", content)
        report = self.run_drain()
        self.assertEqual(self.outcomes(report), {"report.txt": Outcome.ARCHIVED},
                         report.render_human())
        self.assertFalse(path.exists())

        row = self.occurrence("report.txt")
        self.assertEqual(row["state"], "evicted")
        attempt = records.get_attempt(self.db, row["confirmed_attempt_id"])
        self.assertEqual(attempt["outcome"], "confirmed")
        snapshot = records.get_snapshot(self.db, attempt["snapshot_id"])
        self.assertEqual(snapshot["status"], "confirmed")
        self.assertEqual(len(self.engine.snapshots(tag="dropin:v=1")), 1)
        self.assertEqual(records.store_meta(self.db)["published_frontier"], 1)

        # The bytes are really there, and the export travelled with them.
        with self.engine.dump(attempt["snapshot_id"], str(path)) as stream:
            self.assertEqual(hashlib.sha256(stream.read()).hexdigest(),
                             hashlib.sha256(content).hexdigest())
        run = self.db.execute("SELECT * FROM run").fetchone()
        self.assertEqual(run["exit_code"], 0)
        self.assertGreater(run["restic_peak_rss_kb"], 0)

    def test_local_repository_is_independent_of_invocation_directory(self):
        path = self.drop_file("portable-repository.txt", b"portable bytes")
        self.assertEqual(self.outcomes(self.run_drain()),
                         {path.name: Outcome.ARCHIVED})
        attempt = records.get_attempt(
            self.db, self.occurrence(path.name)["confirmed_attempt_id"])
        original_cwd = Path.cwd()
        try:
            for cwd in (self.root / "scratch", self.drop, self.state):
                with self.subTest(cwd=cwd):
                    os.chdir(cwd)
                    self.assertEqual(len(self.engine.snapshots(tag="dropin:v=1")), 1)
                    with self.engine.dump(attempt["snapshot_id"], str(path)) as stream:
                        self.assertEqual(stream.read(), b"portable bytes")
        finally:
            os.chdir(original_cwd)

    def test_sc006_the_backend_holds_neither_plaintext_nor_names(self):
        needle = b"NEEDLE-" + os.urandom(8).hex().encode()
        name = "unmistakable-name-" + os.urandom(4).hex() + ".txt"
        self.drop_file(name, needle * 32)
        self.assertEqual(self.outcomes(self.run_drain()), {name: Outcome.ARCHIVED})
        for dirpath, _dirs, files in os.walk(self.repo):
            for filename in files:
                self.assertNotIn(name, filename)
                self.assertNotIn(needle, (Path(dirpath) / filename).read_bytes())


class ScenarioBTest(ResticTestCase):
    def test_identical_content_shares_blobs_across_occurrences(self):
        content = b"dedup me\n" * 512
        first = self.drop_file("first.txt", content)
        self.run_drain()
        second = self.drop_file("second.txt", content)
        self.run_drain()
        rows = [self.occurrence("first.txt"), self.occurrence("second.txt")]
        self.assertTrue(rows[1]["dedup_of"] == rows[0]["occ_id"])
        ids = set()
        for row, path in zip(rows, (first, second)):
            attempt = records.get_attempt(self.db, row["confirmed_attempt_id"])
            ids.add(tuple(self.engine.node_content_ids(attempt["snapshot_id"],
                                                       str(path))))
        self.assertEqual(len(ids), 1, "one blob set for identical bytes")
        self.assertEqual(self.db.execute("SELECT count(*) FROM occurrence")
                         .fetchone()[0], 2)


class ScenarioCTest(ResticTestCase):
    def test_a_tree_with_a_symlink_archives_and_a_fifo_refuses_its_tree(self):
        tree = self.drop_tree("tree")
        bad = self.drop / "bad"
        (bad / "inner").mkdir(parents=True)
        (bad / "inner" / "ok.txt").write_bytes(b"fine")
        os.mkfifo(bad / "pipe")
        self.describe(bad)
        report = self.run_drain()
        outcomes = self.outcomes(report)
        self.assertEqual(outcomes["tree"], Outcome.ARCHIVED, report.render_human())
        self.assertEqual(outcomes["bad"], Outcome.REFUSED)
        self.assertFalse(tree.exists())
        self.assertTrue((bad / "pipe").exists())
        self.assertTrue((bad / "inner" / "ok.txt").exists())
        row = self.occurrence("tree")
        entries = {r["rel_path"]: r for r in records.iter_entries(self.db,
                                                                 row["occ_id"])}
        self.assertEqual(entries["link"]["entry_type"], "symlink")
        self.assertEqual(entries["link"]["link_target"], "a.txt")
        self.assertIn("empty", entries)


class ScenarioDTest(ResticTestCase):
    def test_an_unreadable_file_is_isolated(self):
        if os.geteuid() == 0:
            self.skipTest("root can read anything")
        blocked = self.drop_file("blocked.bin", b"secret")
        blocked.chmod(0)
        self.addCleanup(blocked.chmod, 0o600)
        fine = self.drop_file("fine.txt", b"fine")
        report = self.run_drain()
        outcomes = self.outcomes(report)
        self.assertEqual(outcomes["blocked.bin"], Outcome.REFUSED)
        self.assertEqual(outcomes["fine.txt"], Outcome.ARCHIVED)
        self.assertTrue(blocked.exists())
        self.assertFalse(fine.exists())
        self.assertEqual(len(self.engine.snapshots(tag="dropin:v=1")), 1)


class ScenarioFTest(ResticTestCase):
    def test_recovery_from_the_repository_and_password_alone(self):
        self.drop_file("a.txt", b"alpha\n" * 10)
        self.drop_tree("tree")
        self.run_drain()
        originals = {row["item_name"]: dict(row) for row in
                     self.db.execute("SELECT * FROM occurrence")}
        original_meta = dict(records.store_meta(self.db))
        self.db.close()
        shutil.rmtree(self.state)

        fresh = self.root / "fresh"
        for name in ("cache", "tmp"):
            (fresh / name).mkdir(parents=True)
        report = Report(verb="recover", run_id="rec")
        engine = ResticEngine(load(self._config_for(fresh)))
        result = recover(engine, fresh, report)
        self.assertEqual(report.exit_code(), 0, report.render_human())

        db = connect(fresh / "store.sqlite")
        self.addCleanup(db.close)
        for name in ("a.txt", "tree"):
            row = self.occurrence(name, db)
            self.assertEqual(row["state"], "recoverable")
            self.assertEqual(row["occ_id"], originals[name]["occ_id"])
            self.assertEqual(row["confirmed_attempt_id"],
                             originals[name]["confirmed_attempt_id"])
            self.assertEqual(
                sorted(r["rel_path"] for r in records.iter_entries(db, row["occ_id"])),
                sorted(r["rel_path"] for r in db.execute(
                    "SELECT rel_path FROM entry WHERE occ_id = ?",
                    (row["occ_id"],))))
        meta = records.store_meta(db)
        self.assertEqual(meta["store_id"], original_meta["store_id"])
        self.assertEqual(meta["export_seq"], original_meta["export_seq"])
        self.assertEqual(meta["published_frontier"],
                         original_meta["published_frontier"])
        self.assertEqual(result.counts["recoverable"], 2)

    def test_recovery_of_the_newest_item_whose_only_catalog_is_its_own(self):
        """Discard the store before drain completion."""
        self.drop_file("a.txt", b"alpha\n")
        self.run_drain()
        self.drop_file("zzz-newest.txt", b"newest\n")
        with FaultHook("verified"), self.assertRaises(FaultInjected):
            self.run_drain()
        self.db.close()
        shutil.rmtree(self.state)

        fresh = self.root / "fresh"
        for name in ("cache", "tmp"):
            (fresh / name).mkdir(parents=True)
        report = Report(verb="recover", run_id="rec")
        recover(ResticEngine(load(self._config_for(fresh))), fresh, report)
        db = connect(fresh / "store.sqlite")
        self.addCleanup(db.close)
        self.assertEqual(self.occurrence("a.txt", db)["state"], "recoverable")
        newest = self.occurrence("zzz-newest.txt", db)
        self.assertEqual(newest["state"], "recoverable",
                         "confirmed from its own snapshot's catalog and payload")
        self.assertEqual(records.get_attempt(db, newest["confirmed_attempt_id"])
                         ["outcome"], "confirmed")
        self.assertEqual(records.store_meta(db)["published_frontier"], 2)

    def _config_for(self, state_dir: Path) -> Path:
        path = self.root / f"config-{state_dir.name}.toml"
        path.write_text(self.config_path.read_text().replace(
            f'state_dir = "{self.state}"', f'state_dir = "{state_dir}"'))
        state_dir.mkdir(exist_ok=True)
        return path


class ScenarioGTest(ResticTestCase):
    def test_crash_after_backup_returned_converges_on_one_snapshot(self):
        path = self.drop_file("g.txt", b"crash me\n")
        with FaultHook("backup-returned"), self.assertRaises(FaultInjected):
            self.run_drain()
        self.assertTrue(path.exists())
        [attempt] = records.attempts_for(self.db, self.occurrence("g.txt")["occ_id"])
        self.assertIsNone(attempt["snapshot_id"])
        self.assertEqual(len(self.engine.snapshots(tag="dropin:v=1")), 1)

        report = self.run_drain(now_offset=1000)
        self.assertEqual(self.outcomes(report), {"g.txt": Outcome.ARCHIVED},
                         report.render_human())
        self.assertFalse(path.exists())
        self.assertEqual(len(self.engine.snapshots(tag="dropin:v=1")), 1)
        history = records.attempts_for(self.db, self.occurrence("g.txt")["occ_id"])
        self.assertEqual([a["outcome"] for a in history], ["confirmed"])

    def test_crash_after_intent_written_resumes_the_deletion(self):
        tree = self.drop_tree("tree")
        with FaultHook("intent-written"), self.assertRaises(FaultInjected):
            self.run_drain()
        self.assertTrue(tree.exists())
        self.assertEqual(self.occurrence("tree")["state"], "evicting")
        report = self.run_drain()
        self.assertEqual(self.outcomes(report), {"tree": Outcome.ARCHIVED})
        self.assertFalse(tree.exists())
        self.assertEqual(self.occurrence("tree")["state"], "evicted")


@unittest.skipUnless(sys.platform == "linux", "requires real Linux open-writer detection")
class ScenarioITest(ResticTestCase):
    def test_an_open_writer_is_retained_and_evicts_after_it_closes(self):
        path = self.drop_file("held.txt", b"held\n")
        with path.open("ab") as handle:
            writer = subprocess.Popen(["sleep", "60"], stdout=handle)
        self.addCleanup(writer.wait)
        self.addCleanup(writer.kill)
        try:
            report = self.run_drain()
            record = report.records[0]
            self.assertEqual(record.outcome, Outcome.RETAINED, report.render_human())
            self.assertIn(str(writer.pid), record.reason)
            self.assertEqual(record.state, "recoverable")
            self.assertTrue(path.exists())
            self.assertEqual(report.exit_code(), 1)
        finally:
            writer.kill()
            writer.wait()
        report = self.run_drain()
        self.assertEqual(self.outcomes(report), {"held.txt": Outcome.ARCHIVED})
        self.assertFalse(path.exists())


class CatalogCorruptionTest(ResticTestCase):
    def test_a_tampered_catalog_fails_the_attempt_and_retains_the_original(self):
        path = self.drop_file("tampered.txt", b"catalog corruption scenario\n")
        with FaultHook("verified"), self.assertRaises(FaultInjected):
            self.run_drain()
        occ = self.occurrence("tampered.txt")
        [attempt] = records.attempts_for(self.db, occ["occ_id"])
        export_path = attempt["export_path"]
        blob_ids = self.engine.node_content_ids(attempt["snapshot_id"], export_path)
        self.assertTrue(blob_ids)
        self._flip_byte_in_blob(blob_ids[0])

        clean = self.drop_file("clean.txt", b"clean\n")
        report = self.run_drain(now_offset=1000)
        outcomes = self.outcomes(report)
        self.assertEqual(outcomes["clean.txt"], Outcome.ARCHIVED, report.render_human())
        self.assertEqual(outcomes["tampered.txt"], Outcome.REFUSED)
        tampered = next(r for r in report.records if r.name == "tampered.txt")
        self.assertIn("catalog", tampered.reason)
        self.assertTrue(path.exists())
        self.assertFalse(clean.exists())
        occ = self.occurrence("tampered.txt")
        self.assertEqual(occ["state"], "recorded")
        failed = records.get_attempt(self.db, attempt["attempt_id"])
        self.assertEqual(failed["outcome"], "failed")
        self.assertTrue(failed["reason"].startswith("catalog"))
        self.assertEqual(records.get_snapshot(self.db, attempt["snapshot_id"])
                         ["status"], "orphaned")

    def _flip_byte_in_blob(self, blob_id: str) -> None:
        located = None
        for index in self.raw_restic("list", "index").decode().split():
            payload = json.loads(self.raw_restic("cat", "index", index))
            for pack in payload["packs"]:
                for blob in pack["blobs"]:
                    if blob["id"] == blob_id and blob["type"] == "data":
                        located = (pack["id"], blob["offset"])
        self.assertIsNotNone(located, "export data blob must be locatable")
        pack_id, offset = located
        pack_path = self.repo / "data" / pack_id[:2] / pack_id
        with pack_path.open("r+b") as pack:
            pack.seek(offset + 5)
            original = pack.read(1)
            pack.seek(offset + 5)
            pack.write(bytes([original[0] ^ 0xFF]))


if __name__ == "__main__":
    unittest.main()
