"""Synthetic, explicitly confirmed query corpus; no engine or remote."""

import hashlib
from pathlib import Path
import unittest

from dropin.capture.mdls_parser import Attr
from dropin.capture.model import CapturedEntry, CapturedItem
from dropin.engine.interface import Identity
from dropin.macos.interface import XattrValue
from dropin.spool.walk import WalkEntry
from dropin.store import records
from dropin.store.db import connect
from tests.support import TempStateDir, run_cli


class QueryTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = TempStateDir(prefix="dropin-query-")
        self.addCleanup(self.temp.cleanup)
        self.db = connect(self.temp.store_path)
        self.addCleanup(self.db.close)
        self.store_id = records.initialise_store(self.db)
        self.paths = {}
        self.hashes = {}
        names = ["Tax-March.PDF", "tax-april.pdf", "receipt.pdf", "letter.txt",
                 "Notes.txt", "photo.jpg", "budget.csv", "tax-old.pdf",
                 "Résumé.txt", "100%_literal.txt", "empty.txt", "draft.pdf"]
        for i, name in enumerate(names):
            self.seed(name, size=i * 10, uti="com.adobe.pdf" if name.lower().endswith("pdf") else "public.text",
                      created=("2026-03-01T00:00:00Z" if i % 3 == 0 else
                               "2026-03-31T23:59:59.999999Z" if i % 3 == 1 else
                               "2026-04-01T00:00:00Z"),
                      modified="2026-04-02T12:00:00Z" if i % 2 == 0 else "2026-04-03T00:00:00Z",
                      tags=(["tax"] if i % 2 == 0 else []) + (["work"] if i % 3 == 0 else []),
                      text="orchid invoice" if i in (0, 2, 5) else None,
                      comment="orchid comment" if i == 4 else None)
        self.config_path = self.temp.root / "config.toml"
        password = self.temp.root / "pw"
        password.write_text("disposable")
        password.chmod(0o600)
        self.config_path.write_text(f'''[paths]
drop_dir = "{self.temp.drop_dir}"
state_dir = "{self.temp.state_dir}"
[repository]
repo = "rclone:unreachable:/no-such-archive"
password_file = "{password}"
[tools]
restic = "/not-installed/restic"
rclone = "/not-installed/rclone"
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
''')

    def seed(self, name, *, size=1, uti="public.text", created=None, modified=None,
             tags=(), text=None, comment=None, state="evicted", confirmed=True,
             kind="file", children=()):
        digest = hashlib.sha256(name.encode()).hexdigest()
        attributes = {
            ("kMDItemContentType", "mdls"): Attr(uti, "string"),
            ("kMDItemKind", "mdls"): Attr("PDF document" if uti == "com.adobe.pdf" else "Text", "string"),
            ("kMDItemContentCreationDate", "mdls"): Attr(created, "date" if created else "null"),
            ("kMDItemContentModificationDate", "mdls"): Attr(modified, "date" if modified else "null"),
            ("overlap", "mdls"): Attr("spotlight", "string"),
            ("overlap", "importer"): Attr("importer", "string"),
            ("importerOnly", "importer"): Attr({"nested": [1, True, None]}, "dict"),
        }
        entries = []
        for rel in ("", *children):
            entry_type = "dir" if not rel and kind != "file" else "file"
            entry_size = size if entry_type == "file" else None
            fp = WalkEntry(rel, entry_type, entry_size, 0o100600, 1, 1, 1, 1, None)
            entries.append(CapturedEntry(
                rel, entry_type, entry_size, digest if entry_type == "file" else None,
                None, fp.mode, fp, searchable=not rel or kind != "bundle",
                attributes=attributes, tags=list(tags), text=text, comment=comment,
                xattrs={"user.ok": XattrValue(b"data", "ok"),
                        "user.unreadable": XattrValue(None, "skipped:permission")}))
        item = CapturedItem(str(self.temp.drop_dir / name), name, kind, digest, size, entries)
        occ = records.record_occurrence(self.db, item, self.store_id)
        self.db.execute("UPDATE occurrence SET recorded_at=? WHERE occ_id=?",
                        ("2026-05-01T00:00:00Z", occ))
        if confirmed:
            attempt = records.start_attempt(self.db, occ, self.store_id, export_path="/unused/export")
            snapshot = hashlib.sha256(occ.encode()).hexdigest()
            records.set_attempt_snapshot(self.db, attempt.attempt_id, snapshot)
            records.finish_attempt(self.db, attempt.attempt_id, "confirmed")
            identity = Identity(self.store_id, occ, attempt.attempt_id, attempt.export_seq, kind, "a" * 64)
            records.observe_snapshot(self.db, snapshot, identity, status="confirmed")
            self.db.execute("UPDATE occurrence SET confirmed_attempt_id=? WHERE occ_id=?",
                            (attempt.attempt_id, occ))
        self.db.execute("UPDATE occurrence SET state=? WHERE occ_id=?", (state, occ))
        self.paths[name] = records.get_occurrence(self.db, occ)["archive_path"]
        self.hashes[name] = digest
        return occ

    def cli(self, *args, stdin=None):
        return run_cli(["--config", str(self.config_path), *args], stdin=stdin)
