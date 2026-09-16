"""Disposable captured/confirmed retrieval fixture, with an explicit fake engine."""
from dropin.engine.interface import Identity
from dropin.store import records
from tests.unit.test_verify_payload import PayloadTestCase
from tests.unit.test_config import EXAMPLE


class RetrieveTestCase(PayloadTestCase):
    def setUp(self):
        super().setUp()
        self.out = self.root / "out"
        self.out.mkdir()
        self.config_path = self.root / "config.toml"
        password = self.root / "pw"
        password.write_text("disposable")
        password.chmod(0o600)
        # State and spool must not overlap; move only the fixture database.
        state = self.root / "state"
        state.mkdir()
        self.db.close()
        (self.root / "store.sqlite").rename(state / "store.sqlite")
        from dropin.store.db import connect
        self.db = connect(state / "store.sqlite")
        self.addCleanup(self.db.close)
        self.config_path.write_text(EXAMPLE.format(drop=self.drop, state=state, password=password)
            .replace('restic = "restic"', 'restic = "/missing/restic"')
            .replace('rclone = "rclone"', 'rclone = "/missing/rclone"'))

    def archived(self, name="file.txt", content=b"verified bytes", *, tree=False, bundle=False, captured_dir_mode=None, empty_tree=False):
        path = self.drop / name
        if empty_tree:
            tree = True
            path.mkdir()
        elif tree:
            (path / "sub").mkdir(parents=True)
            (path / "sub" / "a").write_bytes(content)
            (path / "empty").mkdir()
            (path / "link").symlink_to("sub/a")
        else:
            path.write_bytes(content)
        path.chmod(0o750 if tree else 0o640)
        if captured_dir_mode is None:
            occ = self.record(path)
        else:
            from dropin.capture.extract import capture_item
            self.configure(path)
            item = capture_item(self.macos, path)
            for entry in item.entries:
                if entry.entry_type == 'dir':
                    entry.mode = captured_dir_mode
            occ = records.record_occurrence(self.db, item, self.store_id)
        if bundle:
            self.db.execute("UPDATE occurrence SET kind='bundle' WHERE occ_id=?", (occ,))
        snapshot = self.publish(path)
        attempt = records.start_attempt(self.db, occ, self.store_id, export_path="/unused")
        records.set_attempt_snapshot(self.db, attempt.attempt_id, snapshot)
        records.finish_attempt(self.db, attempt.attempt_id, "confirmed")
        records.observe_snapshot(self.db, snapshot, Identity(self.store_id, occ,
            attempt.attempt_id, attempt.export_seq, "bundle" if bundle else "dir" if tree else "file", "a" * 64), status="confirmed")
        self.db.execute("UPDATE occurrence SET confirmed_attempt_id=?,state='evicted' WHERE occ_id=?", (attempt.attempt_id, occ))
        row = records.get_occurrence(self.db, occ)
        return path, occ, snapshot, row["archive_path"]

    def restore(self, archive_path, **kwargs):
        from dropin.retrieve import retrieve
        return retrieve(self.db, self.engine, archive_path, self.out, **kwargs)
