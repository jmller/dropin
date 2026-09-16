"""Real publication, clean-state recovery, and offline metadata queries."""

import json
import os
from pathlib import Path
import shutil
import unittest

from dropin.config import load
from dropin.engine.restic import ResticEngine
from dropin.recover import recover
from dropin.report import Report
from tests.integration import test_restic_roundtrip as roundtrip
from tests.support import run_cli


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "synthetic"


@unittest.skipUnless(os.environ.get("DROPIN_RESTIC_BIN") and os.environ.get("DROPIN_RCLONE_BIN"),
                     "pinned tool environment not provided")
class QueryAfterRecoverTest(roundtrip.ResticTestCase):
    def test_real_recovery_restores_same_query_paths_and_full_captured_record(self):
        path = self.drop_file("query-recover.txt", b"real query after recover\n")
        self.macos.set_mdls(
            str(path), (FIXTURES / "mdls" / "pdf_tagged.txt").read_text())
        self.macos.set_importer(
            str(path), (FIXTURES / "mdimport" / "pdf_with_text.txt").read_text())
        self.macos.set_xattr(
            str(path), "com.apple.metadata:_kMDItemUserTags",
            (FIXTURES / "xattr" / "tags_plist.bin").read_bytes())
        self.macos.set_xattr(
            str(path), "com.apple.metadata:kMDItemFinderComment",
            (FIXTURES / "xattr" / "comment_plist.bin").read_bytes())
        report = self.run_drain()
        self.assertEqual(report.exit_code(), 0, report.render_human())
        self.assertFalse(path.exists())
        archive_path = self.occurrence("query-recover.txt")["archive_path"]
        before = run_cli(["--config", str(self.config_path), "--json", "show", archive_path])
        self.assertEqual(before.returncode, 0, before.stderr)
        full_before = json.loads(before.stdout)
        self.assertEqual(full_before["name"], "query-recover.txt")
        self.assertEqual(full_before["uti"], "com.adobe.pdf")
        self.assertEqual(full_before["created"], "2026-03-01T10:11:12Z")
        self.assertEqual(full_before["modified"], "2026-03-02T08:00:00Z")
        self.assertEqual(full_before["kind"], "PDF Document")
        self.assertEqual(full_before["tags"], ["important", "tax"])
        self.assertEqual(full_before["comment"], 'quarterly "final" copy')
        self.assertEqual(
            full_before["attributes"]["kMDItemImporterOnlyKey"]["value"],
            "importer-supplied")
        self.db.close()
        shutil.rmtree(self.state)
        fresh = self.root / "fresh"
        for name in ("cache", "tmp"):
            (fresh / name).mkdir(parents=True)
        config_path = self.root / "fresh.toml"
        config_path.write_text(self.config_path.read_text().replace(str(self.state), str(fresh)))
        recovery_report = Report(verb="recover", run_id="query-recover")
        recover(ResticEngine(load(config_path)), fresh, recovery_report)
        self.assertEqual(recovery_report.exit_code(), 0, recovery_report.render_human())
        # Queries now have neither usable tools nor a reachable repository.
        config_path.write_text(config_path.read_text().replace(self.restic, "/missing/restic")
                               .replace(self.rclone, "/missing/rclone")
                               .replace(f"rclone:local:{self.repo}", "rclone:absent:/offline"))
        found = run_cli(["--config", str(config_path), "find", "--name", "query-recover"])
        self.assertEqual(found.returncode, 0, found.stderr)
        self.assertEqual(found.stdout.decode().strip(), archive_path)
        found_text = run_cli([
            "--config", str(config_path), "find", "--text", "quarterly"])
        self.assertEqual(found_text.stdout, found.stdout)
        found_metadata = run_cli([
            "--config", str(config_path), "find", "--kind", "com.adobe.pdf",
            "--tag", "tax", "--since", "2026-03-01",
            "--modified-until", "2026-03-03"])
        self.assertEqual(found_metadata.returncode, 0, found_metadata.stderr)
        self.assertEqual(found_metadata.stdout, found.stdout)
        shown = run_cli(["--config", str(config_path), "--json", "show", archive_path])
        self.assertEqual(shown.returncode, 0, shown.stderr)
        full_after = json.loads(shown.stdout)
        self.assertEqual(full_after["state"], "recoverable")
        for key in ("archive_path", "entry", "normalized", "attributes", "attribute_values", "xattrs", "snapshot"):
            self.assertEqual(full_after[key], full_before[key], key)
