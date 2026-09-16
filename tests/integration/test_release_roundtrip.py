"""Candidate release archive/query/restore and corruption gates."""
from __future__ import annotations

import os
import unittest

from tests.integration import test_restic_roundtrip as roundtrip
from tests.support import run_cli


@unittest.skipUnless(
    os.environ.get("DROPIN_RESTIC_BIN") and os.environ.get("DROPIN_RCLONE_BIN"),
    "pinned tool environment not provided",
)
class ReleaseRoundTripTest(roundtrip.ResticTestCase):
    """Candidate-release behavior that must remain true after eviction."""

    def cli(self, *args, stdin=None):
        return run_cli(["--config", str(self.config_path), *args], stdin=stdin)

    def destination(self):
        destination = self.root / "release-out"
        destination.mkdir()
        return destination

    def test_evicted_items_query_and_verified_file_tree_bundle_restore(self):
        file = self.drop_file("release.txt", b"release payload\n")
        tree = self.drop_tree("ReleaseTree")
        bundle = self.drop_tree("Release.pages")

        self.assertEqual(self.run_drain().exit_code(), 0)
        for source in (file, tree, bundle):
            self.assertFalse(source.exists())

        found = self.cli("find", "--glob", "release.txt")
        self.assertEqual(found.returncode, 0, found.stderr)
        self.assertIn(b"release.txt", found.stdout)

        out = self.destination()
        restored = self.cli("get", "-", "-o", str(out), stdin=found.stdout)
        self.assertEqual(restored.returncode, 0, restored.stderr)
        self.assertEqual((out / file.name).read_bytes(), b"release payload\n")

        existing = out / tree.name
        existing.mkdir()
        (existing / "sentinel").write_bytes(b"keep me")
        forced = self.cli("get", self.occurrence(tree.name)["archive_path"],
                          "--force", "-o", str(out))
        self.assertEqual(forced.returncode, 0, forced.stderr)
        self.assertEqual((existing / "a.txt").read_bytes(), b"alpha\n" * 100)
        [aside] = list(out.glob(".dropin-aside-*"))
        self.assertEqual((aside / tree.name / "sentinel").read_bytes(), b"keep me")

        for source in (bundle,):
            archive = self.occurrence(source.name)["archive_path"]
            result = self.cli("get", archive, "-o", str(out))
            self.assertEqual(result.returncode, 0, result.stderr)
            restored_root = out / source.name
            self.assertEqual((restored_root / "sub" / "b.txt").read_bytes(),
                             b"beta\n" * 100)
            self.assertEqual(os.readlink(restored_root / "link"), "a.txt")

    def test_corrupt_archive_refuses_without_partial_output_or_state_change(self):
        source = self.drop_file("release-corrupt.bin", b"x" * 65536)
        self.assertEqual(self.run_drain().exit_code(), 0)
        occurrence = self.occurrence(source.name)
        attempt = roundtrip.records.get_attempt(
            self.db, occurrence["confirmed_attempt_id"])
        blob = self.engine.node_content_ids(attempt["snapshot_id"], str(source))[0]
        roundtrip.CatalogCorruptionTest._flip_byte_in_blob(self, blob)

        out = self.destination()
        before = list(self.db.iterdump())
        result = self.cli("get", occurrence["archive_path"], "-o", str(out))
        self.assertEqual(result.returncode, 4, result.stderr)
        self.assertEqual(list(out.iterdir()), [])
        self.assertEqual(before, list(self.db.iterdump()))
