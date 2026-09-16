"""Pins the restic behaviours the adapter in `dropin/engine/restic.py` relies on.

This probe settles the *adapter contract*: argv, tag round-tripping, JSON shapes, ordering,
tar member naming, stream termination, and cache-independent reads. Production
code must not rely on a [contract-probe] line until this file records it.

Run with Python 3.11+ and explicit RESTIC_BIN / RCLONE_BIN executable paths:

    make probe

Only synthetic data inside a TemporaryDirectory is written, backed up, or
destroyed. All transport uses rclone's local backend: this is NOT a cloud or
macOS test.
"""

from contextlib import closing
import errno
import hashlib
import io
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import tarfile
import tempfile
import time
import unittest

STORE_ID = "0123456789abcdef0123456789abcdef"
OCC_ID = f"{STORE_ID}.01J0000000000000000000000A"
ATTEMPT_ID = f"{STORE_ID}.01J0000000000000000000000B"


class ResticContractProbe(unittest.TestCase):
    """Each test records one contract line. Failures downgrade it to [assumption]."""

    @classmethod
    def setUpClass(cls):
        import shutil

        cls.restic = shutil.which(os.environ.get("RESTIC_BIN", "restic"))
        cls.rclone = shutil.which(os.environ.get("RCLONE_BIN", "rclone"))
        if not cls.restic or not cls.rclone:
            raise RuntimeError("Set RESTIC_BIN and RCLONE_BIN to the probe executables")
        for executable, prefix in ((cls.restic, "restic 0.19.1 "),
                                   (cls.rclone, "rclone v1.75.1")):
            version = subprocess.check_output([executable, "version"], text=True)
            if not version.startswith(prefix):
                raise RuntimeError(f"Probe version mismatch: {version.splitlines()[0]}")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dropin-contract-probe-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.drop = self.root / "drop"
        self.state = self.root / "state"
        self.export_dir = self.state / "export"
        self.cache = self.state / "cache"
        for path in (self.drop, self.state, self.export_dir, self.cache,
                     self.root / "home", self.root / "scratch"):
            path.mkdir(parents=True)
        self.repo = self.root / "repo"
        self.config = self.root / "rclone.conf"
        self.config.write_text("[probe]\ntype = local\n")
        self.password = self.root / "password"
        self.password.write_bytes(os.urandom(32).hex().encode())
        self.password.chmod(0o600)
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(("RESTIC_", "RCLONE_", "XDG_"))}
        self.env.update({
            "HOME": str(self.root / "home"),
            "TMPDIR": str(self.root / "scratch"),
            "RESTIC_PASSWORD_FILE": str(self.password),
            "RCLONE_CONFIG": str(self.config),
            "GOMAXPROCS": "2",
        })
        # The archiver's own invocation shape (see dropin/engine/restic.py).
        self.base = [self.restic, "--repo", f"rclone:probe:{self.repo}",
                     "--cache-dir", str(self.cache),
                     "--option", f"rclone.program={self.rclone}",
                     "--option", "rclone.connections=2"]
        self.run_restic("init")

    # ---- helpers -----------------------------------------------------------

    def run_restic(self, *args, expected=0, timeout=120):
        result = subprocess.run(self.base + list(args), cwd=self.root,
                                env=self.env, capture_output=True, timeout=timeout)
        if expected is not None:
            self.assertEqual(result.returncode, expected,
                             result.stderr.decode(errors="replace"))
        return result

    def write_export(self, occ_id=OCC_ID, attempt_id=ATTEMPT_ID, seq=1):
        """A stand-in catalog export at the archiver's documented path."""
        path = self.export_dir / f"{occ_id}-{attempt_id}.sqlite"
        with closing(sqlite3.connect(path)) as db:
            db.execute("CREATE TABLE export_lineage (store_id TEXT, export_seq INT,"
                       " occ_id TEXT, attempt_id TEXT, exported_at TEXT)")
            db.execute("INSERT INTO export_lineage VALUES (?,?,?,?,?)",
                       (STORE_ID, seq, occ_id, attempt_id, "2026-09-06T00:00:00Z"))
            db.commit()
            db.execute("PRAGMA journal_mode=DELETE")
        return path

    def canonical_tags(self, *, occ_id=OCC_ID, attempt_id=ATTEMPT_ID, seq=1,
                       kind="dir", digest=None):
        digest = digest or hashlib.sha256(b"probe").hexdigest()
        return ["--tag", "dropin:v=1",
                "--tag", f"dropin:store={STORE_ID}",
                "--tag", f"dropin:occ={occ_id}",
                "--tag", f"dropin:attempt={attempt_id}",
                "--tag", f"dropin:seq={seq}",
                "--tag", f"dropin:kind={kind}",
                "--tag", f"dropin:catalog-sha256={digest}"]

    def backup(self, *paths, tags=None, expected=0):
        result = self.run_restic("--json", "backup", "--no-scan",
                                 "--read-concurrency", "1",
                                 *(tags if tags is not None else self.canonical_tags()),
                                 *[str(p) for p in paths], expected=expected)
        events = [json.loads(line) for line in result.stdout.splitlines()]
        summary = next(e for e in events if e.get("message_type") == "summary")
        return summary["snapshot_id"]

    def snapshots(self, *args):
        return json.loads(self.run_restic("--json", "snapshots", *args).stdout)

    # ---- contract lines ----------------------------------------------------

    def test_backup_records_both_paths_and_export_is_found_by_suffix(self):
        """`paths` lists both absolute paths; the export is located by suffix."""
        item = self.drop / "item"
        item.mkdir()
        (item / "document.txt").write_bytes(b"synthetic payload")
        export = self.write_export()
        snapshot_id = self.backup(item, export)

        [snapshot] = self.snapshots("--tag", f"dropin:attempt={ATTEMPT_ID}")
        self.assertEqual(snapshot["id"], snapshot_id)
        self.assertEqual(sorted(snapshot["paths"]), sorted([str(item), str(export)]))
        for path in snapshot["paths"]:
            self.assertTrue(path.startswith("/"), path)

        suffix = f"/export/{OCC_ID}-{ATTEMPT_ID}.sqlite"
        located = [p for p in snapshot["paths"] if p.endswith(suffix)]
        self.assertEqual(located, [str(export)])
        # Never by index: record the order restic actually returned.
        self.assertIn(snapshot["paths"].index(str(export)), (0, 1))

    def test_seven_canonical_tags_round_trip_verbatim(self):
        item = self.drop / "item.txt"
        item.write_bytes(b"tagged")
        digest = hashlib.sha256(b"catalog").hexdigest()
        snapshot_id = self.backup(item, tags=self.canonical_tags(kind="file",
                                                                digest=digest))
        [snapshot] = self.snapshots("--tag", f"dropin:occ={OCC_ID}")
        self.assertEqual(snapshot["id"], snapshot_id)
        self.assertEqual(sorted(snapshot["tags"]), sorted([
            "dropin:v=1",
            f"dropin:store={STORE_ID}",
            f"dropin:occ={OCC_ID}",
            f"dropin:attempt={ATTEMPT_ID}",
            "dropin:seq=1",
            "dropin:kind=file",
            f"dropin:catalog-sha256={digest}",
        ]))

    def test_duplicate_reserved_tag_survives_so_the_parser_can_reject_it(self):
        """The strict parser must be able to *see* a duplicate reserved key."""
        item = self.drop / "item.txt"
        item.write_bytes(b"duplicate tags")
        tags = self.canonical_tags(kind="file") + ["--tag", "dropin:seq=2"]
        self.backup(item, tags=tags)
        [snapshot] = self.snapshots("--tag", f"dropin:occ={OCC_ID}")
        seq_tags = sorted(t for t in snapshot["tags"] if t.startswith("dropin:seq="))
        self.assertEqual(seq_tags, ["dropin:seq=1", "dropin:seq=2"],
                         "restic collapsed a duplicate reserved key; the strict "
                         "parser cannot rely on seeing it")

    def test_snapshots_json_order_is_not_relied_upon(self):
        item = self.drop / "item.txt"
        item.write_bytes(b"ordering")
        ids = []
        for seq in (1, 2, 3):
            attempt = f"{STORE_ID}.01J000000000000000000000{seq}A"
            ids.append(self.backup(item, tags=self.canonical_tags(
                attempt_id=attempt, seq=seq, kind="file")))
            time.sleep(1.1)  # distinct `time` values
        listed = self.snapshots("--tag", "dropin:v=1")
        self.assertEqual(len(listed), 3)
        # We sort by `time` ourselves; record whether restic happened to agree.
        by_time = [s["id"] for s in sorted(listed, key=lambda s: s["time"])]
        self.assertEqual(by_time, ids)
        self.observed_order_matches = [s["id"] for s in listed] == ids

    def test_ls_json_recursive_node_fields_and_root_entry(self):
        item = self.drop / "tree"
        (item / "sub").mkdir(parents=True)
        (item / "sub" / "file.txt").write_bytes(b"12345")
        (item / "link").symlink_to("sub/file.txt")
        (item / "empty").mkdir()
        snapshot_id = self.backup(item)

        # The documented argv passes the item path, which scopes the listing.
        result = self.run_restic("--json", "ls", "--recursive", snapshot_id, str(item))
        objects = [json.loads(line) for line in result.stdout.splitlines()]
        header = objects[0]
        self.assertEqual(header["struct_type"], "snapshot")
        nodes = [o for o in objects[1:] if o["struct_type"] == "node"]
        paths = {n["path"]: n for n in nodes}
        for node in nodes:
            self.assertTrue(node["path"].startswith("/"), node["path"])
            self.assertIn(node["type"], {"file", "dir", "symlink"})

        # The root entry of the backed-up path is present, as a dir node, and
        # nothing above it is: scoping by path suppresses the ancestor nodes.
        self.assertEqual(set(paths), {
            str(item), f"{item}/empty", f"{item}/link", f"{item}/sub",
            f"{item}/sub/file.txt",
        })
        self.assertEqual(paths[str(item)]["type"], "dir")
        self.assertEqual(paths[f"{item}/sub/file.txt"]["type"], "file")
        self.assertEqual(paths[f"{item}/sub/file.txt"]["size"], 5)
        self.assertEqual(paths[f"{item}/link"]["type"], "symlink")
        self.assertEqual(paths[f"{item}/empty"]["type"], "dir")
        # Only regular files carry `size`.
        self.assertNotIn("size", paths[f"{item}/empty"])

        # CONTRACT BREAK, recorded: 0.19.1 `ls --json` nodes carry no
        # `linktarget`, so reconciliation cannot compare link targets here. The
        # tar member `linkname` (see the tar test) is where targets are proven.
        self.assertNotIn("linktarget", paths[f"{item}/link"],
                         "restic began emitting linktarget in ls --json; the "
                         "contract may go back to reconciling targets there")

        # Without the path argument the listing additionally contains a dir node
        # for every ancestor of the backed-up path. The archiver always passes
        # the path, so it never sees these; recorded so nobody drops the arg.
        unscoped = self.run_restic("--json", "ls", "--recursive", snapshot_id)
        unscoped_paths = {json.loads(line)["path"]
                          for line in unscoped.stdout.splitlines()
                          if json.loads(line)["struct_type"] == "node"}
        self.assertTrue(unscoped_paths > set(paths))
        self.assertIn(str(item.parent), unscoped_paths)

    def test_dump_tar_member_naming_for_special_entries(self):
        item = self.drop / "tree"
        item.mkdir()
        (item / "empty").mkdir()
        (item / "file.txt").write_bytes(b"member")
        (item / "hardlink.txt").hardlink_to(item / "file.txt")
        (item / "link").symlink_to("file.txt")
        weird = os.fsdecode(b"non-utf8-\xff.bin")
        try:
            (item / weird).write_bytes(b"weird name")
        except OSError as error:
            if error.errno != errno.EILSEQ:
                raise
            weird = None  # macOS filesystems reject surrogateescaped names
        snapshot_id = self.backup(item)

        stream = self.run_restic("dump", "--archive", "tar", snapshot_id,
                                 str(item)).stdout
        with tarfile.open(fileobj=io.BytesIO(stream), mode="r|") as tar:
            members = {m.name: m for m in tar}
        # Member names are the absolute path without the leading slash.
        expected_prefix = str(item).lstrip("/")
        for name in members:
            self.assertTrue(name.startswith(expected_prefix), name)
        self.assertIn(f"{expected_prefix}/file.txt", members)
        if weird is not None:
            self.assertIn(f"{expected_prefix}/{weird}", members)
        self.assertTrue(any(n.rstrip("/").endswith("/empty") for n in members),
                        f"empty directory missing from tar members: {sorted(members)}")
        link = members[f"{expected_prefix}/link"]
        self.assertTrue(link.issym())
        self.assertEqual(link.linkname, "file.txt")
        hardlink = members[f"{expected_prefix}/hardlink.txt"]
        self.assertIn(hardlink.type, (tarfile.REGTYPE, tarfile.LNKTYPE))
        self.hardlink_type = "link" if hardlink.islnk() else "regular"

    def test_dump_stdout_consumed_to_eof_then_wait_terminates(self):
        item = self.drop / "big.bin"
        item.write_bytes(os.urandom(4 * 1024 * 1024))
        snapshot_id = self.backup(item)
        argv = self.base + ["dump", snapshot_id, str(item)]
        digest = hashlib.sha256()
        with subprocess.Popen(argv, cwd=self.root, env=self.env,
                              stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE) as process:
            for chunk in iter(lambda: process.stdout.read(65536), b""):
                digest.update(chunk)
            process.stderr.read()
            self.assertEqual(process.wait(timeout=60), 0)
        self.assertEqual(digest.hexdigest(),
                         hashlib.sha256(item.read_bytes()).hexdigest())

    def test_killed_dump_yields_a_truncated_stream_the_reader_reports(self):
        item = self.drop / "tree"
        item.mkdir()
        for index in range(64):
            (item / f"file-{index:03d}.bin").write_bytes(os.urandom(256 * 1024))
        snapshot_id = self.backup(item)
        argv = self.base + ["dump", "--archive", "tar", snapshot_id, str(item)]
        process = subprocess.Popen(argv, cwd=self.root, env=self.env,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            first = process.stdout.read(65536)
            self.assertTrue(first)
            process.send_signal(signal.SIGKILL)
            rest = process.stdout.read()
        finally:
            process.stdout.close()
            process.stderr.close()
            process.wait(timeout=30)
        self.assertNotEqual(process.returncode, 0)

        truncated = io.BytesIO(first + rest)
        with self.assertRaises(tarfile.TarError):
            with tarfile.open(fileobj=truncated, mode="r|") as tar:
                for _ in tar:
                    pass

    def test_no_cache_read_reaches_the_remote(self):
        item = self.drop / "item.bin"
        content = os.urandom(1024 * 1024)
        item.write_bytes(content)
        snapshot_id = self.backup(item)

        # Wipe every cached byte, then read without letting restic rebuild one.
        for child in self.cache.iterdir():
            if child.is_dir():
                import shutil

                shutil.rmtree(child)
            else:
                child.unlink()
        result = subprocess.run(
            self.base + ["--no-cache", "dump", snapshot_id, str(item)],
            cwd=self.root, env=self.env, capture_output=True, timeout=120)
        self.assertEqual(result.returncode, 0,
                         result.stderr.decode(errors="replace"))
        self.assertEqual(result.stdout, content)
        self.assertEqual(list(self.cache.iterdir()), [],
                         "--no-cache populated the cache directory")


if __name__ == "__main__":
    unittest.main(verbosity=2)
