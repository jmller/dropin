"""Metadata capture before any transfer (metadata-first ordering)."""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest

from dropin.capture.extract import CaptureAborted, capture_item
from dropin.macos.fake import FakeMacOS

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "synthetic"


def fixture(kind: str, name: str) -> str:
    return (FIXTURES / kind / f"{name}.txt").read_text()


class CaptureTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dropin-capture-")
        self.addCleanup(self.temp.cleanup)
        self.drop = Path(self.temp.name)
        self.macos = FakeMacOS()

    def configure(self, path: Path, mdls: str = "pdf_tagged",
                  importer: str | None = "pdf_with_text") -> None:
        self.macos.set_mdls(str(path), fixture("mdls", mdls))
        if importer is not None:
            self.macos.set_importer(str(path), fixture("mdimport", importer))


class FileCaptureTest(CaptureTestCase):
    def setUp(self):
        super().setUp()
        self.path = self.drop / "report.pdf"
        self.path.write_bytes(b"%PDF-1.7 synthetic")
        self.configure(self.path)
        self.macos.set_xattr(str(self.path),
                             "com.apple.metadata:_kMDItemUserTags",
                             (FIXTURES / "xattr" / "tags_plist.bin").read_bytes())
        self.macos.set_xattr(str(self.path),
                             "com.apple.metadata:kMDItemFinderComment",
                             (FIXTURES / "xattr" / "comment_plist.bin").read_bytes())
        self.item = capture_item(self.macos, self.path)

    def test_one_entry_for_a_file(self):
        self.assertEqual(len(self.item.entries), 1)
        self.assertEqual(self.item.entries[0].rel_path, "")
        self.assertEqual(self.item.kind, "file")

    def test_content_hash_and_stat(self):
        import hashlib

        entry = self.item.entries[0]
        self.assertEqual(entry.sha256,
                         hashlib.sha256(self.path.read_bytes()).hexdigest())
        self.assertEqual(entry.size_bytes, len(b"%PDF-1.7 synthetic"))
        self.assertGreater(entry.fingerprint.inode, 0)

    def test_mdls_attributes_are_recorded_with_their_source(self):
        attributes = self.item.entries[0].attributes
        self.assertEqual(attributes[("kMDItemContentType", "mdls")].value,
                         "com.adobe.pdf")

    def test_importer_only_keys_are_retained_with_importer_source(self):
        attributes = self.item.entries[0].attributes
        self.assertEqual(attributes[("kMDItemImporterOnlyKey", "importer")].value,
                         "importer-supplied")

    def test_both_sources_survive_a_key_collision(self):
        attributes = self.item.entries[0].attributes
        self.assertIn(("kMDItemKind", "mdls"), attributes)
        self.assertIn(("kMDItemKind", "importer"), attributes)

    def test_tags_and_comment(self):
        entry = self.item.entries[0]
        self.assertEqual(entry.tags, ["tax", "important"])
        self.assertEqual(entry.comment, 'quarterly "final" copy')

    def test_searchable_text_comes_from_the_importer(self):
        self.assertEqual(self.item.entries[0].text,
                         "quarterly results, revenue up")

    def test_xattrs_are_recorded(self):
        names = set(self.item.entries[0].xattrs)
        self.assertIn("com.apple.metadata:_kMDItemUserTags", names)

    def test_capture_status_is_ok(self):
        self.assertEqual(self.item.entries[0].capture_status, "ok")


class DegradedCaptureTest(CaptureTestCase):
    """Metadata failures are recorded, never fatal."""

    def path_with(self, importer: str | None):
        path = self.drop / "item.pdf"
        path.write_bytes(b"x")
        self.configure(path, importer=importer)
        return path

    def test_importer_without_text_yields_no_text_and_status_ok(self):
        item = capture_item(self.macos, self.path_with("no_text"))
        self.assertIsNone(item.entries[0].text)
        self.assertEqual(item.entries[0].capture_status, "ok")

    def test_importer_failure_is_partial_and_the_archive_proceeds(self):
        path = self.path_with(None)
        self.macos.fail_importer(str(path), "importer crashed")
        item = capture_item(self.macos, path)
        self.assertEqual(item.entries[0].capture_status, "partial:importer")
        self.assertIsNone(item.entries[0].text)
        self.assertTrue(item.entries[0].sha256)

    def test_mdls_failure_is_partial_not_fatal(self):
        path = self.drop / "nomdls.bin"
        path.write_bytes(b"x")
        self.macos.set_importer(str(path), fixture("mdimport", "no_text"))
        item = capture_item(self.macos, path)
        self.assertIn("mdls", item.entries[0].capture_status)

    def test_unreadable_xattr_is_recorded_with_a_reason(self):
        path = self.path_with("no_text")
        self.macos.set_xattr(str(path), "user.readable", b"ok")
        self.macos.make_xattr_unreadable(str(path), "user.exotic", "EPERM")
        item = capture_item(self.macos, path)
        self.assertEqual(item.entries[0].xattrs["user.exotic"].status,
                         "skipped:EPERM")
        self.assertEqual(item.entries[0].xattrs["user.readable"].status, "ok")


class TreeCaptureTest(CaptureTestCase):
    def setUp(self):
        super().setUp()
        self.tree = self.drop / "Receipts"
        (self.tree / "sub").mkdir(parents=True)
        (self.tree / "a.txt").write_bytes(b"alpha")
        (self.tree / "sub" / "b.txt").write_bytes(b"beta")
        (self.tree / "link").symlink_to("a.txt")
        for path in (self.tree, self.tree / "a.txt", self.tree / "sub",
                     self.tree / "sub" / "b.txt", self.tree / "link"):
            self.macos.set_mdls(str(path), fixture("mdls", "text_plain"))
            self.macos.set_importer(str(path), fixture("mdimport", "no_text"))
        self.macos.set_mdls(str(self.tree), fixture("mdls", "folder_plain"))
        self.item = capture_item(self.macos, self.tree)

    def test_kind_is_dir_for_a_plain_folder(self):
        self.assertEqual(self.item.kind, "dir")

    def test_every_descendant_is_an_entry_in_byte_order(self):
        rel = [entry.rel_path for entry in self.item.entries]
        self.assertEqual(rel, sorted(rel))
        self.assertEqual(set(rel), {"", "a.txt", "link", "sub", "sub/b.txt"})

    def test_regular_files_carry_hashes_and_others_do_not(self):
        by_path = {entry.rel_path: entry for entry in self.item.entries}
        self.assertTrue(by_path["a.txt"].sha256)
        self.assertIsNone(by_path["sub"].sha256)
        self.assertIsNone(by_path["link"].sha256)
        self.assertEqual(by_path["link"].link_target, "a.txt")

    def test_all_entries_are_searchable_in_a_plain_tree(self):
        self.assertTrue(all(entry.searchable for entry in self.item.entries))

    def test_manifest_hash_is_deterministic_and_order_independent(self):
        again = capture_item(self.macos, self.tree)
        self.assertEqual(self.item.root_sha256, again.root_sha256)
        self.assertEqual(len(self.item.root_sha256), 64)

    def test_manifest_hash_changes_with_content(self):
        (self.tree / "a.txt").write_bytes(b"ALPHA-DIFFERENT")
        self.assertNotEqual(capture_item(self.macos, self.tree).root_sha256,
                            self.item.root_sha256)


class BundleCaptureTest(CaptureTestCase):
    def setUp(self):
        super().setUp()
        self.bundle = self.drop / "Letter.pages"
        (self.bundle / "Data").mkdir(parents=True)
        (self.bundle / "index.xml").write_bytes(b"<xml/>")
        (self.bundle / "Data" / "image.bin").write_bytes(b"image")
        self.macos.set_mdls(str(self.bundle), fixture("mdls", "pages_bundle"))
        self.macos.set_importer(str(self.bundle), fixture("mdimport", "bundle"))
        for path in (self.bundle / "index.xml", self.bundle / "Data",
                     self.bundle / "Data" / "image.bin"):
            self.macos.set_mdls(str(path), fixture("mdls", "text_plain"))
            self.macos.set_importer(str(path), fixture("mdimport", "no_text"))
        self.item = capture_item(self.macos, self.bundle)

    def test_kind_is_bundle(self):
        self.assertEqual(self.item.kind, "bundle")

    def test_complete_internal_tree_is_captured(self):
        self.assertEqual({entry.rel_path for entry in self.item.entries},
                         {"", "Data", "Data/image.bin", "index.xml"})

    def test_only_the_root_is_searchable(self):
        by_path = {entry.rel_path: entry for entry in self.item.entries}
        self.assertTrue(by_path[""].searchable)
        self.assertFalse(any(entry.searchable for entry in self.item.entries
                             if entry.rel_path))

    def test_root_metadata_comes_from_the_bundle_importer(self):
        self.assertEqual(self.item.entries[0].text, "dear sir or madam")


class OrderingTest(CaptureTestCase):
    """The fingerprint brackets the hash."""

    def test_sampled_before_hashing_and_again_after(self):
        path = self.drop / "changing.bin"
        path.write_bytes(b"original")
        self.configure(path, importer="no_text")
        original_hash = None

        def mutate_during_hash(_path):
            nonlocal original_hash
            original_hash = b"mutated during hashing"
            path.write_bytes(original_hash)
            return "0" * 64

        import dropin.capture.extract as extract

        real = extract.sha256_stream
        extract.sha256_stream = mutate_during_hash
        self.addCleanup(setattr, extract, "sha256_stream", real)
        with self.assertRaises(CaptureAborted):
            capture_item(self.macos, path)

    def test_stable_source_captures_normally(self):
        path = self.drop / "stable.bin"
        path.write_bytes(b"stable")
        self.configure(path, importer="no_text")
        self.assertTrue(capture_item(self.macos, path).entries[0].sha256)


class SpecialEntryTest(CaptureTestCase):
    def test_special_entry_refuses_before_any_metadata_work(self):
        from dropin.spool.walk import SpecialEntry

        tree = self.drop / "tree"
        tree.mkdir()
        (tree / "keep.txt").write_bytes(b"x")
        os.mkfifo(tree / "pipe")
        with self.assertRaises(SpecialEntry):
            capture_item(self.macos, tree)
