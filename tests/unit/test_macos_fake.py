"""The macOS seam and its fixture-backed fake."""

from __future__ import annotations

from pathlib import Path
import plistlib
import tempfile
import unittest

from dropin.macos.fake import FakeMacOS
from dropin.macos.interface import CaptureFailure, Unsupported

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "synthetic"


class ImporterOutcomeTest(unittest.TestCase):
    """Three distinguishable outcomes, never conflated."""

    def test_importer_returns_a_dictionary(self):
        macos = FakeMacOS()
        macos.set_importer("/drop/report.pdf",
                           (FIXTURES / "mdimport" / "pdf_with_text.txt").read_text())
        attributes = macos.importer_attributes("/drop/report.pdf")
        self.assertEqual(attributes["kMDItemTextContent"].value,
                         "quarterly results, revenue up")

    def test_importer_returns_an_empty_dictionary(self):
        macos = FakeMacOS()
        macos.set_importer("/drop/photo.raw",
                           (FIXTURES / "mdimport" / "no_text.txt").read_text())
        attributes = macos.importer_attributes("/drop/photo.raw")
        self.assertNotIn("kMDItemTextContent", attributes)
        self.assertTrue(attributes)

    def test_importer_with_no_attributes_at_all_is_empty_not_a_failure(self):
        macos = FakeMacOS()
        macos.set_importer("/drop/x", "mdimport: no importer\n")
        self.assertEqual(macos.importer_attributes("/drop/x"), {})

    def test_importer_failure_is_distinguishable_from_empty(self):
        macos = FakeMacOS()
        macos.fail_importer("/drop/broken", "importer crashed")
        with self.assertRaises(CaptureFailure) as caught:
            macos.importer_attributes("/drop/broken")
        self.assertIn("importer crashed", str(caught.exception))

    def test_unconfigured_path_is_a_capture_failure_not_silence(self):
        with self.assertRaises(CaptureFailure):
            FakeMacOS().importer_attributes("/drop/never-configured")


class MdlsTest(unittest.TestCase):
    def test_mdls_returns_parsed_attributes(self):
        macos = FakeMacOS()
        macos.set_mdls("/drop/report.pdf",
                       (FIXTURES / "mdls" / "pdf_tagged.txt").read_text())
        attributes = macos.mdls("/drop/report.pdf")
        self.assertEqual(attributes["kMDItemContentType"].value, "com.adobe.pdf")


class FinderMetadataTest(unittest.TestCase):
    def setUp(self):
        self.macos = FakeMacOS()
        self.macos.set_xattr(
            "/drop/report.pdf", "com.apple.metadata:_kMDItemUserTags",
            (FIXTURES / "xattr" / "tags_plist.bin").read_bytes())
        self.macos.set_xattr(
            "/drop/report.pdf", "com.apple.metadata:kMDItemFinderComment",
            (FIXTURES / "xattr" / "comment_plist.bin").read_bytes())

    def test_tags_drop_the_colour_index(self):
        self.assertEqual(self.macos.finder_tags("/drop/report.pdf"),
                         ["tax", "important"])

    def test_comment_is_decoded(self):
        self.assertEqual(self.macos.finder_comment("/drop/report.pdf"),
                         'quarterly "final" copy')

    def test_absent_tags_are_an_empty_list_not_an_error(self):
        self.assertEqual(self.macos.finder_tags("/drop/untagged.pdf"), [])

    def test_absent_comment_is_none(self):
        self.assertIsNone(self.macos.finder_comment("/drop/untagged.pdf"))

    def test_empty_tag_plist_is_an_empty_list(self):
        self.macos.set_xattr("/drop/e", "com.apple.metadata:_kMDItemUserTags",
                             (FIXTURES / "xattr" / "empty_tags_plist.bin").read_bytes())
        self.assertEqual(self.macos.finder_tags("/drop/e"), [])

    def test_undecodable_tag_plist_is_a_capture_failure(self):
        self.macos.set_xattr("/drop/bad", "com.apple.metadata:_kMDItemUserTags",
                             (FIXTURES / "xattr" / "not_a_plist.bin").read_bytes())
        with self.assertRaises(CaptureFailure):
            self.macos.finder_tags("/drop/bad")

    def test_round_trip_against_a_generated_plist(self):
        self.macos.set_xattr("/drop/gen", "com.apple.metadata:_kMDItemUserTags",
                             plistlib.dumps(["alpha\n2"], fmt=plistlib.FMT_BINARY))
        self.assertEqual(self.macos.finder_tags("/drop/gen"), ["alpha"])


class XattrTest(unittest.TestCase):
    def test_readable_xattrs_are_returned_with_status_ok(self):
        macos = FakeMacOS()
        macos.set_xattr("/drop/x", "user.simple", b"value")
        entries = macos.xattrs("/drop/x")
        self.assertEqual(entries["user.simple"].value, b"value")
        self.assertEqual(entries["user.simple"].status, "ok")

    def test_unreadable_xattr_is_recorded_not_fatal(self):
        macos = FakeMacOS()
        macos.set_xattr("/drop/x", "user.simple", b"value")
        macos.make_xattr_unreadable("/drop/x", "user.exotic", "EPERM")
        entries = macos.xattrs("/drop/x")
        self.assertEqual(entries["user.simple"].status, "ok")
        self.assertIsNone(entries["user.exotic"].value)
        self.assertEqual(entries["user.exotic"].status, "skipped:EPERM")


class BundlePredicateTest(unittest.TestCase):
    """Only the content-type tree decides; `kMDItemKind` never does."""

    def macos_with(self, fixture: str) -> FakeMacOS:
        macos = FakeMacOS()
        macos.set_mdls("/drop/item", (FIXTURES / "mdls" / f"{fixture}.txt").read_text())
        return macos

    def test_package_is_a_bundle(self):
        self.assertTrue(self.macos_with("pages_bundle").is_bundle("/drop/item"))

    def test_application_bundle_is_a_bundle(self):
        self.assertTrue(self.macos_with("app_bundle").is_bundle("/drop/item"))

    def test_plain_folder_is_not_a_bundle(self):
        # Its kMDItemKind is "Folder"; a kind-based predicate would misfire.
        self.assertFalse(self.macos_with("folder_plain").is_bundle("/drop/item"))

    def test_plain_file_is_not_a_bundle(self):
        self.assertFalse(self.macos_with("pdf_tagged").is_bundle("/drop/item"))


class CapabilityTest(unittest.TestCase):
    """No fake reports ownership support unless a test configures it."""

    def test_default_capability_is_unsupported(self):
        capabilities = FakeMacOS().capabilities()
        self.assertFalse(capabilities.ownership_check)
        self.assertTrue(capabilities.ownership_reason)

    def test_open_descriptors_raises_by_default(self):
        with self.assertRaises(Unsupported):
            FakeMacOS().open_descriptors("/drop/x", is_dir=False)

    def test_configured_support_reports_clear(self):
        macos = FakeMacOS()
        macos.set_ownership_supported(True)
        self.assertTrue(macos.capabilities().ownership_check)
        self.assertEqual(macos.open_descriptors("/drop/x", is_dir=False), [])

    def test_configured_holder_is_reported(self):
        macos = FakeMacOS()
        macos.set_ownership_supported(True)
        macos.set_open_holder("/drop/held.txt", 4242)
        self.assertEqual(macos.open_descriptors("/drop/held.txt", is_dir=False),
                         [4242])

    def test_capability_can_be_lost_mid_run(self):
        macos = FakeMacOS()
        macos.set_ownership_supported(True)
        macos.set_ownership_supported(False, reason="lsof vanished")
        with self.assertRaises(Unsupported):
            macos.open_descriptors("/drop/x", is_dir=False)

    def test_directory_form_is_distinguished_from_the_file_form(self):
        macos = FakeMacOS()
        macos.set_ownership_supported(True)
        macos.set_open_holder("/drop/tree", 7, is_dir=True)
        self.assertEqual(macos.open_descriptors("/drop/tree", is_dir=True), [7])
        self.assertEqual(macos.open_descriptors("/drop/tree", is_dir=False), [])


class LaunchAgentTest(unittest.TestCase):
    def test_write_launch_agent_records_the_plist(self):
        with tempfile.TemporaryDirectory() as temp:
            macos = FakeMacOS()
            target = Path(temp) / "dev.dropin.drain.plist"
            macos.write_launch_agent(target, label="dev.dropin.drain",
                                     program=["/usr/bin/python3", "-m", "dropin"],
                                     queue_directories=["/drop"], interval=900)
            self.assertEqual(macos.launch_agents[-1].path, target)
            self.assertEqual(macos.launch_agents[-1].label, "dev.dropin.drain")
